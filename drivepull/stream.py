"""영상 미리보기용 로컬 중계 서버.

플레이어(QMediaPlayer/FFmpeg)는 http://127.0.0.1:<port>/v/<token> 을 열고,
이 서버가 Range 요청을 그대로 구글 드라이브 원본으로 넘긴다 (로그인 토큰·공개 링크 처리 포함).
원본을 받으면서 재생하므로 ProRes 등 드라이브 웹 플레이어가 못 여는 코덱도 재생된다.
"""
from __future__ import annotations

import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

import requests

from .engine import DownloadError, TransientError, make_session

RELAY_CHUNK = 256 * 1024
PF_CHUNK = 4 << 20          # 미리 받기 단위 4MB
PF_CONNS = 8                # 시작 연결 수 (구글은 연결당 속도 제한이 있어서 여러 개로 받아야 원본 속도가 나옴)
PF_MAX_CONNS = 32           # 원본이 무거우면 여기까지 늘림
PF_CACHE = 768 << 20        # 메모리 버퍼 기본 768MB
PF_CACHE_MAX = 3 << 30      # 고비트레이트 원본은 최대 3GB (약 10초 분량 목표)
PF_BEHIND = 2               # 재생 위치 뒤로 남겨둘 조각 (짧게 되감기용)
PF_REGION = 8               # 요청 하나로 이어 받을 조각 수 (32MB)


