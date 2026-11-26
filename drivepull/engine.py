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
AUTO_START = 8             # 자동 조절 시작 연결 수 (이전 고정 기본값과 같음)
TUNE_WINDOW = 2.0          # 속도 측정 간격(초)
TUNE_GAIN = 1.08           # 연결을 늘렸을 때 이만큼(8%) 이상 빨라져야 유지
TUNE_SETTLE = 1.5          # 새 연결이 첫 데이터를 받기까지 기다린 뒤 재기 시작(구글 응답 ~1초)
TUNE_FAST = 1.4            # 늘렸을 때 이만큼(40%) 이상 빨라지면 다음엔 두 배로 늘림
PART_SUFFIX = ".drivepull"


# ---------------------------------------------------------------- errors
class DownloadError(Exception):
    """재시도해도 해결되지 않는 오류."""


class QuotaExceeded(DownloadError):
    """구글의 다운로드 할당량 초과."""


class TransientError(Exception):
    """재시도할 만한 일시적 오류 (429, 5xx 등)."""

    def __init__(self, msg: str, wait: float | None = None, throttle: bool = False):
        super().__init__(msg)
        self.wait = wait
        self.throttle = throttle   # 구글 요청 제한 (자동 조절이 연결을 줄이는 신호)


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


def _win_set_eof(fh, size: int) -> bool:
    """SetFilePointerEx + SetEndOfFile 로 크기만 지정 (0 채우기 없음)."""
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong,
                                         ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
        k32.SetFilePointerEx.restype = wintypes.BOOL
        k32.SetEndOfFile.argtypes = [wintypes.HANDLE]
        k32.SetEndOfFile.restype = wintypes.BOOL
        h = wintypes.HANDLE(msvcrt.get_osfhandle(fh.fileno()))
        return bool(k32.SetFilePointerEx(h, size, None, 0)) and bool(k32.SetEndOfFile(h))
    except Exception:  # noqa: BLE001
        return False


def preallocate(path: str, size: int) -> bool:
    """받을 파일 크기만큼 미리 잡는다. 순간적으로 끝나면 True.

    Python 의 truncate() 는 Windows 에서 늘린 만큼 0을 실제로 써서 40GB면 수십 초가 걸린다.
    sparse 지정 후 SetEndOfFile 로 크기만 바꾼다.
    """
    with open(fs_path(path), "wb") as f:
        if sys.platform == "win32":
            sparse = _set_sparse(f, True)
            if _win_set_eof(f, size):
                return sparse
        f.truncate(size)
        return True


HDD_MAX_CONNS = 4
_disk_cache: dict[str, Optional[bool]] = {}
disk_debug: dict[str, str] = {}   # 판별 실패 원인 (진단용)


def disk_is_hdd(path: str) -> Optional[bool]:
    """저장 위치가 HDD(탐색 지연 있는 디스크)면 True, SSD면 False, 모르면 None (네트워크 드라이브 등).

    Windows IOCTL_STORAGE_QUERY_PROPERTY / StorageDeviceSeekPenaltyProperty 사용 (관리자 권한 불필요).
    """
    if sys.platform != "win32":
        return None
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if not drive or drive.startswith("\\\\"):
        return None
    key = drive.upper()
    if key in _disk_cache:
        return _disk_cache[key]
    result: Optional[bool] = None
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                        ctypes.c_void_p]
        k32.DeviceIoControl.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.CreateFileW(f"\\\\.\\{drive}", 0, 3, None, 3, 0, None)  # 접근 0, 읽기·쓰기 공유, OPEN_EXISTING
        if h and h != wintypes.HANDLE(-1).value:
            class Query(ctypes.Structure):
                _fields_ = [("PropertyId", wintypes.DWORD), ("QueryType", wintypes.DWORD),
                            ("AdditionalParameters", ctypes.c_ubyte * 1)]

            class SeekPenalty(ctypes.Structure):
                _fields_ = [("Version", wintypes.DWORD), ("Size", wintypes.DWORD),
                            ("IncursSeekPenalty", wintypes.BOOLEAN)]

            class Trim(ctypes.Structure):
                _fields_ = [("Version", wintypes.DWORD), ("Size", wintypes.DWORD),
                            ("TrimEnabled", wintypes.BOOLEAN)]

            ret = wintypes.DWORD(0)
            q = Query(7, 0)   # StorageDeviceSeekPenaltyProperty, PropertyStandardQuery
            out = SeekPenalty()
            if k32.DeviceIoControl(h, 0x2D1400, ctypes.byref(q), ctypes.sizeof(q),
                                   ctypes.byref(out), ctypes.sizeof(out), ctypes.byref(ret), None):
                result = bool(out.IncursSeekPenalty)
            else:
                disk_debug[key] = f"seek penalty 조회 실패 err={ctypes.get_last_error()}"
                q2 = Query(8, 0)   # StorageDeviceTrimProperty: TRIM 이 켜져 있으면 SSD
                tr = Trim()
                if k32.DeviceIoControl(h, 0x2D1400, ctypes.byref(q2), ctypes.sizeof(q2),
                                       ctypes.byref(tr), ctypes.sizeof(tr), ctypes.byref(ret), None) \
                        and tr.TrimEnabled:
                    result = False
                else:
                    disk_debug[key] += f", trim 조회 err={ctypes.get_last_error()}"
            k32.CloseHandle(h)
        else:
            disk_debug[key] = f"볼륨 열기 실패 err={ctypes.get_last_error()}"
    except Exception as e:  # noqa: BLE001
        disk_debug[key] = repr(e)
        result = None
    _disk_cache[key] = result
    return result


