"""Google Drive 링크 해석, 파일 목록, 다운로드 소스."""
from __future__ import annotations

import html
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from .engine import DownloadError, ProbeResult, QuotaExceeded, TransientError, make_session

API = "https://www.googleapis.com/drive/v3"
USERCONTENT = "https://drive.usercontent.google.com/download"
FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
TIMEOUT = (15, 60)
FIELDS = ("id,name,mimeType,size,md5Checksum,resourceKey,shortcutDetails,thumbnailLink,"
          "videoMediaMetadata(width,height,durationMillis),imageMediaMetadata(width,height)")

# 구글 문서류는 오피스 파일로 내보낸다
EXPORTS = {
    "application/vnd.google-apps.document":
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet":
        ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation":
        ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
    "application/vnd.google-apps.drawing": ("application/pdf", ".pdf"),
}


# ---------------------------------------------------------------- 링크 해석
@dataclass
class DriveLink:
    kind: str          # "folder" | "file" | "unknown"
    id: str
    resource_key: str = ""


_ID = r"[A-Za-z0-9_-]{10,}"


def parse_link(text: str) -> Optional[DriveLink]:
    text = text.strip()
    if not text:
        return None
    if re.fullmatch(_ID, text):
        return DriveLink("unknown", text)
    try:
        u = urlparse(text)
    except ValueError:
        return None
    if "google.com" not in (u.netloc or ""):
        return None
    q = parse_qs(u.query)
    rk = (q.get("resourcekey") or [""])[0]
    m = re.search(r"/folders/(" + _ID + ")", u.path)
    if m:
        return DriveLink("folder", m.group(1), rk)
    m = re.search(r"/(?:file|document|spreadsheets|presentation|drawings)/(?:u/\d+/)?d/(" + _ID + ")", u.path)
    if m:
        return DriveLink("file", m.group(1), rk)
    if "id" in q and re.fullmatch(_ID, q["id"][0]):
        kind = "folder" if "folderview" in u.path else "unknown"
        return DriveLink(kind, q["id"][0], rk)
    return None


# ---------------------------------------------------------------- 항목
@dataclass
class DriveItem:
    id: str
    name: str
    mime: str = ""
    size: Optional[int] = None
    md5: Optional[str] = None
    resource_key: str = ""
    is_folder: bool = False
    children: list["DriveItem"] = field(default_factory=list)
    export_mime: Optional[str] = None
    unsupported: bool = False
    mode: str = "api"   # "api" | "public"
    thumb_url: str = ""
    width: int = 0
    height: int = 0
    duration_ms: int = 0

    def walk_files(self):
        if self.is_folder:
            for c in self.children:
                yield from c.walk_files()
        else:
            yield self

    def total_size(self) -> int:
        return sum(f.size or 0 for f in self.walk_files())


def safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(". ")
    if not name:
        name = "_"
    if name.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                                        *(f"LPT{i}" for i in range(1, 10))}:
        name = "_" + name
    return name[:200]


# ---------------------------------------------------------------- API 오류 처리
def _api_error(resp: requests.Response) -> Exception:
    reason, message = "", ""
    try:
        err = resp.json().get("error", {})
        message = err.get("message", "")
        errs = err.get("errors") or []
        if errs:
            reason = errs[0].get("reason", "")
    except ValueError:
        message = resp.text[:200]
    code = resp.status_code
    if reason in ("downloadQuotaExceeded",) or "quota" in message.lower() and code == 403 \
            and "download" in message.lower():
        return QuotaExceeded("구글 다운로드 할당량 초과 (많은 사람이 받은 파일)")
    if reason == "cannotDownloadAbusiveFile":
        return _Abusive()
    if code == 429 or reason in ("rateLimitExceeded", "userRateLimitExceeded", "backendError"):
        ra = resp.headers.get("Retry-After")
        return TransientError(f"요청 제한 ({code})", float(ra) if ra and ra.isdigit() else None,
                              throttle=code == 429 or "RateLimit" in reason or reason == "rateLimitExceeded")
    if code >= 500:
        return TransientError(f"구글 서버 오류 ({code})")
    if code == 401:
        return _Unauthorized()
    if code == 404:
        return DownloadError("파일을 찾을 수 없거나 접근 권한이 없습니다")
    if code == 403:
        if reason in ("insufficientFilePermissions", "forbidden", "appNotAuthorizedToFile"):
            return DownloadError("이 파일에 접근할 권한이 없습니다")
        if "copy" in message.lower() or "download" in message.lower():
            return DownloadError(f"소유자가 다운로드를 막아둔 파일입니다 ({message})")
        return DownloadError(f"권한 오류: {message or reason}")
    return DownloadError(f"오류 {code}: {message or reason}")


