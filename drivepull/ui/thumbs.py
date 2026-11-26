"""썸네일 비동기 로더 (메모리 + 디스크 캐시)."""
from __future__ import annotations

import os
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, Qt, Signal
from PySide6.QtGui import QImage

from ..config import app_dir
from ..engine import make_session

MAX_W = 480
MEM_LIMIT = 800


class ThumbLoader(QObject):
    loaded = Signal(str)  # drive id

    def __init__(self, headers_fn):
        super().__init__()
        self.headers_fn = headers_fn
        self.dir = app_dir() / "thumbs"
        self.dir.mkdir(exist_ok=True)
        self.pool = ThreadPoolExecutor(max_workers=8)
        self.mem: OrderedDict[str, QImage] = OrderedDict()
        self.failed: set[str] = set()
        self.pending: set[str] = set()
        self.wanted: set[str] = set()
        self.lock = threading.Lock()
        self._local = threading.local()
        threading.Thread(target=self._prune, daemon=True).start()

    def _prune(self, limit: int = 300 << 20) -> None:
        """디스크 캐시가 300MB를 넘으면 오래된 것부터 지운다."""
        try:
            files = sorted(self.dir.glob("*.jpg"), key=lambda f: f.stat().st_mtime)
            total = sum(f.stat().st_size for f in files)
            for f in files:
                if total <= limit:
                    break
                total -= f.stat().st_size
                f.unlink(missing_ok=True)
        except OSError:
            pass

    def get(self, fid: str) -> QImage | None:
        with self.lock:
            img = self.mem.get(fid)
            if img is not None:
                self.mem.move_to_end(fid)
            return img

    def request(self, items) -> None:
        """items: DriveItem 목록. 화면에 보이는 순서대로 넣으면 그 순서로 받는다."""
        with self.lock:
            self.wanted = {it.id for it in items}
            jobs = [it for it in items
                    if it.thumb_url and it.id not in self.mem and it.id not in self.pending
                    and it.id not in self.failed]
            for it in jobs:
                self.pending.add(it.id)
        for it in jobs:
            self.pool.submit(self._job, it.id, it.thumb_url, it.mode)

    def _session(self):
        s = getattr(self._local, "s", None)
        if s is None:
            s = self._local.s = make_session()
        return s

    def _job(self, fid: str, url: str, mode: str) -> None:
        try:
            with self.lock:
                if fid not in self.wanted:  # 다른 폴더로 이동해서 더 이상 필요 없음
                    return
            path = self.dir / f"{fid}.jpg"
            img = QImage()
            if path.exists():
                img.load(str(path))
            if img.isNull():
                headers = self.headers_fn() if mode == "api" else {}
                r = self._session().get(url, headers=headers, timeout=(10, 30))
                if r.status_code != 200 or not r.headers.get("Content-Type", "").startswith("image"):
                    with self.lock:
                        self.failed.add(fid)
                    return
                img.loadFromData(QByteArray(r.content))
                if img.isNull():
                    with self.lock:
                        self.failed.add(fid)
                    return
                if img.width() > MAX_W:
                    img = img.scaledToWidth(MAX_W, Qt.SmoothTransformation)
                buf = QBuffer()
                buf.open(QIODevice.WriteOnly)
                img.save(buf, "JPG", 88)
                tmp = str(path) + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(bytes(buf.data()))
                os.replace(tmp, path)
            with self.lock:
                self.mem[fid] = img
                while len(self.mem) > MEM_LIMIT:
                    self.mem.popitem(last=False)
            self.loaded.emit(fid)
        except Exception:  # noqa: BLE001
            with self.lock:
                self.failed.add(fid)
        finally:
            with self.lock:
                self.pending.discard(fid)

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
