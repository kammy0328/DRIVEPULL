"""엔진 테스트: Range 지원 로컬 서버로 분할 다운로드, 일시정지/이어받기, 오류 재시도를 확인."""
import hashlib
import os
import random
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from drivepull.engine import (DownloadError, DownloadManager, ProbeResult, SegmentedDownload,
                              Task, TransientError)

SIZE = 37 * 1024 * 1024 + 12345
DATA = random.Random(1).randbytes(SIZE)
MD5 = hashlib.md5(DATA).hexdigest()


class Handler(BaseHTTPRequestHandler):
    fail_every = 0
    counter = 0
    delay = 0.0

    def log_message(self, *a):
        pass

    def do_GET(self):
        cls = type(self)
        cls.counter += 1
        if cls.fail_every and cls.counter % cls.fail_every == 0:
            self.send_response(503)
            self.end_headers()
            return
        rng = self.headers.get("Range")
        start, end = 0, SIZE - 1
        if rng:
            a, b = rng.split("=")[1].split("-")
            start = int(a)
            end = int(b) if b else SIZE - 1
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{SIZE}")
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        pos = start
        try:
            while pos <= end:
                n = min(256 * 1024, end - pos + 1)
                self.wfile.write(DATA[pos:pos + n])
                pos += n
                if cls.delay:
                    time.sleep(cls.delay)
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/f"
    srv.shutdown()


class FakeSource:
    def __init__(self, url):
        self.url = url

    def probe(self, sess):
        r = self.open(sess, 0, 0)
        r.close()
        return ProbeResult(SIZE, r.status_code == 206)

    def open(self, sess, start, end):
        h = {"Range": f"bytes={start}-{'' if end is None else end}"} if start is not None else {}
        r = sess.get(self.url, headers=h, stream=True, timeout=10)
        if r.status_code >= 500:
            r.close()
            raise TransientError("503")
        return r


def _task(tmp):
    return Task(name="f.bin", dest=os.path.join(tmp, "sub", "f.bin"), source={"id": "x"}, md5=MD5)


def _md5(p):
    return hashlib.md5(open(p, "rb").read()).hexdigest()


def test_full(server, tmp_path):
    Handler.delay, Handler.fail_every = 0, 0
    t = _task(str(tmp_path))
    dl = SegmentedDownload(t, FakeSource(server), 8, threading.Event())
    assert dl.run() is True
    assert _md5(t.dest) == MD5
    assert not os.path.exists(t.part_path) and not os.path.exists(t.state_path)
    assert len(dl.segs) >= 8


def test_pause_resume(server, tmp_path):
    Handler.delay, Handler.fail_every = 0.05, 0
    t = _task(str(tmp_path))
    stop = threading.Event()
    dl = SegmentedDownload(t, FakeSource(server), 6, stop)
    threading.Timer(0.4, stop.set).start()
    assert dl.run() is False
    part_done = dl.done_bytes()
    assert 0 < part_done < SIZE
    assert os.path.exists(t.state_path)
    Handler.delay = 0
    dl2 = SegmentedDownload(t, FakeSource(server), 6, threading.Event())
    assert dl2.run() is True
    assert dl2.counter <= SIZE - part_done + 6 * (1 << 20)  # 이미 받은 부분은 다시 안 받음
    assert _md5(t.dest) == MD5


def test_transient_errors(server, tmp_path):
    Handler.delay, Handler.fail_every = 0, 4
    t = _task(str(tmp_path))
    dl = SegmentedDownload(t, FakeSource(server), 8, threading.Event())
    assert dl.run() is True
    assert _md5(t.dest) == MD5
    Handler.fail_every = 0


def test_md5_mismatch(server, tmp_path):
    Handler.delay, Handler.fail_every = 0, 0
    t = _task(str(tmp_path))
    t.md5 = "0" * 32
    dl = SegmentedDownload(t, FakeSource(server), 4, threading.Event())
    with pytest.raises(DownloadError):
        dl.run()


class S:
    concurrent_files = 2
    connections_per_file = 4
    verify_md5 = True
    quota_copy_fallback = False


def test_manager(server, tmp_path):
    Handler.delay, Handler.fail_every = 0, 0
    m = DownloadManager(S(), lambda t: FakeSource(server), tmp_path / "q.json")
    tasks = [Task(name=f"{i}.bin", dest=str(tmp_path / f"{i}.bin"), source={"id": str(i)}, md5=MD5)
             for i in range(4)]
    m.add(tasks)
    deadline = time.time() + 60
    while time.time() < deadline and not all(t.status == "done" for t in m.snapshot()):
        time.sleep(0.2)
    assert all(t.status == "done" for t in m.snapshot()), [(t.status, t.error) for t in m.snapshot()]
    # 이미 받은 파일은 다시 추가하면 건너뜀
    m.clear_finished()
    m.add([Task(name="0.bin", dest=str(tmp_path / "0.bin"), source={"id": "0"}, size=SIZE)])
    assert m.snapshot()[0].status == "skipped"
    m.shutdown()
    assert (tmp_path / "q.json").exists()


def test_known_size_skips_probe(server, tmp_path):
    Handler.delay, Handler.fail_every = 0, 0
    t = _task(str(tmp_path))
    t.size = SIZE
    before = Handler.counter
    dl = SegmentedDownload(t, FakeSource(server), 8, threading.Event())
    assert dl.run() is True
    assert _md5(t.dest) == MD5
    assert Handler.counter - before == len(dl.segs)  # 구간마다 요청 1번, 크기 확인 요청 없음


def test_size_changed(server, tmp_path):
    Handler.delay, Handler.fail_every = 0, 0
    t = _task(str(tmp_path))
    t.size = SIZE + 10
    dl = SegmentedDownload(t, FakeSource(server), 4, threading.Event())
    with pytest.raises(DownloadError):
        dl.run()
