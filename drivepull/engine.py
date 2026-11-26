"""멀티 커넥션 분할 다운로드 엔진.

- 파일 하나를 여러 구간으로 나눠 동시에 받는다 (HTTP Range).
- 먼저 끝난 연결은 남은 구간이 가장 큰 연결의 뒤쪽 절반을 가져가서 끝까지 속도를 유지한다.
- 구간 진행 상태를 <파일>.drivepull.json 에 주기적으로 저장해서 일시정지, 앱 종료, 네트워크 끊김 후에도 이어받는다.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import requests
from requests.adapters import HTTPAdapter

CHUNK = 1 << 20            # 1 MiB 단위로 읽기
MIN_SPLIT = 4 << 20        # 구간을 나눌 최소 크기
MAX_RETRY = 10
STATE_SAVE_INTERVAL = 1.0
PART_SUFFIX = ".drivepull"


# ---------------------------------------------------------------- errors
class DownloadError(Exception):
    """재시도해도 해결되지 않는 오류."""


class QuotaExceeded(DownloadError):
    """구글의 다운로드 할당량 초과."""


class TransientError(Exception):
    """재시도할 만한 일시적 오류 (429, 5xx 등)."""

    def __init__(self, msg: str, wait: float | None = None):
        super().__init__(msg)
        self.wait = wait


@dataclass
class ProbeResult:
    size: Optional[int]
    supports_range: bool
    filename: Optional[str] = None


# ---------------------------------------------------------------- utils
def fs_path(p: str) -> str:
    """Windows 260자 경로 제한 우회."""
    if sys.platform == "win32":
        ap = os.path.abspath(p)
        if len(ap) >= 240 and not ap.startswith("\\\\?\\"):
            if ap.startswith("\\\\"):
                return "\\\\?\\UNC\\" + ap[2:]
            return "\\\\?\\" + ap
        return ap
    return p


def _set_sparse(fh, on: bool = True) -> bool:
    """NTFS에서 큰 파일을 미리 잡을 때 0으로 채우는 지연을 없앤다 (Windows 전용)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes
        handle = msvcrt.get_osfhandle(fh.fileno())
        FSCTL_SET_SPARSE = 0x000900C4
        flag = ctypes.c_ubyte(1 if on else 0)
        ret = wintypes.DWORD(0)
        ok = ctypes.windll.kernel32.DeviceIoControl(
            wintypes.HANDLE(handle), FSCTL_SET_SPARSE, ctypes.byref(flag), 1, None, 0,
            ctypes.byref(ret), None)
        return bool(ok)
    except Exception:  # noqa: BLE001
        return False


def make_session() -> requests.Session:
    s = requests.Session()
    ad = HTTPAdapter(pool_connections=2, pool_maxsize=4, max_retries=0)
    s.mount("https://", ad)
    s.mount("http://", ad)
    s.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DRIVEPULL"
    return s


def unique_path(path: str) -> str:
    if not os.path.exists(fs_path(path)):
        return path
    root, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(fs_path(f"{root} ({i}){ext}")):
        i += 1
    return f"{root} ({i}){ext}"


def md5_file(path: str, progress: Callable[[int], None] | None = None,
             stop: threading.Event | None = None) -> Optional[str]:
    h = hashlib.md5()
    done = 0
    with open(fs_path(path), "rb") as f:
        while True:
            if stop is not None and stop.is_set():
                return None
            b = f.read(8 << 20)
            if not b:
                break
            h.update(b)
            done += len(b)
            if progress:
                progress(done)
    return h.hexdigest()


# ---------------------------------------------------------------- task
STATUS_LABEL = {
    "queued": "대기",
    "starting": "연결 중",
    "downloading": "다운로드 중",
    "paused": "일시정지",
    "verifying": "검증 중",
    "done": "완료",
    "skipped": "이미 있음",
    "error": "오류",
}


@dataclass
class Task:
    name: str
    dest: str
    source: dict
    size: Optional[int] = None
    md5: Optional[str] = None
    uid: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = "queued"
    done_bytes: int = 0
    error: str = ""
    note: str = ""
    # 런타임 전용
    speed: float = 0.0
    connections: int = 0

    PERSIST = ("name", "dest", "source", "size", "md5", "uid", "status",
               "done_bytes", "error", "note")

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.PERSIST}

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        t = cls(**{k: d[k] for k in cls.PERSIST if k in d})
        if t.status in ("downloading", "starting", "verifying"):
            t.status = "paused"
        return t

    @property
    def part_path(self) -> str:
        return self.dest + PART_SUFFIX

    @property
    def state_path(self) -> str:
        return self.dest + PART_SUFFIX + ".json"