class _Abusive(Exception):
    pass


class _Unauthorized(Exception):
    pass


class DriveAPI:
    """로그인 토큰 또는 API 키로 Drive API를 부른다."""

    def __init__(self, auth=None, api_key: str = ""):
        self.auth = auth
        self.api_key = api_key.strip()

    @property
    def usable(self) -> bool:
        return (self.auth is not None and self.auth.logged_in) or bool(self.api_key)

    def _headers(self, rk_pairs: dict[str, str] | None = None) -> dict:
        h = {}
        if self.auth is not None and self.auth.logged_in:
            h.update(self.auth.headers())
        if rk_pairs:
            h["X-Goog-Drive-Resource-Keys"] = ",".join(f"{k}/{v}" for k, v in rk_pairs.items() if v)
        return h

    def _params(self, p: dict) -> dict:
        p = dict(p)
        if not (self.auth is not None and self.auth.logged_in) and self.api_key:
            p["key"] = self.api_key
        return p

    def request(self, method: str, path: str, params: dict | None = None,
                rk: dict[str, str] | None = None, json_body=None, session=None,
                stream=False, extra_headers=None) -> requests.Response:
        sess = session or requests
        for attempt in range(6):
            h = self._headers(rk)
            if extra_headers:
                h.update(extra_headers)
            try:
                r = sess.request(method, API + path, params=self._params(params or {}), headers=h,
                                 json=json_body, timeout=TIMEOUT, stream=stream)
            except requests.RequestException as e:
                if attempt == 5:
                    raise TransientError(str(e))
                threading.Event().wait(1.5 ** attempt)
                continue
            if r.status_code < 400:
                return r
            err = _api_error(r)
            r.close()
            if isinstance(err, _Unauthorized) and self.auth is not None and self.auth.logged_in and attempt < 2:
                self.auth.force_refresh()
                continue
            if isinstance(err, TransientError) and stream:
                raise err   # 다운로드 요청은 엔진이 재시도하면서 연결 수도 조절한다
            if isinstance(err, TransientError) and attempt < 5:
                threading.Event().wait(err.wait or 1.5 ** (attempt + 1))
                continue
            if isinstance(err, _Unauthorized):
                raise DownloadError("로그인이 만료되었습니다. 다시 로그인해 주세요")
            raise err
        raise TransientError("요청 실패")

    def get(self, fid: str, rk: str = "") -> dict:
        r = self.request("GET", f"/files/{fid}", {"fields": FIELDS, "supportsAllDrives": "true"},
                         rk={fid: rk} if rk else None)
        return r.json()

    def children(self, fid: str, rk: str = "") -> list[dict]:
        out, token = [], None
        while True:
            p = {
                "q": f"'{fid}' in parents and trashed = false",
                "fields": f"nextPageToken,files({FIELDS})",
                "pageSize": 1000,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
                "orderBy": "folder,name_natural",
            }
            if token:
                p["pageToken"] = token
            data = self.request("GET", "/files", p, rk={fid: rk} if rk else None).json()
            out.extend(data.get("files", []))
            token = data.get("nextPageToken")
            if not token:
                return out

    def copy(self, fid: str, rk: str, name: str) -> dict:
        r = self.request("POST", f"/files/{fid}/copy",
                         {"supportsAllDrives": "true", "fields": "id,size,md5Checksum"},
                         rk={fid: rk} if rk else None,
                         json_body={"name": name, "description": "DRIVEPULL 임시 사본 (자동 삭제)"})
        return r.json()

    def delete(self, fid: str) -> None:
        try:
            self.request("DELETE", f"/files/{fid}", {"supportsAllDrives": "true"})
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- 목록 (API)
def _item_from_meta(m: dict) -> DriveItem:
    mime = m.get("mimeType", "")
    it = DriveItem(
        id=m["id"], name=safe_name(m.get("name", m["id"])), mime=mime,
        size=int(m["size"]) if m.get("size") is not None else None,
        md5=m.get("md5Checksum"), resource_key=m.get("resourceKey", ""),
        is_folder=mime == FOLDER_MIME, mode="api",
    )
    if m.get("thumbnailLink"):
        # 기본 220px 썸네일 주소의 크기 부분만 바꿔서 큰 이미지로 받는다
        it.thumb_url = re.sub(r"=s\d+$", "=s640", m["thumbnailLink"])
    md = m.get("videoMediaMetadata") or m.get("imageMediaMetadata") or {}
    it.width = int(md.get("width") or 0)
    it.height = int(md.get("height") or 0)
    it.duration_ms = int(md.get("durationMillis") or 0)
    if mime in EXPORTS:
        it.export_mime, ext = EXPORTS[mime]
        if not it.name.lower().endswith(ext):
            it.name += ext
    elif mime.startswith("application/vnd.google-apps.") and not it.is_folder and mime != SHORTCUT_MIME:
        it.unsupported = True
    return it


