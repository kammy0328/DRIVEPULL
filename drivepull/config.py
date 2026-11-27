"""앱 경로, 설정 저장/불러오기."""
from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = "DRIVEPULL"
APP_VERSION = "0.6.2"


def app_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


SETTINGS_PATH = app_dir() / "settings.json"
QUEUE_PATH = app_dir() / "queue.json"
TOKEN_PATH = app_dir() / "token.json"
CLIENT_SECRET_PATH = app_dir() / "client_secret.json"


@dataclass
class Settings:
    connections_per_file: int = 16   # 자동 조절이 켜져 있으면 최대값
    auto_connections: bool = True
    concurrent_files: int = 3
    verify_md5: bool = True
    quota_copy_fallback: bool = True
    keep_folder_structure: bool = True
    cleanup_on_exit: bool = True     # 종료 시 받다 만 파일 정리
    sort_key: str = "name"           # name | size | type | modified
    sort_desc: bool = False
    api_key: str = ""
    last_save_dir: str = ""
    window_geometry: str = ""
    extra: dict = field(default_factory=dict)

    _lock = threading.Lock()

    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            for k, v in data.items():
                if hasattr(s, k) and not k.startswith("_"):
                    setattr(s, k, v)
        except (OSError, ValueError):
            pass
        if not s.extra.get("v2"):
            # 0.2 이하 기본값(8)은 자동 조절 최대값으로 쓰기엔 낮아서 16으로 올린다
            if s.connections_per_file == 8:
                s.connections_per_file = 16
            s.extra["v2"] = True
        if not s.last_save_dir:
            s.last_save_dir = str(Path.home() / "Downloads")
        s.connections_per_file = max(1, min(32, int(s.connections_per_file)))
        s.concurrent_files = max(1, min(10, int(s.concurrent_files)))
        return s

    def save(self) -> None:
        with self._lock:
            data = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
            tmp = SETTINGS_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, SETTINGS_PATH)