# ---------------------------------------------------------------- segmented download
class _Seg:
    __slots__ = ("start", "end", "pos", "active")

    def __init__(self, start: int, end: int, pos: int | None = None):
        self.start = start
        self.end = end          # inclusive
        self.pos = start if pos is None else pos
        self.active = False

    @property
    def remaining(self) -> int:
        return max(0, self.end - self.pos + 1)


class SegmentedDownload:
    def __init__(self, task: Task, source, connections: int, stop: threading.Event,
                 status_cb: Callable[[str], None] | None = None):
        self.task = task
        self.source = source
        self.connections = max(1, connections)
        self.stop = stop
        self.status_cb = status_cb or (lambda s: None)
        self.lock = threading.Lock()
        self.segs: list[_Seg] = []
        self.size: Optional[int] = None
        self.counter = 0            # 이번 세션에서 받은 바이트 (속도 계산용)
        self.fatal: Optional[BaseException] = None
        self.active_workers = 0
        self._single_done = 0
        self.skipped = False

    # ---- 진행률
    def done_bytes(self) -> int:
        if self.size is None or not self.segs:
            return self._single_done
        with self.lock:
            return sum(min(s.pos, s.end + 1) - s.start for s in self.segs)

    # ---- 상태 파일
    def _load_state(self, size: int) -> bool:
        try:
            with open(fs_path(self.task.state_path), "r", encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError):
            return False
        if st.get("size") != size or st.get("id") != self.task.source.get("id"):
            return False
        if not os.path.exists(fs_path(self.task.part_path)):
            return False
        if os.path.getsize(fs_path(self.task.part_path)) != size:
            return False
        self.segs = [_Seg(a, b, p) for a, b, p in st["segments"]]
        return True

    def _save_state(self) -> None:
        with self.lock:
            segs = [[s.start, s.end, min(s.pos, s.end + 1)] for s in self.segs]
        st = {"id": self.task.source.get("id"), "size": self.size, "segments": segs}
        tmp = self.task.state_path + ".tmp"
        try:
            with open(fs_path(tmp), "w", encoding="utf-8") as f:
                json.dump(st, f)
            os.replace(fs_path(tmp), fs_path(self.task.state_path))
        except OSError:
            pass

    def _clear_state(self) -> None:
        for p in (self.task.state_path, self.task.state_path + ".tmp"):
            try:
                os.remove(fs_path(p))
            except OSError:
                pass

    # ---- 구간 배분
    def _acquire(self) -> Optional[_Seg]:
        with self.lock:
            for s in self.segs:
                if not s.active and s.remaining > 0:
                    s.active = True
                    return s
            # 남은 게 가장 큰 활성 구간을 반으로 쪼개서 가져온다
            best = max((s for s in self.segs if s.active), key=lambda s: s.remaining, default=None)
            if best is None or best.remaining < 2 * MIN_SPLIT:
                return None
            mid = best.pos + best.remaining // 2
            new = _Seg(mid, best.end)
            best.end = mid - 1
            new.active = True
            self.segs.append(new)
            return new

    def _halted(self) -> bool:
        return self.stop.is_set()

    def _fail(self, e: BaseException) -> None:
        if self.fatal is None:
            self.fatal = e
        self.stop.set()

    def _fetch(self, seg: _Seg, sess: requests.Session, fh) -> None:
        attempt = 0
        try:
            while seg.remaining > 0 and not self._halted():
                try:
                    resp = self.source.open(sess, seg.pos, seg.end)
                    try:
                        if resp.status_code != 206:
                            raise DownloadError("서버가 구간 다운로드를 지원하지 않습니다")
                        cr = resp.headers.get("Content-Range", "")
                        if not cr.startswith(f"bytes {seg.pos}-"):
                            raise TransientError(f"잘못된 Content-Range: {cr}")
                        for chunk in resp.iter_content(CHUNK):
                            if self._halted():
                                break
                            if not chunk:
                                continue
                            with self.lock:
                                allowed = seg.end - seg.pos + 1
                                pos = seg.pos
                            if allowed <= 0:
                                break
                            data = chunk[:allowed]
                            fh.seek(pos)
                            fh.write(data)
                            with self.lock:
                                seg.pos = min(seg.pos + len(data), seg.end + 1) if seg.pos == pos else seg.pos
                                self.counter += len(data)
                            attempt = 0
                    finally:
                        resp.close()
                except DownloadError as e:
                    self._fail(e)
                    return
                except (TransientError, requests.RequestException, OSError) as e:
                    if isinstance(e, OSError) and not isinstance(e, requests.RequestException):
                        # 디스크 오류는 재시도 의미 없음
                        if getattr(e, "errno", None) in (28,):  # ENOSPC
                            self._fail(DownloadError("디스크 공간이 부족합니다"))
                            return
                    attempt += 1
                    if attempt > MAX_RETRY:
                        self._fail(DownloadError(f"재시도 한도 초과: {e}"))
                        return
                    wait = getattr(e, "wait", None) or min(30.0, 1.5 ** attempt) + random.random()
                    self.stop.wait(wait)
        finally:
            with self.lock:
                seg.active = False

    def _worker(self) -> None:
        sess = make_session()
        try:
            fh = open(fs_path(self.task.part_path), "r+b", buffering=0)
        except OSError as e:
            self._fail(DownloadError(f"파일을 열 수 없습니다: {e}"))
            return
        with self.lock:
            self.active_workers += 1
        try:
            while not self._halted():
                seg = self._acquire()
                if seg is None:
                    return
                self._fetch(seg, sess, fh)
        except BaseException as e:  # noqa: BLE001
            self._fail(DownloadError(f"내부 오류: {e!r}"))
        finally:
            with self.lock:
                self.active_workers -= 1
            fh.close()
            sess.close()

    # ---- 단일 스트림 (크기를 모르거나 Range 미지원)
    def _run_single(self) -> bool:
        sess = make_session()
        self._single_done = 0
        attempt = 0
        while True:
            try:
                resp = self.source.open(sess, None, None)
                try:
                    with open(fs_path(self.task.part_path), "wb") as fh:
                        for chunk in resp.iter_content(CHUNK):
                            if self._halted():
                                return False
                            fh.write(chunk)
                            self._single_done += len(chunk)
                            self.counter += len(chunk)
                finally:
                    resp.close()
                self.size = self._single_done
                return True
            except DownloadError:
                raise
            except (TransientError, requests.RequestException) as e:
                attempt += 1
                if attempt > MAX_RETRY:
                    raise DownloadError(f"재시도 한도 초과: {e}")
                self._single_done = 0
                if self.stop.wait(min(30.0, 1.5 ** attempt)):
                    return False

    # ---- 실행
    def run(self) -> bool:
        """완료하면 True, 일시정지로 멈추면 False, 실패하면 예외."""
        t = self.task
        os.makedirs(fs_path(os.path.dirname(t.dest) or "."), exist_ok=True)

        sess = make_session()
        attempt = 0
        while True:
            try:
                probe: ProbeResult = self.source.probe(sess)
                break
            except DownloadError:
                raise
            except (TransientError, requests.RequestException) as e:
                attempt += 1
                if attempt > 5:
                    raise DownloadError(f"연결 실패: {e}")
                if self.stop.wait(min(20.0, 2 ** attempt)):
                    return False
        sess.close()

        if probe.size is not None:
            t.size = probe.size
        self.size = probe.size

        # 같은 파일이 이미 받아져 있으면 건너뛴다
        if probe.size is not None and os.path.exists(fs_path(t.dest)) \
                and not os.path.exists(fs_path(t.part_path)) \
                and os.path.getsize(fs_path(t.dest)) == probe.size:
            self.skipped = True
            self._single_done = probe.size
            return True

        if probe.size is None or not probe.supports_range:
            t.note = "단일 연결 (서버가 구간 다운로드를 지원하지 않음)"
            self.status_cb("downloading")
            if not self._run_single():
                return False
            return self._finish()

        size = probe.size
        if size == 0:
            open(fs_path(t.part_path), "wb").close()
            return self._finish()

        if not self._load_state(size):
            self._clear_state()
            with open(fs_path(t.part_path), "wb") as f:
                _set_sparse(f, True)
                f.truncate(size)
            n = max(1, min(self.connections, size // MIN_SPLIT or 1))
            step = size // n
            self.segs = []
            for i in range(n):
                a = i * step
                b = size - 1 if i == n - 1 else (i + 1) * step - 1
                self.segs.append(_Seg(a, b))
            self._save_state()

        if self.done_bytes() >= size:
            return self._finish()

        self.status_cb("downloading")
        workers = [threading.Thread(target=self._worker, daemon=True)
                   for _ in range(self.connections)]
        for w in workers:
            w.start()
        while any(w.is_alive() for w in workers):
            for w in workers:
                w.join(STATE_SAVE_INTERVAL / len(workers))
            self._save_state()
        self._save_state()

        if self.fatal is not None:
            raise self.fatal
        if self.done_bytes() < size:
            return False  # 일시정지
        return self._finish()

    def _finish(self) -> bool:
        t = self.task
        if t.md5 and t.source.get("verify", True):
            self.status_cb("verifying")
            vstop = self.stop
            got = md5_file(t.part_path, stop=vstop)
            if got is None:
                return False
            if got.lower() != t.md5.lower():
                for p in (t.part_path,):
                    try:
                        os.remove(fs_path(p))
                    except OSError:
                        pass
                self._clear_state()
                raise DownloadError("MD5 불일치: 파일이 손상되어 처음부터 다시 받아야 합니다")
        if sys.platform == "win32":
            try:
                with open(fs_path(t.part_path), "r+b") as f:
                    _set_sparse(f, False)
            except OSError:
                pass
        dest = unique_path(t.dest)
        os.replace(fs_path(t.part_path), fs_path(dest))
        if dest != t.dest:
            t.note = f"같은 이름이 있어 {os.path.basename(dest)} 로 저장"
            t.dest = dest
        self._clear_state()
        return True


# ---------------------------------------------------------------- manager
class DownloadManager:
    """다운로드 큐. UI는 tasks 를 주기적으로 읽어서 화면을 갱신한다."""

    def __init__(self, settings, source_factory: Callable[[Task], object],
                 queue_path: Path | None = None,
                 quota_handler: Callable[[Task, object], object] | None = None):
        self.settings = settings
        self.source_factory = source_factory
        self.quota_handler = quota_handler
        self.queue_path = queue_path
        self.tasks: list[Task] = []
        self.lock = threading.RLock()
        self._running: dict[str, tuple[threading.Thread, threading.Event, list]] = {}
        self._want: dict[str, str] = {}
        self._quit = threading.Event()
        self._dirty = False
        self._sched = threading.Thread(target=self._loop, daemon=True)
        self._sched.start()

    # ---- 큐 조작
    def add(self, tasks: list[Task]) -> None:
        with self.lock:
            existing = {t.dest for t in self.tasks if t.status not in ("done", "skipped")}
            for t in tasks:
                if t.dest in existing:
                    continue
                if os.path.exists(fs_path(t.dest)) and t.size is not None \
                        and os.path.getsize(fs_path(t.dest)) == t.size:
                    t.status = "skipped"
                    t.done_bytes = t.size
                self.tasks.append(t)
            self._dirty = True

    def get(self, uid: str) -> Optional[Task]:
        with self.lock:
            return next((t for t in self.tasks if t.uid == uid), None)

    def pause(self, uids) -> None:
        with self.lock:
            for uid in uids:
                t = self.get(uid)
                if not t:
                    continue
                if uid in self._running:
                    self._want[uid] = "pause"
                    self._running[uid][1].set()
                elif t.status == "queued":
                    t.status = "paused"
            self._dirty = True

    def resume(self, uids) -> None:
        with self.lock:
            for uid in uids:
                t = self.get(uid)
                if t and t.status in ("paused", "error"):
                    t.status = "queued"
                    t.error = ""
            self._dirty = True

    def retry(self, uids) -> None:
        self.resume(uids)

    def remove(self, uids, delete_partial: bool = True) -> None:
        uids = list(uids)
        with self.lock:
            for uid in uids:
                if uid in self._running:
                    self._want[uid] = "remove"
                    self._running[uid][1].set()
                else:
                    t = self.get(uid)
                    if t:
                        if delete_partial and t.status not in ("done", "skipped"):
                            self._delete_partial(t)
                        self.tasks.remove(t)
            self._dirty = True

    def clear_finished(self) -> None:
        with self.lock:
            self.tasks = [t for t in self.tasks if t.status not in ("done", "skipped")]
            self._dirty = True

    def pause_all(self) -> None:
        with self.lock:
            self.pause([t.uid for t in self.tasks if t.status in ("queued", "downloading", "starting", "verifying")])

    def resume_all(self) -> None:
        with self.lock:
            self.resume([t.uid for t in self.tasks if t.status in ("paused", "error")])

    def snapshot(self) -> list[Task]:
        with self.lock:
            return list(self.tasks)

    def total_speed(self) -> float:
        with self.lock:
            return sum(t.speed for t in self.tasks if t.uid in self._running)

    def has_active(self) -> bool:
        with self.lock:
            return bool(self._running) or any(t.status == "queued" for t in self.tasks)

    @staticmethod
    def _delete_partial(t: Task) -> None:
        for p in (t.part_path, t.state_path, t.state_path + ".tmp"):
            try:
                os.remove(fs_path(p))
            except OSError:
                pass

    # ---- 저장/복원
    def save_queue(self) -> None:
        if not self.queue_path:
            return
        with self.lock:
            data = [t.to_dict() for t in self.tasks]
            self._dirty = False
        tmp = str(self.queue_path) + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self.queue_path)
        except OSError:
            pass

    def load_queue(self) -> None:
        if not self.queue_path or not os.path.exists(self.queue_path):
            return
        try:
            with open(self.queue_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        with self.lock:
            for d in data:
                try:
                    t = Task.from_dict(d)
                except TypeError:
                    continue
                if t.status == "queued":
                    t.status = "paused"
                self.tasks.append(t)

    # ---- 스케줄러
    def _loop(self) -> None:
        last_stat = time.monotonic()
        last_save = time.monotonic()
        while not self._quit.wait(0.25):
            now = time.monotonic()
            with self.lock:
                limit = max(1, int(self.settings.concurrent_files))
                if len(self._running) < limit:
                    for t in self.tasks:
                        if len(self._running) >= limit:
                            break
                        if t.status == "queued" and t.uid not in self._running:
                            self._start(t)
                dt = now - last_stat
                if dt >= 0.5:
                    for uid, (_, _, holder) in self._running.items():
                        t = self.get(uid)
                        dl: SegmentedDownload | None = holder[0] if holder else None
                        if not t or dl is None:
                            continue
                        c = dl.counter
                        prev = holder[1]
                        holder[1] = c
                        inst = (c - prev) / dt
                        t.speed = inst if t.speed == 0 else t.speed * 0.6 + inst * 0.4
                        t.done_bytes = dl.done_bytes()
                        t.connections = dl.active_workers
                    last_stat = now
            if self._dirty or now - last_save > 5:
                self.save_queue()
                last_save = now

    def _start(self, t: Task) -> None:
        stop = threading.Event()
        holder: list = [None, 0]
        t.status = "starting"
        t.error = ""
        th = threading.Thread(target=self._run_task, args=(t, stop, holder), daemon=True)
        self._running[t.uid] = (th, stop, holder)
        th.start()

    def _run_task(self, t: Task, stop: threading.Event, holder: list) -> None:
        def set_status(s: str) -> None:
            t.status = s

        result: Optional[bool] = None
        err: Optional[BaseException] = None
        try:
            src = self.source_factory(t)
            t.source["verify"] = bool(self.settings.verify_md5)
            while True:
                dl = SegmentedDownload(t, src, int(self.settings.connections_per_file), stop, set_status)
                holder[0] = dl
                holder[1] = 0
                try:
                    result = dl.run()
                    break
                except QuotaExceeded as qe:
                    if self.quota_handler and self.settings.quota_copy_fallback \
                            and not t.source.get("copied_from"):
                        t.status = "starting"
                        t.note = "할당량 초과 → 내 드라이브로 복사 후 받는 중"
                        src = self.quota_handler(t, qe)  # 실패하면 예외
                        continue
                    raise
            t.done_bytes = dl.done_bytes() if result is not None else t.done_bytes
        except BaseException as e:  # noqa: BLE001
            err = e
        finally:
            cleanup = getattr(self, "_cleanup_hook", None)
            with self.lock:
                want = self._want.pop(t.uid, None)
                self._running.pop(t.uid, None)
                t.speed = 0.0
                t.connections = 0
                if want == "remove":
                    self._delete_partial(t)
                    if t in self.tasks:
                        self.tasks.remove(t)
                elif err is not None and not (want == "pause" and stop.is_set()):
                    t.status = "error"
                    t.error = str(err) or err.__class__.__name__
                elif result:
                    t.status = "skipped" if getattr(holder[0], "skipped", False) else "done"
                    if t.size is not None:
                        t.done_bytes = t.size
                else:
                    t.status = "paused"
                self._dirty = True
            if cleanup and (result or want == "remove"):
                try:
                    cleanup(t)
                except Exception:  # noqa: BLE001
                    pass

    def shutdown(self, timeout: float = 10.0) -> None:
        with self.lock:
            running = list(self._running.items())
            for uid, (_, stop, _) in running:
                self._want[uid] = "pause"
                stop.set()
        end = time.monotonic() + timeout
        for _, (th, _, _) in running:
            th.join(max(0.1, end - time.monotonic()))
        self._quit.set()
        self.save_queue()