def list_api(api: DriveAPI, link: DriveLink, progress: Callable[[int], None] | None = None,
             cancel: threading.Event | None = None) -> DriveItem:
    root_meta = api.get(link.id, link.resource_key)
    root = _resolve_shortcut(api, _item_from_meta(root_meta), root_meta)
    if not root.is_folder:
        return root
    count = [0]
    lock = threading.Lock()

    def fill(folder: DriveItem, pool: ThreadPoolExecutor) -> list:
        if cancel is not None and cancel.is_set():
            return []
        metas = api.children(folder.id, folder.resource_key)
        futs = []
        for m in metas:
            it = _item_from_meta(m)
            if m.get("mimeType") == SHORTCUT_MIME:
                it = _resolve_shortcut(api, it, m)
                if it is None:
                    continue
            folder.children.append(it)
            if it.is_folder:
                futs.append(pool.submit(fill, it, pool))
        with lock:
            count[0] += len(metas)
            if progress:
                progress(count[0])
        return futs

    with ThreadPoolExecutor(max_workers=8) as pool:
        pending = [pool.submit(fill, root, pool)]
        while pending:
            f = pending.pop()
            pending.extend(f.result())
    return root


def _resolve_shortcut(api: DriveAPI, it: DriveItem, meta: dict) -> Optional[DriveItem]:
    if meta.get("mimeType") != SHORTCUT_MIME:
        return it
    tid = (meta.get("shortcutDetails") or {}).get("targetId")
    if not tid:
        return None
    try:
        tm = api.get(tid, (meta.get("shortcutDetails") or {}).get("targetResourceKey", ""))
    except Exception:  # noqa: BLE001
        return None
    t = _item_from_meta(tm)
    t.name = safe_name(meta.get("name") or t.name)
    if t.export_mime:
        ext = EXPORTS[tm["mimeType"]][1]
        if not t.name.lower().endswith(ext):
            t.name += ext
    return t


# ---------------------------------------------------------------- 목록 (로그인 없이 공개 링크)
_ENTRY = re.compile(
    r'<div class="flip-entry" id="entry-([A-Za-z0-9_-]+)".*?<a href="([^"]+)".*?'
    r'<div class="flip-entry-title">(.*?)</div>', re.S)


def _public_folder(sess: requests.Session, fid: str, rk: str) -> tuple[str, list[tuple[str, str, str, bool]]]:
    params = {"id": fid}
    if rk:
        params["resourcekey"] = rk
    r = sess.get("https://drive.google.com/embeddedfolderview", params=params, timeout=TIMEOUT)
    if r.status_code == 404:
        raise DownloadError("폴더를 찾을 수 없습니다. 링크가 공개 상태인지 확인하거나 구글 로그인 후 다시 시도하세요")
    if r.status_code >= 400 or "accounts.google.com" in r.url:
        raise DownloadError("공개 폴더가 아닙니다. 구글 로그인 후 다시 시도하세요")
    m = re.search(r"<title>(.*?)</title>", r.text, re.S)
    title = html.unescape(m.group(1)).strip() if m else fid
    entries = []
    for eid, href, name in _ENTRY.findall(r.text):
        href = html.unescape(href)
        crk = (parse_qs(urlparse(href).query).get("resourcekey") or [""])[0]
        entries.append((eid, html.unescape(re.sub("<[^>]+>", "", name)).strip(), crk, "/folders/" in href))
    return title, entries


