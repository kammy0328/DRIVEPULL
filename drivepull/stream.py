"""영상 미리보기용 로컬 중계 서버.

플레이어(QMediaPlayer/FFmpeg)는 http://127.0.0.1:<port>/v/<token> 을 열고,
이 서버가 Range 요청을 그대로 구글 드라이브 원본으로 넘긴다 (로그인 토큰·공개 링크 처리 포함).
원본을 받으면서 재생하므로 ProRes 등 드라이브 웹 플레이어가 못 여는 코덱도 재생된다.
"""
from __future__ import annotations

import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

import requests

from .engine import DownloadError, TransientError, make_session

RELAY_CHUNK = 256 * 1024


class _Entry:
    def __init__(self, source_factory: Callable[[], object], size: Optional[int], mime: str):
        self.factory = source_factory
        self.source = None
        self.size = size
        self.mime = mime or "application/octet-stream"
        self.lock = threading.Lock()
        self.bytes_sent = 0
        self.error = ""

    def get_source(self):
        with self.lock:
            if self.source is None:
                self.source = self.factory()
            return self.source


class StreamServer:
    def __init__(self) -> None:
        self.entries: dict[str, _Entry] = {}
        self._local = threading.local()
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_HEAD(self):
                self._serve(head=True)

            def do_GET(self):
                self._serve(head=False)

            def _serve(self, head: bool):
                m = re.match(r"^/v/([A-Za-z0-9_-]+)", self.path)
                e = server.entries.get(m.group(1)) if m else None
                if e is None:
                    self.send_error(404)
                    return
                rng = self.headers.get("Range", "")
                mr = re.match(r"bytes=(\d*)-(\d*)", rng)
                start = int(mr.group(1)) if mr and mr.group(1) else 0
                end = int(mr.group(2)) if mr and mr.group(2) else None
                if mr and not mr.group(1) and mr.group(2) and e.size:
                    # bytes=-N (끝에서 N바이트): MOV moov 가 파일 끝에 있을 때
                    start, end = max(0, e.size - int(mr.group(2))), e.size - 1
                if e.size is not None:
                    if start >= e.size:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{e.size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    end = e.size - 1 if end is None else min(end, e.size - 1)
                if head:
                    self.send_response(200)
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Type", e.mime)
                    if e.size is not None:
                        self.send_header("Content-Length", str(e.size))
                    self.end_headers()
                    return
                sess = server._session()
                resp = None
                try:
                    src = e.get_source()
                    for attempt in range(4):
                        try:
                            resp = src.open(sess, start, end)
                            break
                        except TransientError as te:
                            if attempt == 3:
                                raise
                            threading.Event().wait(te.wait or 1.0 + attempt)
                    if resp.status_code == 206:
                        cr = resp.headers.get("Content-Range", "")
                        mt = re.match(r"bytes (\d+)-(\d+)/(\d+)", cr)
                        if mt and e.size is None:
                            e.size = int(mt.group(3))
                        a, b = (int(mt.group(1)), int(mt.group(2))) if mt else (start, end)
                        self.send_response(206)
                        self.send_header("Content-Range", cr or f"bytes {a}-{b}/{e.size or '*'}")
                        length = b - a + 1
                    else:
                        self.send_response(200)
                        cl = resp.headers.get("Content-Length")
                        length = int(cl) if cl and cl.isdigit() else None
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Type", e.mime)
                    if length is not None:
                        self.send_header("Content-Length", str(length))
                    else:
                        self.send_header("Connection", "close")
                    self.end_headers()
                    for chunk in resp.iter_content(RELAY_CHUNK):
                        if not chunk:
                            continue
                        self.wfile.write(chunk)
                        e.bytes_sent += len(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass   # 플레이어가 탐색하면서 연결을 끊은 것: 정상
                except (DownloadError, TransientError, requests.RequestException, OSError) as ex:
                    e.error = str(ex) or ex.__class__.__name__
                    try:
                        self.send_error(502, "upstream error")
                    except OSError:
                        pass
                finally:
                    if resp is not None:
                        resp.close()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def _session(self) -> requests.Session:
        s = getattr(self._local, "s", None)
        if s is None:
            s = self._local.s = make_session()
        return s

    def register(self, source_factory: Callable[[], object], size: Optional[int], mime: str = "") -> str:
        token = secrets.token_urlsafe(12)
        self.entries[token] = _Entry(source_factory, size, mime)
        return f"http://127.0.0.1:{self.port}/v/{token}"

    def entry_for(self, url: str) -> Optional[_Entry]:
        m = re.search(r"/v/([A-Za-z0-9_-]+)", url)
        return self.entries.get(m.group(1)) if m else None

    def release(self, url: str) -> None:
        m = re.search(r"/v/([A-Za-z0-9_-]+)", url)
        if m:
            self.entries.pop(m.group(1), None)

    def shutdown(self) -> None:
        try:
            self.httpd.shutdown()
        except Exception:  # noqa: BLE001
            pass