def _range_total(cr: str) -> Optional[int]:
    import re
    m = re.match(r"bytes \d+-\d+/(\d+)", cr or "")
    return int(m.group(1)) if m else None


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
    "waiting": "구글 제한 대기",
}
LIMIT_WAITS = (10, 30, 60, 120)   # 구글 제한 시 재시도 대기(분)


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
    retry_at: float = 0.0     # 구글 제한 대기 후 자동 재시도 시각 (epoch)
    limit_hits: int = 0
    # 런타임 전용
    speed: float = 0.0
    connections: int = 0
    verify_bytes: int = 0

    PERSIST = ("name", "dest", "source", "size", "md5", "uid", "status",
               "done_bytes", "error", "note", "retry_at", "limit_hits")

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
                 status_cb: Callable[[str], None] | None = None,
                 auto: bool = False, start: int | None = None):
        self.task = task
        self.source = source
        self.connections = max(1, min(connections, getattr(source, "max_connections", connections)))
        self.min_split = getattr(source, "min_split", MIN_SPLIT)
        self.hdd = disk_is_hdd(task.dest) is True
        if self.hdd and self.connections > HDD_MAX_CONNS:
            # HDD에 여러 위치를 동시에 쓰면 헤드가 왔다 갔다 해서 오히려 느려짐
            self.connections = HDD_MAX_CONNS
            task.note = f"HDD 감지: 연결 {HDD_MAX_CONNS}개로 제한"
        self.auto = auto
        self.target = min(self.connections, max(1, start or AUTO_START)) if auto else self.connections
        self.best_target = self.target
        self.throttled = 0          # 429·요청 제한 응답 횟수
        self._retiring = 0
        self._tune_t = 0.0
        self._tune_c = 0
        self._tune_probe: Optional[tuple[int, float]] = None
        self._plateau_until = 0.0
        self._settle_until = 0.0
        self._best_rate = 0.0
        self._fast = True           # 처음엔 두 배씩 늘린다
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
        self._prefetch = None   # (구간, 시작 위치, 응답): 첫 요청 응답을 첫 구간에 그대로 사용
        self.manager_wake: Callable[[], None] = lambda: None

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
            if best is None or best.remaining < 2 * self.min_split:
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

    def _should_retire(self) -> bool:
        """자동 조절로 목표 연결 수가 줄면 남는 연결은 지금 구간을 반납하고 빠진다."""
        if not self.auto:
            return False
        with self.lock:
            if self.active_workers - self._retiring > self.target:
                self._retiring += 1
                return True
        return False

    def _fetch(self, seg: _Seg, sess: requests.Session, fh) -> bool:
        """구간을 받는다. 연결을 줄여야 해서 중간에 빠지면 True."""
        attempt = 0
        retire = False
        try:
            while seg.remaining > 0 and not self._halted() and not retire:
                try:
                    resp = self._take_prefetch(seg) or self.source.open(sess, seg.pos, seg.end)
                    try:
                        if resp.status_code != 206:
                            raise DownloadError("서버가 구간 다운로드를 지원하지 않습니다")
                        cr = resp.headers.get("Content-Range", "")
                        if not cr.startswith(f"bytes {seg.pos}-"):
                            raise TransientError(f"잘못된 Content-Range: {cr}")
                        total = _range_total(cr)
                        if total is not None and self.size is not None and total != self.size:
                            self._clear_state()
                            raise DownloadError("원본 파일 크기가 바뀌었습니다. 다시 시도하면 처음부터 받습니다")
                        for chunk in resp.iter_content(CHUNK):
                            if self._halted():
                                break
                            if self._should_retire():
                                retire = True
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
                    return False
                except (TransientError, requests.RequestException, OSError) as e:
                    if isinstance(e, OSError) and not isinstance(e, requests.RequestException):
                        # 디스크 오류는 재시도 의미 없음
                        if getattr(e, "errno", None) in (28, 112):  # ENOSPC / ERROR_DISK_FULL
                            self._fail(DownloadError("디스크 공간이 부족합니다"))
                            return False
                    if getattr(e, "throttle", False):
                        with self.lock:
                            self.throttled += 1
                    attempt += 1
                    if attempt > MAX_RETRY:
                        self._fail(DownloadError(f"재시도 한도 초과: {e}"))
                        return False
                    wait = getattr(e, "wait", None) or min(30.0, 1.5 ** attempt) + random.random()
                    self.stop.wait(wait)
        finally:
            with self.lock:
                seg.active = False
        return retire

    def _take_prefetch(self, seg: _Seg):
        with self.lock:
            pf = self._prefetch
            if pf is not None and pf[0] is seg and seg.pos == pf[1]:
                self._prefetch = None
                return pf[2]
        return None

    def _worker(self) -> None:
        sess = make_session()
        try:
            fh = open(fs_path(self.task.part_path), "r+b", buffering=0)
        except OSError as e:
            self._fail(DownloadError(f"파일을 열 수 없습니다: {e}"))
            return
        with self.lock:
            self.active_workers += 1
        retired = False
        try:
            while not self._halted():
                if self._should_retire():
                    retired = True
                    return
                seg = self._acquire()
                if seg is None:
                    return
                if self._fetch(seg, sess, fh):
                    retired = True
                    return
        except BaseException as e:  # noqa: BLE001
            self._fail(DownloadError(f"내부 오류: {e!r}"))
        finally:
            with self.lock:
                self.active_workers -= 1
                if retired:
                    self._retiring -= 1
            fh.close()
            sess.close()

    # ---- 단일 스트림 (크기를 모르거나 Range 미지원)
    def _run_single(self, first=None) -> bool:
        sess = make_session()
        self._single_done = 0
        attempt = 0
        while True:
            try:
                resp, first = (first, None) if first is not None else (self.source.open(sess, None, None), None)
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

        first = None
        size = t.size
        resumed = size is not None and self._load_state(size)
        if size is None:
            # 크기를 모르면 0번 바이트부터 요청하고, 그 응답을 첫 구간 다운로드로 그대로 쓴다
            first = self._first_request()
            if first is None:
                return False
            if first.status_code == 206:
                size = _range_total(first.headers.get("Content-Range", ""))
            if size is None or first.status_code != 206:
                cl = first.headers.get("Content-Length")
                if first.status_code == 200 and cl and cl.isdigit():
                    t.size = int(cl)
                t.note = "단일 연결 (서버가 구간 다운로드를 지원하지 않음)"
                self.status_cb("downloading")
                if not self._run_single(first):
                    return False
                return self._finish()
            t.size = size
        self.size = size

        # 같은 파일이 이미 받아져 있으면 건너뛴다
        if not resumed and os.path.exists(fs_path(t.dest)) \
                and not os.path.exists(fs_path(t.part_path)) \
                and os.path.getsize(fs_path(t.dest)) == size:
            if first is not None:
                first.close()
            self.skipped = True
            self._single_done = size
            return True

        if size == 0:
            if first is not None:
                first.close()
            open(fs_path(t.part_path), "wb").close()
            return self._finish()

        if not resumed:
            self._clear_state()
            if not preallocate(t.part_path, size):
                t.note = "이 드라이브(exFAT 등)는 sparse 파일을 지원하지 않아 초반 속도가 느릴 수 있음"
            n = max(1, min(self.connections, size // self.min_split or 1))
            step = size // n
            self.segs = []
            for i in range(n):
                a = i * step
                b = size - 1 if i == n - 1 else (i + 1) * step - 1
                self.segs.append(_Seg(a, b))
            self._save_state()
        if first is not None:
            seg0 = next((sg for sg in self.segs if sg.start == 0 and sg.remaining > 0 and sg.pos == 0), None)
            if seg0 is not None:
                self._prefetch = (seg0, 0, first)
            else:
                first.close()

        if self.done_bytes() >= size:
            return self._finish()

        self.status_cb("downloading")
        workers: list[threading.Thread] = []
        self._spawn(workers, self.target)
        last_save = time.monotonic()
        while any(w.is_alive() for w in workers):
            alive = [w for w in workers if w.is_alive()]
            alive[0].join(0.25)
            now = time.monotonic()
            if self.auto and not self._halted():
                self._tune(now, workers)
            if now - last_save >= STATE_SAVE_INTERVAL:
                self._save_state()
                last_save = now
        self._save_state()
        with self.lock:
            if self._prefetch is not None:
                self._prefetch[2].close()
                self._prefetch = None

        if self.fatal is not None:
            raise self.fatal
        if self.done_bytes() < size:
            return False  # 일시정지
        return self._finish()

    def _spawn(self, workers: list, n: int) -> None:
        for _ in range(max(0, n)):
            w = threading.Thread(target=self._worker, daemon=True)
            w.start()
            workers.append(w)

    def _remaining(self) -> int:
        with self.lock:
            return sum(s.remaining for s in self.segs)

    def _tune(self, now: float, workers: list) -> None:
        """연결 수 자동 조절.

        AUTO_START 개로 시작해서 TUNE_WINDOW 초마다 속도를 잰다.
        - 처음엔 두 배씩 늘리다가, 늘려도 40% 미만으로 빨라지면 2~4개씩 늘린다.
        - 연결을 늘린 뒤 속도가 GAIN 이상 오르면 유지하고 더 늘려본다.
        - 거의 안 오르면 이전 개수로 되돌리고 잠시 멈췄다가 다시 시도한다 (회선 상태가 바뀔 수 있으니).
        - 구글이 요청을 제한(429 등)하면 바로 40% 줄이고 한동안 늘리지 않는다.
        """
        if self.counter == 0:
            self._tune_t = now   # 첫 데이터가 들어오기 전엔 재지 않음
            return
        if self._tune_t == 0.0 or now < self._settle_until:
            self._tune_t, self._tune_c = now, self.counter   # 새 연결이 자리 잡는 동안은 재지 않음
            with self.lock:
                self.throttled = 0   # 줄이기 전에 이미 보낸 요청의 제한 응답은 무시
            return
        dt = now - self._tune_t
        if dt < TUNE_WINDOW:
            return
        rate = (self.counter - self._tune_c) / dt
        self._tune_t, self._tune_c = now, self.counter
        with self.lock:
            throttled, self.throttled = self.throttled, 0
        if throttled:
            self.target = max(2, int(self.target * 0.6))
            self._tune_probe = None
            self._plateau_until = now + 60
            self._settle_until = now + TUNE_SETTLE
            self.best_target = min(self.best_target, self.target)
            return
        if self._tune_probe is not None:
            prev_target, prev_rate = self._tune_probe
            self._tune_probe = None
            if rate < prev_rate * TUNE_GAIN:
                self.target = prev_target        # 늘려도 별 차이 없음 → 되돌림
                self._plateau_until = now + 30
                self._fast = False
                return
            self._fast = self._fast and rate >= prev_rate * TUNE_FAST
        if rate >= self._best_rate * 0.95:
            self._best_rate = max(self._best_rate, rate)
            self.best_target = self.target
        possible = max(1, self._remaining() // self.min_split)
        cap = min(self.connections, possible)
        if now >= self._plateau_until and self.target < cap and self.active_workers >= self.target:
            step = self.target if self._fast else (2 if self.target < 8 else 4)
            self._tune_probe = (self.target, rate)
            new = min(cap, self.target + step)
            old, self.target = self.target, new     # 목표를 먼저 올려야 새 연결이 바로 빠지지 않음
            self._spawn(workers, new - old)
            self._settle_until = now + TUNE_SETTLE
            self._tune_t, self._tune_c = now, self.counter

    def _first_request(self):
        sess = make_session()
        attempt = 0
        while True:
            try:
                return self.source.open(sess, 0, None)
            except DownloadError:
                raise
            except (TransientError, requests.RequestException) as e:
                attempt += 1
                if attempt > 5:
                    raise DownloadError(f"연결 실패: {e}")
                if self.stop.wait(min(20.0, 2 ** attempt)):
                    return None

    def _finish(self) -> bool:
        t = self.task
        if t.md5 and t.source.get("verify", True):
            self.status_cb("verifying")
            self.manager_wake()
            vstop = self.stop
            t.verify_bytes = 0
            got = md5_file(t.part_path, progress=lambda n: setattr(t, "verify_bytes", n), stop=vstop)
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
        self.learned: int | None = None   # 자동 조절로 찾은 연결 수 (다음 파일 시작값)
        self._quit = threading.Event()
        self._wake = threading.Event()
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
        self._wake.set()

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
                elif t.status in ("queued", "waiting"):
                    t.status = "paused"
            self._dirty = True

    def resume(self, uids) -> None:
        with self.lock:
            for uid in uids:
                t = self.get(uid)
                if t and t.status in ("paused", "error", "waiting"):
                    t.status = "queued"
                    t.error = ""
                    t.retry_at = 0.0
            self._dirty = True
        self._wake.set()

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
            self.resume([t.uid for t in self.tasks if t.status in ("paused", "error", "waiting")])

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
        while not self._quit.is_set():
            self._wake.wait(0.25)
            self._wake.clear()
            if self._quit.is_set():
                break
            now = time.monotonic()
            with self.lock:
                wall = time.time()
                for t in self.tasks:
                    if t.status == "waiting" and t.retry_at and wall >= t.retry_at:
                        t.status = "queued"
                        t.retry_at = 0.0
                        self._dirty = True
                limit = max(1, int(self.settings.concurrent_files))

                def busy() -> int:
                    # MD5 검증 중인 파일은 다운로드 칸을 차지하지 않음 → 검증하는 동안 다음 파일 시작
                    return sum(1 for u in self._running if (x := self.get(u)) and x.status != "verifying")
                if busy() < limit:
                    for t in self.tasks:
                        if busy() >= limit:
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
                        t.connections = dl.active_workers - dl._retiring
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
        limited: Optional[BaseException] = None
        try:
            src = self.source_factory(t)
            t.source["verify"] = bool(self.settings.verify_md5)
            while True:
                auto = bool(getattr(self.settings, "auto_connections", False))
                dl = SegmentedDownload(t, src, int(self.settings.connections_per_file), stop, set_status,
                                       auto=auto, start=self.learned)
                dl.manager_wake = self._wake.set
                holder[0] = dl
                holder[1] = 0
                try:
                    result = dl.run()
                    break
                except QuotaExceeded as qe:
                    if self.quota_handler and self.settings.quota_copy_fallback \
                            and not t.source.get("copied_from"):
                        try:
                            new_src = self.quota_handler(t, qe)
                        except DownloadError as he:
                            new_src = None
                            t.note = str(he)
                        if new_src is not None:
                            t.status = "starting"
                            t.note = "구글 제한 → 내 드라이브 사본으로 받는 중"
                            src = new_src
                            continue
                    limited = qe
                    break
            t.done_bytes = dl.done_bytes()
            if dl.auto and dl._best_rate > 0 and (t.size or 0) >= 64 * MIN_SPLIT:
                # 다음 파일은 이번에 찾은 값 근처에서 시작 (처음부터 다시 올라가지 않게)
                with self.lock:
                    self.learned = max(AUTO_START, dl.best_target)
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
                elif limited is not None and want != "pause":
                    # 받은 부분은 그대로 두고 기다렸다가 자동으로 이어받기
                    wait_min = LIMIT_WAITS[min(t.limit_hits, len(LIMIT_WAITS) - 1)]
                    t.limit_hits += 1
                    t.status = "waiting"
                    t.retry_at = time.time() + wait_min * 60
                    t.error = str(limited)
                    if not t.note:
                        t.note = "구글 로그인하면 내 드라이브 사본으로 기다리지 않고 받을 수 있음"
                elif err is not None and not (want == "pause" and stop.is_set()):
                    t.status = "error"
                    t.error = str(err) or err.__class__.__name__
                elif result:
                    t.limit_hits = 0
                    t.status = "skipped" if getattr(holder[0], "skipped", False) else "done"
                    if t.size is not None:
                        t.done_bytes = t.size
                else:
                    t.status = "paused"
                self._dirty = True
            self._wake.set()
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
        self._wake.set()
        self.save_queue()