def public_thumb(fid: str, rk: str = "") -> str:
    u = f"https://drive.google.com/thumbnail?id={fid}&sz=w640"
    return u + (f"&resourcekey={rk}" if rk else "")


def list_public(link: DriveLink, progress: Callable[[int], None] | None = None,
                cancel: threading.Event | None = None) -> DriveItem:
    sess = make_session()
    if link.kind == "file":
        src = PublicSource(link.id, link.resource_key)
        pr = src.probe(sess)
        return DriveItem(id=link.id, name=safe_name(pr.filename or link.id), size=pr.size, mode="public",
                         resource_key=link.resource_key, thumb_url=public_thumb(link.id, link.resource_key))
    title, entries = _public_folder(sess, link.id, link.resource_key)
    root = DriveItem(id=link.id, name=safe_name(title), is_folder=True, mode="public",
                     resource_key=link.resource_key)
    count = [0]
    stack = [(root, entries)]
    while stack:
        if cancel is not None and cancel.is_set():
            break
        folder, ents = stack.pop()
        for eid, name, crk, is_dir in ents:
            it = DriveItem(id=eid, name=safe_name(name), is_folder=is_dir, mode="public", resource_key=crk,
                           thumb_url="" if is_dir else public_thumb(eid, crk))
            folder.children.append(it)
            if is_dir:
                _, sub = _public_folder(sess, eid, crk)
                stack.append((it, sub))
        count[0] += len(ents)
        if progress:
            progress(count[0])
    return root


def probe_public_sizes(items: list[DriveItem], on_item: Callable[[DriveItem], None],
                       cancel: threading.Event | None = None) -> None:
    """로그인 없는 모드에서는 목록에 용량이 없어서 파일마다 확인한다."""
    def one(it: DriveItem) -> None:
        if cancel is not None and cancel.is_set():
            return
        try:
            sess = make_session()
            pr = PublicSource(it.id, it.resource_key).probe(sess)
            it.size = pr.size
            sess.close()
        except Exception:  # noqa: BLE001
            return
        on_item(it)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(one, items))


# ---------------------------------------------------------------- 다운로드 소스
def _filename_from_cd(cd: str) -> Optional[str]:
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd)
    if m:
        from urllib.parse import unquote
        return unquote(m.group(1))
    m = re.search(r'filename="([^"]+)"', cd)
    return m.group(1) if m else None


def _probe_from_response(r: requests.Response) -> ProbeResult:
    name = _filename_from_cd(r.headers.get("Content-Disposition", ""))
    if r.status_code == 206:
        cr = r.headers.get("Content-Range", "")
        m = re.match(r"bytes \d+-\d+/(\d+)", cr)
        return ProbeResult(int(m.group(1)) if m else None, bool(m), name)
    cl = r.headers.get("Content-Length")
    return ProbeResult(int(cl) if cl and cl.isdigit() else None, False, name)


class ApiSource:
    def __init__(self, api: DriveAPI, fid: str, rk: str = "", export_mime: str | None = None):
        self.api = api
        self.fid = fid
        self.rk = rk
        self.export_mime = export_mime
        self.ack_abuse = False

    def _path_params(self):
        if self.export_mime:
            return f"/files/{self.fid}/export", {"mimeType": self.export_mime}
        p = {"alt": "media", "supportsAllDrives": "true"}
        if self.ack_abuse:
            p["acknowledgeAbuse"] = "true"
        return f"/files/{self.fid}", p

    def probe(self, sess) -> ProbeResult:
        if not self.api.usable:
            raise DownloadError("구글 로그인이 필요합니다")
        if self.export_mime:
            return ProbeResult(None, False)
        r = self.open(sess, 0, 0)
        try:
            return _probe_from_response(r)
        finally:
            r.close()

    def open(self, sess, start, end) -> requests.Response:
        extra = {}
        if start is not None:
            extra["Range"] = f"bytes={start}-{'' if end is None else end}"
        for _ in range(2):
            path, params = self._path_params()
            try:
                return self.api.request("GET", path, params, rk={self.fid: self.rk} if self.rk else None,
                                        session=sess, stream=True, extra_headers=extra)
            except _Abusive:
                self.ack_abuse = True  # 바이러스 검사 경고 파일: 확인 후 받기
        raise DownloadError("구글이 악성 파일로 표시해 다운로드를 막았습니다")


