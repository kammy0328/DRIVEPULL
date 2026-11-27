"""자동 연결 조절 테스트: 연결당 속도 제한 + 전체 회선 제한이 있는 가짜 서버."""
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import drivepull.engine as E
from drivepull.engine import ProbeResult, SegmentedDownload, Task, TransientError

BLOCK = bytes(range(256)) * 4096          # 1 MiB 반복 패턴
SIZE = 360 * 1024 * 1024
PER_CONN = 1 * 1024 * 1024               # 연결당 1 MB/s
TOTAL = 20 * 1024 * 1024                 # 회선 전체 20 MB/s → 20연결이 최적


class Bucket:
    def __init__(self, rate):
        self.rate, self.tokens, self.t, self.lock = rate, 0.0, time.monotonic(), threading.Lock()

    def take(self, n):
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.rate * 0.2, self.tokens + (now - self.t) * self.rate)
                self.t = now
                if self.tokens >= n:
                    self.tokens -= n
                    return
                need = (n - self.tokens) / self.rate
            time.sleep(need)


class H(BaseHTTPRequestHandler):
    bucket = None
    limit_conns = 0
    active = 0
    lock = threading.Lock()

    def log_message(self, *a):
        pass

    def do_GET(self):
        cls = type(self)
        with cls.lock:
            cls.active += 1
            over = cls.limit_conns and cls.active > cls.limit_conns
        try:
            if over:
                self.send_response(429)
                self.end_headers()
                return
            a, b = self.headers["Range"].split("=")[1].split("-")
            start, end = int(a), (int(b) if b else SIZE - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{SIZE}")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            pos, conn_t = start, time.monotonic()
            while pos <= end:
                n = min(64 * 1024, end - pos + 1)
                cls.bucket.take(n)
                off = pos % len(BLOCK)
                chunk = (BLOCK[off:] + BLOCK[:off])[:n]
                self.wfile.write(chunk)
                pos += n
                # 연결당 속도 제한
                ahead = (pos - start) / PER_CONN - (time.monotonic() - conn_t)
                if ahead > 0:
                    time.sleep(ahead)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with cls.lock:
                cls.active -= 1


@pytest.fixture()
def srv():
    H.bucket = Bucket(TOTAL)
    H.limit_conns = 0
    s = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_address[1]}/"
    s.shutdown()


class Src:
    def __init__(self, url):
        self.url = url

    def probe(self, sess):
        return ProbeResult(SIZE, True)

    def open(self, sess, start, end):
        h = {"Range": f"bytes={start}-{'' if end is None else end}"}
        r = sess.get(self.url, headers=h, stream=True, timeout=10)
        if r.status_code == 429:
            r.close()
            raise TransientError("429", wait=0.3, throttle=True)
        return r


def _run(url, tmp_path, maxc, seconds):
    E.TUNE_WINDOW = 1.0
    E.TUNE_SETTLE = 0.6
    t = Task(name="big.mov", dest=str(tmp_path / "big.mov"), source={"id": "x"}, size=SIZE)
    stop = threading.Event()
    dl = SegmentedDownload(t, Src(url), maxc, stop, auto=True)
    th = threading.Thread(target=dl.run, daemon=True)
    th.start()
    hist = []
    for _ in range(seconds * 2):
        time.sleep(0.5)
        hist.append(dl.target)
    stop.set()
    th.join(15)
    return dl, hist


def test_converges(srv, tmp_path):
    dl, hist = _run(srv, tmp_path, 32, 20)
    print("targets", hist, "best", dl.best_target)
    assert hist[0] == 8
    # 회선 한도(20 MB/s)의 85% 이상까지 올라가야 함
    assert dl._best_rate >= 0.85 * TOTAL
    assert dl.best_target >= 16


def test_throttle_backs_off(srv, tmp_path):
    H.limit_conns = 10
    H.bucket = Bucket(200 * 1024 * 1024)   # 회선은 넉넉, 대신 7연결부터 429
    dl, hist = _run(srv, tmp_path, 32, 12)
    print("targets", hist)
    assert hist[-1] <= H.limit_conns   # 429 를 주는 개수 아래로 내려와 있어야 함
