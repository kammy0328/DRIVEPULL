"""영상 미리보기: 로컬 중계 서버 + Qt(FFmpeg) 플레이어로 ProRes 원본을 받으면서 재생."""
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtMultimedia = pytest.importorskip("PySide6.QtMultimedia")

from drivepull.engine import TransientError  # noqa: E402
from drivepull.stream import StreamServer  # noqa: E402

SAMPLE = os.path.join(os.path.dirname(__file__), "data", "prores4444_moov_end.mov")
DATA = open(SAMPLE, "rb").read()


class Up(BaseHTTPRequestHandler):
    """구글 드라이브 대신 Range 를 지원하는 원본 서버."""
    protocol_version = "HTTP/1.1"
    ranges = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        a, b = self.headers["Range"].split("=")[1].split("-")
        s, e = int(a), (int(b) if b else len(DATA) - 1)
        type(self).ranges.append((s, e))
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {s}-{e}/{len(DATA)}")
        self.send_header("Content-Length", str(e - s + 1))
        self.end_headers()
        try:
            self.wfile.write(DATA[s:e + 1])
        except OSError:
            pass


class Src:
    def __init__(self, url):
        self.url = url

    def open(self, sess, start, end):
        r = sess.get(self.url, headers={"Range": f"bytes={start}-{'' if end is None else end}"},
                     stream=True, timeout=10)
        if r.status_code >= 500:
            raise TransientError("5xx")
        return r


@pytest.fixture(scope="module")
def upstream():
    s = ThreadingHTTPServer(("127.0.0.1", 0), Up)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_address[1]}/"
    s.shutdown()


def test_relay_ranges(upstream):
    import requests
    srv = StreamServer()
    url = srv.register(lambda: Src(upstream), len(DATA), "video/quicktime")
    r = requests.get(url, headers={"Range": "bytes=100-199"}, timeout=10)
    assert r.status_code == 206 and r.content == DATA[100:200]
    r = requests.get(url, headers={"Range": "bytes=-50"}, timeout=10)   # 끝에서 50바이트
    assert r.status_code == 206 and r.content == DATA[-50:]
    r = requests.get(url, headers={"Range": f"bytes={len(DATA)}-"}, timeout=10)
    assert r.status_code == 416
    srv.shutdown()


def test_player_plays_and_seeks_prores(upstream):
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtMultimedia import QMediaPlayer, QVideoSink
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    srv = StreamServer()
    url = srv.register(lambda: Src(upstream), len(DATA), "video/quicktime")
    p = QMediaPlayer()
    sink = QVideoSink()
    p.setVideoSink(sink)
    frames = []
    sink.videoFrameChanged.connect(lambda f: frames.append(f.startTime()))
    errors = []
    p.errorOccurred.connect(lambda e, s: errors.append(s))
    p.setSource(QUrl(url))
    p.play()
    state = {}

    def seek():
        state["before"] = len(frames)
        p.setPosition(1500)

    def done():
        state["dur"] = p.duration()
        state["pos"] = p.position()
        app.quit()

    QTimer.singleShot(1200, seek)
    QTimer.singleShot(2600, done)
    app.exec()
    p.stop()
    srv.shutdown()
    assert not errors, errors
    assert 1900 <= state["dur"] <= 2100
    assert state["before"] > 5                     # 재생되며 프레임이 나옴
    assert len(frames) > state["before"]           # 탐색 후에도 계속 나옴
    assert state["pos"] >= 1500


class SlowUp(Up):
    """연결당 0.4MB/s, 요청마다 첫 응답 0.2초 (구글처럼 연결당 속도 제한)."""
    def do_GET(self):
        a, b = self.headers["Range"].split("=")[1].split("-")
        s, e = int(a), (int(b) if b else len(DATA) - 1)
        time.sleep(0.2)
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {s}-{e}/{len(DATA)}")
        self.send_header("Content-Length", str(e - s + 1))
        self.end_headers()
        pos, t0 = s, time.monotonic()
        try:
            while pos <= e:
                n = min(32 * 1024, e - pos + 1)
                self.wfile.write(DATA[pos:pos + n])
                pos += n
                ahead = (pos - s) / 400_000 - (time.monotonic() - t0)
                if ahead > 0:
                    time.sleep(ahead)
        except OSError:
            pass