QUOTA_PHRASES = (
    "too many users have viewed or downloaded this file recently",
    "download quota for this file has been exceeded",
    "exceeds the download quota",
    "최근에 너무 많은 사용자가",
    "다운로드 할당량을 초과",
)


def _save_unknown_page(r: requests.Response, text: str) -> None:
    try:
        from .config import app_dir
        (app_dir() / "last_google_page.html").write_text(
            f"<!-- {r.status_code} {r.url} -->\n" + text[:200000], encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


class PublicSource:
    """로그인 없이 공개 링크로 받기 (drive.usercontent.google.com).

    로그인 없는 다운로드는 요청이 많으면 구글이 파일을 일시 차단하므로
    연결 수와 구간 재분할을 줄여서 요청 횟수를 낮춘다.
    """
    max_connections = 6
    min_split = 256 << 20

    def __init__(self, fid: str, rk: str = ""):
        self.fid = fid
        self.rk = rk
        self.url: Optional[str] = None
        self.cookies: dict = {}
        self._lock = threading.Lock()

    def _base_url(self) -> str:
        p = {"id": self.fid, "export": "download", "confirm": "t"}
        if self.rk:
            p["resourcekey"] = self.rk
        return USERCONTENT + "?" + urlencode(p)

    def _check_html(self, r: requests.Response) -> Optional[str]:
        """HTML이 오면 원인 판단 후, 확인 폼이면 진짜 다운로드 URL을 돌려준다."""
        text = r.text
        low = text.lower()
        if any(p in low for p in QUOTA_PHRASES):
            raise QuotaExceeded("구글 다운로드 제한 (짧은 시간에 요청이 많았던 파일)")
        if "accounts.google.com" in r.url or "servicelogin" in low:
            raise DownloadError("공개 파일이 아닙니다. 구글 로그인 후 다시 시도하세요")
        m = re.search(r'<form[^>]+id="download-form"[^>]+action="([^"]+)"', text)
        if m:
            action = html.unescape(m.group(1))
            inputs = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', text))
            return action + "?" + urlencode(inputs)
        if r.status_code == 404:
            raise DownloadError("파일을 찾을 수 없습니다")
        # 모르는 페이지: 진단용으로 저장하고 잠시 후 재시도 (일시적인 차단 페이지일 수 있음)
        _save_unknown_page(r, text)
        m = re.search(r"<title>(.*?)</title>", text, re.S)
        title = html.unescape(m.group(1)).strip()[:60] if m else f"HTTP {r.status_code}"
        raise TransientError(f"구글이 다운로드 대신 안내 페이지를 보냄 ({title})", wait=30, throttle=True)

    def _get(self, sess, url, start, end) -> requests.Response:
        h = {}
        if start is not None:
            h["Range"] = f"bytes={start}-{'' if end is None else end}"
        try:
            r = sess.get(url, headers=h, cookies=self.cookies, stream=True, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise TransientError(str(e))
        if r.status_code == 429 or r.status_code >= 500:
            r.close()
            raise TransientError(f"서버 응답 {r.status_code}", throttle=r.status_code == 429)
        return r

    def _resolve(self, sess, start, end) -> requests.Response:
        url = self._base_url()
        for _ in range(3):
            r = self._get(sess, url, start, end)
            if "text/html" not in r.headers.get("Content-Type", ""):
                with self._lock:
                    self.url = url
                    self.cookies.update(sess.cookies.get_dict())
                return r
            try:
                url = self._check_html(r)
            finally:
                r.close()
            self.cookies.update(sess.cookies.get_dict())
        raise DownloadError("다운로드 링크를 얻지 못했습니다")

    def probe(self, sess) -> ProbeResult:
        r = self._resolve(sess, 0, 0)
        try:
            return _probe_from_response(r)
        finally:
            r.close()

    def open(self, sess, start, end) -> requests.Response:
        if self.url is None:
            return self._resolve(sess, start, end)
        r = self._get(sess, self.url, start, end)
        if "text/html" in r.headers.get("Content-Type", ""):
            r.close()
            with self._lock:
                self.url = None
            raise TransientError("다운로드 링크 만료, 다시 연결")
        return r
