"""엔진, 로그인, Drive API를 묶는 계층 (UI와 분리)."""
from __future__ import annotations

import os
import re
from typing import Iterable

from .auth import GoogleAuth
from .config import QUEUE_PATH, Settings
from .drive import ApiSource, DriveAPI, DriveItem, PublicSource
from .engine import DownloadError, DownloadManager, Task


def underscore(name: str) -> str:
    """폴더 이름 띄어쓰기를 _ 로 (연속 공백은 _ 하나, 앞뒤 공백 제거)."""
    return re.sub(r"\s+", "_", name.strip()) or name


class Service:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.auth = GoogleAuth()
        self.manager = DownloadManager(settings, self._source_for, QUEUE_PATH, self._quota_fallback)
        self.manager._cleanup_hook = self._cleanup
        self.manager.load_queue()

    def api(self) -> DriveAPI:
        return DriveAPI(self.auth, self.settings.api_key)

    # ---- 소스 생성
    def _source_for(self, t: Task):
        s = t.source
        mode = s.get("mode", "api")
        if mode == "api" and not self.api().usable:
            # 로그아웃 상태면 공개 링크 방식으로 시도
            if s.get("export"):
                raise DownloadError("구글 문서 내보내기는 로그인이 필요합니다")
            return PublicSource(s["id"], s.get("rk", ""))
        if mode == "public" and self.api().usable and not s.get("export"):
            # 로그인했으면 더 안정적인 API 방식 사용
            return ApiSource(self.api(), s["id"], s.get("rk", ""))
        if mode == "public":
            return PublicSource(s["id"], s.get("rk", ""))
        return ApiSource(self.api(), s["id"], s.get("rk", ""), s.get("export"))

    def source_for_item(self, it: DriveItem):
        """미리보기 재생용 소스 (다운로드와 같은 규칙: 로그인했으면 API, 아니면 공개 링크)."""
        fake = Task(name=it.name, dest="", source={"mode": it.mode, "id": it.id, "rk": it.resource_key})
        return self._source_for(fake)

    def _quota_fallback(self, t: Task, err):
        """할당량 초과 파일을 내 드라이브에 사본으로 만든 뒤 사본을 받는다."""
        if not self.auth.logged_in:
            return None   # 로그인 안 됨 → 대기 후 자동 재시도
        api = self.api()
        orig = t.source["id"]
        try:
            meta = api.copy(orig, t.source.get("rk", ""), f"DRIVEPULL_tmp_{t.name}")
        except DownloadError as e:
            raise DownloadError(f"할당량 초과, 사본 생성도 실패: {e}")
        t.source = {**t.source, "id": meta["id"], "rk": "", "mode": "api", "copied_from": orig}
        if meta.get("md5Checksum"):
            t.md5 = meta["md5Checksum"]
        return ApiSource(api, meta["id"])

    def _cleanup(self, t: Task) -> None:
        if t.source.get("copied_from") and self.auth.logged_in:
            self.api().delete(t.source["id"])

    # ---- 큐 추가
    def enqueue(self, root: DriveItem, selected: Iterable[tuple[DriveItem, str]], save_dir: str) -> int:
        """selected: (파일 항목, 루트 기준 상대 폴더 경로)."""
        tasks = []
        used: set[str] = set()
        for it, rel_dir in selected:
            if it.is_folder or it.unsupported:
                continue
            if self.settings.keep_folder_structure:
                if self.settings.folder_underscore and rel_dir:
                    rel_dir = os.path.join(*[underscore(x) for x in rel_dir.split(os.sep)])
                dest = os.path.join(save_dir, rel_dir, it.name)
            else:
                dest = os.path.join(save_dir, it.name)
                root_, ext = os.path.splitext(dest)
                n = 1
                while dest.lower() in used:
                    dest = f"{root_} ({n}){ext}"
                    n += 1
            used.add(dest.lower())
            tasks.append(Task(
                name=it.name, dest=os.path.normpath(dest), size=it.size, md5=it.md5,
                source={"mode": it.mode, "id": it.id, "rk": it.resource_key, "export": it.export_mime},
            ))
        self.manager.add(tasks)
        return len(tasks)

    def shutdown(self) -> None:
        self.manager.shutdown()