def test_prefetch_uses_parallel_connections(monkeypatch):
    """연결 하나면 1.3MB / 0.4MB/s ≈ 3.4초. 여러 연결로 미리 받으면 훨씬 빨라야 함."""
    import requests

    import drivepull.stream as S
    monkeypatch.setattr(S, "PF_CHUNK", 64 * 1024)
    s = ThreadingHTTPServer(("127.0.0.1", 0), SlowUp)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    srv = StreamServer()
    url = srv.register(lambda: Src(f"http://127.0.0.1:{s.server_address[1]}/"), len(DATA), "video/quicktime")
    t0 = time.monotonic()
    r = requests.get(url, headers={"Range": "bytes=0-"}, timeout=30)
    took = time.monotonic() - t0
    srv.shutdown()
    s.shutdown()
    assert r.content == DATA
    assert took < 2.0, f"{took:.1f}초"


class BigUp(BaseHTTPRequestHandler):
    """메모리에 안 담는 큰 가상 파일 (바이트 i = i % 251). 40GB 원본처럼 버퍼보다 훨씬 큰 파일 흉내."""
    protocol_version = "HTTP/1.1"
    SIZE = 400 * 1024 * 1024

    def log_message(self, *a):
        pass

    def do_GET(self):
        a, b = self.headers["Range"].split("=")[1].split("-")
        s, e = int(a), (int(b) if b else self.SIZE - 1)
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {s}-{e}/{self.SIZE}")
        self.send_header("Content-Length", str(e - s + 1))
        self.end_headers()
        pos = s
        try:
            while pos <= e:
                n = min(256 * 1024, e - pos + 1)
                self.wfile.write(bytes((pos + k) % 251 for k in range(n)))
                pos += n
        except OSError:
            pass


def _pattern(start, n):
    return bytes((start + k) % 251 for k in range(n))


def test_far_reads_on_big_file(monkeypatch):
    """버퍼 범위 밖(파일 끝의 moov, 멀리 탐색)을 읽어도 멈추지 않아야 함 (v0.6.1 재생 안 됨 버그)."""
    import requests

    import drivepull.stream as S
    monkeypatch.setattr(S, "PF_CHUNK", 256 * 1024)
    monkeypatch.setattr(S, "PF_CACHE", 2 * 1024 * 1024)       # 버퍼 8조각
    s = ThreadingHTTPServer(("127.0.0.1", 0), BigUp)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    srv = StreamServer()
    size = BigUp.SIZE
    url = srv.register(lambda: Src(f"http://127.0.0.1:{s.server_address[1]}/"), size, "video/quicktime")
    # 1) 처음부터 재생 흐름
    r = requests.get(url, headers={"Range": "bytes=0-1048575"}, timeout=10)
    assert r.content == _pattern(0, 1048576)
    # 2) 파일 끝 쪽 moov 읽기 (끝에서 3MB 지점부터 1MB: 마지막 2조각보다 앞)
    a = size - 3 * 1024 * 1024
    r = requests.get(url, headers={"Range": f"bytes={a}-{a + 1048575}"}, timeout=10)
    assert r.content == _pattern(a, 1048576)
    # 3) 파일 중간으로 탐색 (버퍼 범위 훨씬 밖)
    a = size // 2 + 12345
    r = requests.get(url, headers={"Range": f"bytes={a}-{a + 2 * 1048576}"}, timeout=10)
    assert r.content == _pattern(a, 2 * 1048576 + 1)
    srv.shutdown()
    s.shutdown()
