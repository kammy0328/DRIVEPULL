"""엔진 테스트: Range 지원 로컬 서버로 분할 다운로드, 일시정지/이어받기, 오류 재시도를 확인."""
import hashlib
import os
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

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


class QuotaOnce(FakeSource):
    """처음 몇 번은 정상, 그다음 한 번 구글 제한, 이후 정상."""
    def __init__(self, url):
        super().__init__(url)
        self.n = 0
        self.fired = False

    def open(self, sess, start, end):
        self.n += 1
        if self.n == 3 and not self.fired:
            self.fired = True
            from drivepull.engine import QuotaExceeded
            raise QuotaExceeded("구글 다운로드 제한")
        return super().open(sess, start, end)


def test_quota_waits_and_resumes(server, tmp_path, monkeypatch):
    import drivepull.engine as E
    monkeypatch.setattr(E, "LIMIT_WAITS", (0.03,))   # 1.8초
    Handler.delay, Handler.fail_every = 0.02, 0
    src = QuotaOnce(server)
    m = DownloadManager(S(), lambda t: src, tmp_path / "q.json")
    t = Task(name="q.bin", dest=str(tmp_path / "q.bin"), source={"id": "q"}, md5=MD5, size=SIZE)
    m.add([t])
    seen = set()
    deadline = time.time() + 60
    while time.time() < deadline and t.status != "done":
        seen.add(t.status)
        if t.status == "waiting":
            assert t.retry_at > time.time() - 1
        time.sleep(0.1)
    Handler.delay = 0
    assert "waiting" in seen, seen
    assert t.status == "done", (t.status, t.error)
    assert _md5(t.dest) == MD5
    m.shutdown()


def test_verify_frees_slot(server, tmp_path, monkeypatch):
    """MD5 검증 중에도 다음 파일 다운로드가 시작돼야 함."""
    import drivepull.engine as E
    Handler.delay, Handler.fail_every = 0, 0
    gate = threading.Event()
    real = E.md5_file

    def slow_md5(path, progress=None, stop=None):
        gate.wait(20)
        return real(path, progress, stop)

    monkeypatch.setattr(E, "md5_file", slow_md5)

    class One(S):
        concurrent_files = 1

    m = DownloadManager(One(), lambda t: FakeSource(server), tmp_path / "q.json")
    a = Task(name="a.bin", dest=str(tmp_path / "a.bin"), source={"id": "a"}, md5=MD5, size=SIZE)
    b = Task(name="b.bin", dest=str(tmp_path / "b.bin"), source={"id": "b"}, md5=MD5, size=SIZE)
    m.add([a, b])
    deadline = time.time() + 30
    while time.time() < deadline and not (a.status == "verifying" and b.status in ("downloading", "verifying")):
        time.sleep(0.05)
    assert a.status == "verifying" and b.status in ("downloading", "verifying"), (a.status, b.status)
    gate.set()
    while time.time() < deadline and not (a.status == b.status == "done"):
        time.sleep(0.1)
    assert a.status == b.status == "done"
    m.shutdown()


def test_cancel_and_discard(server, tmp_path):
    Handler.delay, Handler.fail_every = 0.05, 0
    m = DownloadManager(S(), lambda t: FakeSource(server), tmp_path / "q.json")
    a = Task(name="a.bin", dest=str(tmp_path / "a.bin"), source={"id": "a"}, size=SIZE)
    b = Task(name="b.bin", dest=str(tmp_path / "b.bin"), source={"id": "b"}, size=SIZE)
    m.add([a, b])
    deadline = time.time() + 20
    while time.time() < deadline and not (a.done_bytes > 0 and b.done_bytes > 0):
        time.sleep(0.1)
    # 받는 도중 취소 → 임시 파일 삭제, 목록엔 '취소됨'
    m.cancel([a.uid])
    while time.time() < deadline and a.status != "canceled":
        time.sleep(0.05)
    assert a.status == "canceled" and a.done_bytes == 0
    assert not os.path.exists(a.part_path) and not os.path.exists(a.state_path)
    # 종료 시 정리: 멈춘 뒤 미완료 작업의 임시 파일 삭제, 목록에서 제거
    m.shutdown()
    assert b.status == "paused" and os.path.exists(b.part_path)
    assert m.partial_tasks() == [b]
    assert m.discard_unfinished() == 1
    assert not os.path.exists(b.part_path) and not os.path.exists(b.state_path)
    assert [t.status for t in m.snapshot()] == ["canceled"]
    Handler.delay = 0
    # 취소된 건 다시 받으면 처음부터
    m2 = DownloadManager(S(), lambda t: FakeSource(server), tmp_path / "q2.json")
    m2.add([a])
    m2.resume([a.uid])
    while time.time() < deadline + 20 and a.status != "done":
        time.sleep(0.1)
    assert a.status == "done"
    m2.shutdown()
