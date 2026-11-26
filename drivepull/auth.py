"""구글 계정 로그인 (OAuth 2.0 데스크톱 앱 방식)."""
from __future__ import annotations

import json
import os
import threading
from typing import Optional

import requests

from .config import CLIENT_SECRET_PATH, TOKEN_PATH

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",  # 공유받은 파일 목록·다운로드
    "https://www.googleapis.com/auth/drive.file",      # 할당량 초과 시 내 드라이브에 임시 사본 생성·삭제
]


def load_client_config() -> Optional[dict]:
    """빌드 시 넣은 클라이언트 정보 → 사용자가 불러온 client_secret.json 순으로 찾는다."""
    if CLIENT_SECRET_PATH.exists():
        try:
            return json.loads(CLIENT_SECRET_PATH.read_text(encoding="utf-8"))
        except ValueError:
            pass
    try:
        from . import _client_config  # type: ignore  # CI에서 생성
        if getattr(_client_config, "CLIENT_ID", ""):
            return {
                "installed": {
                    "client_id": _client_config.CLIENT_ID,
                    "client_secret": _client_config.CLIENT_SECRET,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
    except ImportError:
        pass
    return None


def import_client_secret(path: str) -> None:
    data = json.loads(open(path, "r", encoding="utf-8").read())
    if "installed" not in data:
        raise ValueError("데스크톱 앱 유형의 OAuth 클라이언트 JSON이 아닙니다 (\"installed\" 항목 없음)")
    CLIENT_SECRET_PATH.write_text(json.dumps(data), encoding="utf-8")


class GoogleAuth:
    def __init__(self) -> None:
        self.creds = None
        self.email: str = ""
        self.name: str = ""
        self._lock = threading.Lock()

    @property
    def logged_in(self) -> bool:
        return self.creds is not None

    def has_client(self) -> bool:
        return load_client_config() is not None

    def load(self) -> bool:
        if not TOKEN_PATH.exists():
            return False
        try:
            from google.oauth2.credentials import Credentials
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
            self.creds = creds
            self._ensure_valid()
            self._fetch_profile()
            return True
        except Exception:  # noqa: BLE001  토큰 만료/폐기
            self.creds = None
            return False

    def login(self) -> None:
        cfg = load_client_config()
        if cfg is None:
            raise RuntimeError("OAuth 클라이언트가 설정되지 않았습니다. 설정에서 client_secret.json을 불러와 주세요.")
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow = InstalledAppFlow.from_client_config(cfg, SCOPES)
        creds = flow.run_local_server(
            port=0,
            open_browser=True,
            prompt="consent",
            authorization_prompt_message="",
            success_message="DRIVEPULL 로그인이 완료되었습니다. 이 창은 닫아도 됩니다.",
            timeout_seconds=300,
        )
        if creds is None:
            raise RuntimeError("로그인 시간이 초과되었습니다")
        with self._lock:
            self.creds = creds
            self._save()
        self._fetch_profile()

    def logout(self) -> None:
        with self._lock:
            if self.creds is not None and getattr(self.creds, "token", None):
                try:
                    requests.post("https://oauth2.googleapis.com/revoke",
                                  params={"token": self.creds.token}, timeout=10)
                except requests.RequestException:
                    pass
            self.creds = None
            self.email = ""
            self.name = ""
            try:
                os.remove(TOKEN_PATH)
            except OSError:
                pass

    def _save(self) -> None:
        TOKEN_PATH.write_text(self.creds.to_json(), encoding="utf-8")

    def _ensure_valid(self) -> None:
        if self.creds is None:
            return
        if not self.creds.valid:
            from google.auth.transport.requests import Request
            self.creds.refresh(Request())
            self._save()

    def headers(self) -> dict:
        with self._lock:
            if self.creds is None:
                return {}
            self._ensure_valid()
            return {"Authorization": f"Bearer {self.creds.token}"}

    def force_refresh(self) -> None:
        with self._lock:
            if self.creds is None:
                return
            from google.auth.transport.requests import Request
            self.creds.refresh(Request())
            self._save()

    def _fetch_profile(self) -> None:
        try:
            r = requests.get("https://www.googleapis.com/drive/v3/about",
                             params={"fields": "user(emailAddress,displayName)"},
                             headers=self.headers(), timeout=15)
            if r.ok:
                u = r.json().get("user", {})
                self.email = u.get("emailAddress", "")
                self.name = u.get("displayName", "")
        except requests.RequestException:
            pass