class Prefetcher:
    """재생 위치 앞쪽을 여러 연결로 미리 받아 메모리에 쌓아두고 플레이어에 순서대로 넘긴다."""

    def __init__(self, entry: "_Entry", conns: int = PF_CONNS, cache: int = PF_CACHE):
        self.e = entry
        self.size = entry.size
        self.n_chunks = (self.size + PF_CHUNK - 1) // PF_CHUNK
        self.ahead = max(4, cache // PF_CHUNK)
        self.chunks: dict[int, bytes] = {}
        self.inflight: set[int] = set()
        self.urgent: list[int] = []
        self.want = 0                    # 주 재생 흐름이 지금 읽는 조각
        self.cond = threading.Condition()
        self.stop = threading.Event()
        self.fetched = 0
        self.errors = 0
        self.throttled = 0
        self.byterate = 0.0              # 원본 재생에 필요한 초당 바이트 (플레이어가 알려줌)
        self.rate = 0.0                  # 실제 수신 속도 (바이트/초)
        self.workers: list[threading.Thread] = []
        try:
            src = entry.get_source()
        except Exception:  # noqa: BLE001
            src = None
        # 로그인 없는 공개 링크는 요청이 많으면 구글이 파일을 막아서 연결 수 상한을 둔다
        self.max_conns = min(PF_MAX_CONNS, getattr(src, "max_connections", PF_MAX_CONNS))
        self._add_workers(min(conns, self.max_conns))
        threading.Thread(target=self._monitor, daemon=True).start()

    def _add_workers(self, n: int) -> None:
        for _ in range(n):
            w = threading.Thread(target=self._work, daemon=True)
            w.start()
            self.workers.append(w)

    def set_byterate(self, bps: float) -> None:
        """원본 비트레이트를 알면 버퍼를 10초 분량까지 늘린다."""
        self.byterate = bps
        cache = int(min(PF_CACHE_MAX, max(PF_CACHE, bps * 10)))
        with self.cond:
            self.ahead = max(4, cache // PF_CHUNK)
            self.cond.notify_all()

    def _monitor(self) -> None:
        """2초마다 수신 속도를 재서, 원본 속도를 못 따라가면 연결을 4개씩 늘린다."""
        last_b, last_t = 0, time.monotonic()
        last_err = 0
        while not self.stop.wait(2.0):
            now = time.monotonic()
            self.rate = (self.fetched - last_b) / (now - last_t)
            last_b, last_t = self.fetched, now
            throttled = self.throttled > last_err
            last_err = self.throttled
            need = self.byterate * 1.25
            with self.cond:
                pending = any(i not in self.chunks for i in range(self.want, min(self.n_chunks, self.want + self.ahead)))
            if (need and self.rate < need and pending and not throttled
                    and len(self.workers) < self.max_conns):
                self._add_workers(min(4, self.max_conns - len(self.workers)))

    # ---- 상태
    def buffered_ahead(self) -> int:
        """재생 위치부터 끊김 없이 이어지는 버퍼 크기 (바이트)."""
        with self.cond:
            i, n = self.want, 0
            while i in self.chunks:
                n += len(self.chunks[i])
                i += 1
            return n

    def ahead_of(self, byte_pos: int) -> int:
        """재생 위치(byte_pos)부터 플레이어가 이미 가져간 양 + 끊김 없이 쌓여 있는 버퍼."""
        with self.cond:
            served = self.want * PF_CHUNK
            i, n = self.want, 0
            while i in self.chunks:
                n += len(self.chunks[i])
                i += 1
            if i >= self.n_chunks:
                return self.size - byte_pos   # 끝까지 다 받아둠
            return max(0, served - byte_pos) + n

    def set_want(self, idx: int) -> None:
        with self.cond:
            if idx != self.want:
                self.want = idx
                self._evict()
                self.cond.notify_all()

    # ---- 받기
    def _next_job(self) -> Optional[tuple[int, int]]:
        """(시작 조각, 개수). 요청마다 구글 첫 응답 대기(~1초)가 있어서 한 번에 여러 조각을 이어 받는다."""
        def run_from(i: int) -> tuple[int, int]:
            # 재생 위치 가까운 곳은 짧게 나눠 여러 연결이 동시에 (빨리 시작), 먼 곳은 길게 (요청 대기 줄이기)
            cap = max(1, min(PF_REGION, 1 + (i - self.want) // 2))
            n = 1
            limit = min(self.n_chunks, self.want + self.ahead)
            while n < cap and i + n < limit and (i + n) not in self.chunks and (i + n) not in self.inflight:
                n += 1
            return i, n
        while self.urgent:
            i = self.urgent.pop(0)
            if i not in self.chunks and i not in self.inflight:
                return run_from(i)
        for i in range(self.want, min(self.n_chunks, self.want + self.ahead)):
            if i not in self.chunks and i not in self.inflight:
                return run_from(i)
        return None

    def _wanted(self, idx: int) -> bool:
        return self.want - PF_BEHIND <= idx < self.want + self.ahead or idx >= self.n_chunks - 2

    def _work(self) -> None:
        sess = make_session()
        src = None
        while not self.stop.is_set():
            with self.cond:
                job = self._next_job()
                while job is None and not self.stop.is_set():
                    self.cond.wait(0.5)
                    job = self._next_job()
                if job is None:
                    return
                first, count = job
                pending = list(range(first, first + count))
                self.inflight.update(pending)
            attempt = 0
            while pending and not self.stop.is_set():
                a = pending[0] * PF_CHUNK
                b = min(self.size, (pending[-1] + 1) * PF_CHUNK) - 1
                try:
                    src = src or self.e.get_source()
                    r = src.open(sess, a, b)
                    try:
                        if r.status_code != 206:
                            raise DownloadError("구간 요청 미지원")
                        buf = bytearray()
                        for piece in r.iter_content(256 * 1024):
                            if self.stop.is_set():
                                break
                            buf += piece
                            with self.cond:
                                self.fetched += len(piece)
                            idx = pending[0]
                            need = min(self.size, (idx + 1) * PF_CHUNK) - idx * PF_CHUNK
                            while pending and len(buf) >= need:
                                with self.cond:
                                    self.chunks[idx] = bytes(buf[:need])
                                    self.inflight.discard(idx)
                                    pending.pop(0)
                                    self._evict()
                                    self.cond.notify_all()
                                    # 탐색으로 더 이상 필요 없는 구간이면 이 요청은 그만
                                    if pending and not self._wanted(pending[0]):
                                        for k in pending:
                                            self.inflight.discard(k)
                                        pending = []
                                del buf[:need]
                                attempt = 0
                                if pending:
                                    idx = pending[0]
                                    need = min(self.size, (idx + 1) * PF_CHUNK) - idx * PF_CHUNK
                            if not pending:
                                break
                    finally:
                        r.close()
                    if pending and not self.stop.is_set():
                        raise TransientError("응답이 중간에 끊김")
                except DownloadError as ex:
                    self.e.error = str(ex)
                    break
                except (TransientError, requests.RequestException, OSError) as ex:
                    self.errors += 1
                    if getattr(ex, "throttle", False):
                        self.throttled += 1
                    attempt += 1
                    if attempt > 6:
                        break
                    self.stop.wait(getattr(ex, "wait", None) or min(8.0, 0.5 * 2 ** attempt))
            with self.cond:
                for k in pending:
                    self.inflight.discard(k)
                self.cond.notify_all()

    def _evict(self) -> None:
        lo, hi = self.want - PF_BEHIND, self.want + self.ahead
        for i in [i for i in self.chunks if i < lo or i >= hi]:
            # moov 등 파일 끝 쪽(마지막 2조각)은 자주 다시 읽어서 남겨둔다
            if i >= self.n_chunks - 2:
                continue
            del self.chunks[i]

    def get(self, idx: int, timeout: float = 60.0) -> Optional[bytes]:
        with self.cond:
            if idx not in self.chunks and idx not in self.inflight and idx not in self.urgent:
                self.urgent.append(idx)
                self.cond.notify_all()
            end = time.monotonic() + timeout
            while idx not in self.chunks:
                if self.stop.is_set() or self.e.error:
                    return None
                left = end - time.monotonic()
                if left <= 0:
                    return None
                if idx not in self.inflight and idx not in self.urgent:
                    self.urgent.append(idx)
                    self.cond.notify_all()
                self.cond.wait(min(left, 0.5))
            return self.chunks[idx]

    def close(self) -> None:
        self.stop.set()
        with self.cond:
            self.cond.notify_all()
            self.chunks.clear()


class _Entry:
    def __init__(self, source_factory: Callable[[], object], size: Optional[int], mime: str):
        self.factory = source_factory
        self.source = None
        self.size = size
        self.mime = mime or "application/octet-stream"
        self.lock = threading.RLock()   # prefetcher() 안에서 get_source() 를 다시 부름
        self.bytes_sent = 0
        self.error = ""
        self.pf: Optional[Prefetcher] = None

    def prefetcher(self) -> Optional[Prefetcher]:
        with self.lock:
            if self.pf is None and self.size:
                self.pf = Prefetcher(self)
            return self.pf

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
                pf = e.prefetcher() if e.size else None
                if pf is not None:
                    self._serve_prefetch(e, pf, start, end)
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

            def _serve_prefetch(self, e, pf, start, end):
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{e.size}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Type", e.mime)
                self.send_header("Content-Length", str(end - start + 1))
                self.end_headers()
                pos = start
                seq = 0
                try:
                    while pos <= end:
                        idx = pos // PF_CHUNK
                        seq += 1
                        if seq >= 2:
                            # 연속으로 여러 조각을 읽는 쪽이 실제 재생 흐름 (moov 읽기 같은 짧은 요청은 제외)
                            pf.set_want(idx)
                        data = pf.get(idx)
                        if data is None:
                            break
                        off = pos - idx * PF_CHUNK
                        piece = data[off:off + (end - pos + 1)]
                        mv = memoryview(piece)
                        for k in range(0, len(mv), RELAY_CHUNK):
                            self.wfile.write(mv[k:k + RELAY_CHUNK])
                        e.bytes_sent += len(piece)
                        pos += len(piece)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass   # 플레이어가 탐색하면서 연결을 끊은 것: 정상

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
            e = self.entries.pop(m.group(1), None)
            if e is not None and e.pf is not None:
                e.pf.close()

    def shutdown(self) -> None:
        try:
            self.httpd.shutdown()
        except Exception:  # noqa: BLE001
            pass
