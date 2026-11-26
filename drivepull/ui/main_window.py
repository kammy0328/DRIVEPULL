from __future__ import annotations

import os
import subprocess
import sys
import threading

from PySide6.QtCore import QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFrame,
                               QToolButton,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu,
                               QMessageBox, QPushButton, QStackedWidget, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from ..config import APP_NAME, APP_VERSION, Settings
from ..drive import DriveItem, DriveLink, list_api, list_public, parse_link, probe_public_sizes
from ..engine import STATUS_LABEL, UNFINISHED, DownloadError
from ..service import Service
from . import style
from .grid import ThumbGrid, info_text, sort_value
from .settings_dialog import SettingsDialog
from .thumbs import ThumbLoader
from .widgets import (PROGRESS_ROLE, STATUS_ROLE, FadeIn, ProgressDelegate, Spinner, fmt_eta,
                      fmt_size, fmt_speed, make_icon, natural_key)

ITEM_ROLE = Qt.UserRole + 1
SORT_ROLE = Qt.UserRole + 30
SORT_KEYS = [("name", "이름"), ("size", "크기"), ("type", "종류"), ("modified", "수정일")]
SORT_COLS = {"name": 0, "info": 1, "size": 2, "type": 3, "modified": 4}
COL_KEYS = {v: k for k, v in SORT_COLS.items()}
RUNNING = ("downloading", "starting", "verifying")


class FileItem(QTreeWidgetItem):
    """폴더는 항상 위, 이름은 숫자를 숫자로 비교 (A2 < A10)."""

    def __lt__(self, other):
        tw = self.treeWidget()
        desc = tw.header().sortIndicatorOrder() == Qt.DescendingOrder if tw else False
        a, b = self.data(0, ITEM_ROLE), other.data(0, ITEM_ROLE)
        if a is None or b is None:
            return False
        if a.is_folder != b.is_folder:
            return a.is_folder != desc
        key = COL_KEYS.get(tw.sortColumn() if tw else 0, "name")
        va, vb = sort_value(a, key), sort_value(b, key)
        if va == vb:
            return natural_key(a.name) < natural_key(b.name)
        return va < vb


class DlItem(QTreeWidgetItem):
    def __lt__(self, other):
        tw = self.treeWidget()
        col = tw.sortColumn() if tw else 0
        va, vb = self.data(col, SORT_ROLE), other.data(col, SORT_ROLE)
        if va is None or vb is None or va == vb:
            return natural_key(self.text(0)) < natural_key(other.text(0))
        return va < vb


class RowActions(QWidget):
    """다운로드 목록 각 줄의 일시정지/재개, 취소, 삭제 버튼."""

    clicked = Signal(str, str)   # action, uid

    def __init__(self, uid: str):
        super().__init__()
        self.uid = uid
        self.status = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(2)
        self.toggle = self._btn("toggle")
        self.cancel = self._btn("cancel", "cancel", "취소 (받던 임시 파일 삭제)")
        self.delete = self._btn("delete", "trash", "목록에서 삭제")
        for b in (self.toggle, self.cancel, self.delete):
            sp = b.sizePolicy()
            sp.setRetainSizeWhenHidden(True)   # 숨겨도 버튼 위치가 밀리지 않게
            b.setSizePolicy(sp)
            lay.addWidget(b)
        lay.addStretch(1)

    def _btn(self, action, icon=None, tip=""):
        b = QToolButton()
        b.setAutoRaise(True)
        b.setCursor(Qt.PointingHandCursor)
        b.setIconSize(QSize(16, 16))
        b.setFixedSize(28, 26)
        if icon:
            b.setIcon(make_icon(icon))
        b.setToolTip(tip)
        b.clicked.connect(lambda: self.clicked.emit(action, self.uid))
        return b

    def set_status(self, status: str):
        if status == self.status:
            return
        self.status = status
        if status in RUNNING or status in ("queued", "waiting"):
            self.toggle.setIcon(make_icon("pause"))
            self.toggle.setToolTip("일시정지")
        elif status in ("done", "skipped"):
            self.toggle.setIcon(make_icon("folder"))
            self.toggle.setToolTip("폴더 열기")
        elif status in ("error", "canceled"):
            self.toggle.setIcon(make_icon("retry"))
            self.toggle.setToolTip("다시 받기" if status == "canceled" else "다시 시도")
        else:
            self.toggle.setIcon(make_icon("play"))
            self.toggle.setToolTip("재개")
        self.cancel.setVisible(status in UNFINISHED)
        self.delete.setToolTip("목록에서 삭제 (받은 파일은 그대로)" if status in ("done", "skipped")
                               else "목록에서 삭제 (받던 임시 파일도 삭제)")


class Bridge(QObject):
    listed = Signal(object, str, int)        # root, error, request id
    count = Signal(int, int)                 # 찾은 항목 수, request id
    size_found = Signal(str, object)         # drive id, size
    login_done = Signal(str)                 # error ("" = 성공)


def _ext(name: str) -> str:
    e = os.path.splitext(name)[1].lstrip(".").upper()
    return e[:6] if e else "파일"


class MainWindow(QMainWindow):
    def __init__(self, service: Service, settings: Settings):
        super().__init__()
        self.svc = service
        self.s = settings
        self.bridge = Bridge()
        self.bridge.listed.connect(self._on_listed)
        self.bridge.count.connect(self._on_count)
        self.bridge.size_found.connect(self._on_size)
        self.bridge.login_done.connect(self._on_login_done)
        self.root_item: DriveItem | None = None
        self._req = 0
        self._cancel: threading.Event | None = None
        self._id_to_items: dict[str, list[QTreeWidgetItem]] = {}
        self._dl_rows: dict[str, QTreeWidgetItem] = {}
        self._prev_text = ""
        self.loader = ThumbLoader(lambda: self.svc.auth.headers())

        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.resize(1120, 760)
        self.setMinimumSize(860, 560)
        self._build()
        self._update_account()
        self._summary_timer = QTimer(self, singleShot=True, interval=80, timeout=self._update_summary)
        self._folder_timer = QTimer(self, singleShot=True, interval=300, timeout=self._recalc_folder_sizes)
        self._dl_timer = QTimer(self, interval=400, timeout=self._refresh_downloads)
        self._dl_timer.start()
        self._refresh_downloads()
        if self.svc.manager.partial_tasks():
            if self.s.cleanup_on_exit:
                QTimer.singleShot(400, self._ask_leftovers)
            else:
                self._toast("이전에 받던 파일이 있어요. 다운로드 탭에서 이어받을 수 있어요.")

    # ================================================================ 레이아웃
    def _build(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(22, 16, 22, 16)
        v.setSpacing(14)

        # 상단 바
        top = QHBoxLayout()
        logo = QLabel("DRIVEPULL")
        logo.setObjectName("logo")
        dot = QLabel("●")
        dot.setObjectName("logoDot")
        top.addWidget(logo)
        top.addWidget(dot)
        top.addSpacing(18)
        segw = QFrame()
        segw.setObjectName("segWrap")
        sl = QHBoxLayout(segw)
        sl.setContentsMargins(3, 3, 3, 3)
        sl.setSpacing(2)
        self.seg_files = QPushButton("파일 선택")
        self.seg_dl = QPushButton("다운로드")
        self.seg_group = QButtonGroup(self)
        for i, b in enumerate((self.seg_files, self.seg_dl)):
            b.setObjectName("seg")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            self.seg_group.addButton(b, i)
            sl.addWidget(b)
        self.seg_files.setChecked(True)
        self.seg_group.idClicked.connect(self._switch_page)
        top.addWidget(segw)
        top.addStretch(1)
        self.login_spin = Spinner(16)
        top.addWidget(self.login_spin)
        self.account_btn = QPushButton()
        self.account_btn.setObjectName("pill")
        self.account_btn.setCursor(Qt.PointingHandCursor)
        self.account_btn.clicked.connect(self._account_clicked)
        top.addWidget(self.account_btn)
        gear = QPushButton("설정")
        gear.setObjectName("ghost")
        gear.setCursor(Qt.PointingHandCursor)
        gear.clicked.connect(self._open_settings)
        top.addWidget(gear)
        v.addLayout(top)

        # 링크 입력
        link_row = QHBoxLayout()
        link_row.setSpacing(10)
        self.link = QLineEdit()
        self.link.setObjectName("linkInput")
        self.link.setPlaceholderText("구글 드라이브 폴더 또는 파일 링크를 붙여넣으세요")
        self.link.setClearButtonEnabled(True)
        self.link.returnPressed.connect(self._load)
        self.link.textEdited.connect(self._on_link_edited)
        link_row.addWidget(self.link, 1)
        self.load_btn = QPushButton("불러오기")
        self.load_btn.setObjectName("primary")
        self.load_btn.setMinimumHeight(44)
        self.load_btn.setMinimumWidth(110)
        self.load_btn.setCursor(Qt.PointingHandCursor)
        self.load_btn.clicked.connect(self._load)
        link_row.addWidget(self.load_btn)
        v.addLayout(link_row)

        self.msg = QLabel("")
        self.msg.setObjectName("muted")
        self.msg.setWordWrap(True)
        self.msg.hide()
        v.addWidget(self.msg)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_files_page())
        self.pages.addWidget(self._build_dl_page())
        v.addWidget(self.pages, 1)
        self._fades = [FadeIn(self.pages.widget(0)), FadeIn(self.pages.widget(1))]

    def _build_files_page(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        head = QHBoxLayout()
        self.root_lbl = QLabel("링크를 불러오면 파일 목록이 여기에 표시됩니다")
        self.root_lbl.setObjectName("sectionTitle")
        head.addWidget(self.root_lbl)
        self.list_spin = Spinner(16)
        head.addWidget(self.list_spin)
        self.count_lbl = QLabel("")
        self.count_lbl.setObjectName("muted")
        head.addWidget(self.count_lbl)
        head.addStretch(1)
        vw = QFrame()
        vw.setObjectName("segWrap")
        vl = QHBoxLayout(vw)
        vl.setContentsMargins(3, 3, 3, 3)
        vl.setSpacing(2)
        self.view_group = QButtonGroup(self)
        for i, text in enumerate(("목록", "썸네일")):
            b = QPushButton(text)
            b.setObjectName("seg")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            self.view_group.addButton(b, i)
            vl.addWidget(b)
        self.view_group.idClicked.connect(self._set_view)
        head.addWidget(vw)
        self.sort_combo = QComboBox()
        for k, label in SORT_KEYS:
            self.sort_combo.addItem(f"{label}순", k)
        self.sort_combo.setFixedWidth(96)
        self.sort_combo.setToolTip("정렬 기준")
        self.sort_dir = QToolButton()
        self.sort_dir.setAutoRaise(True)
        self.sort_dir.setCursor(Qt.PointingHandCursor)
        self.sort_dir.setFixedSize(32, 32)
        self.sort_dir.setIconSize(QSize(16, 16))
        self.sort_dir.setIcon(make_icon("desc" if self.s.sort_desc else "asc"))
        idx = max(0, [k for k, _ in SORT_KEYS].index(self.s.sort_key) if self.s.sort_key in dict(SORT_KEYS) else 0)
        self.sort_combo.setCurrentIndex(idx)
        self.sort_combo.currentIndexChanged.connect(lambda *_: self._apply_sort(save=True))
        self.sort_dir.clicked.connect(self._flip_sort)
        head.addWidget(self.sort_combo)
        head.addWidget(self.sort_dir)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("이름·확장자 검색 (예: .mov)")
        self.filter.setFixedWidth(240)
        self.filter.textChanged.connect(self._apply_filter)
        head.addWidget(self.filter)
        b_all = QPushButton("전체 선택")
        b_all.clicked.connect(lambda: self._check_all(True))
        b_none = QPushButton("선택 해제")
        b_none.clicked.connect(lambda: self._check_all(False))
        head.addWidget(b_all)
        head.addWidget(b_none)
        v.addLayout(head)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["이름", "정보", "크기", "종류", "수정일"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setAnimated(True)
        self.tree.setTextElideMode(Qt.ElideMiddle)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        hh = self.tree.header()
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for col, wd in ((1, 150), (2, 100), (3, 80), (4, 100)):
            hh.setSectionResizeMode(col, QHeaderView.Fixed)
            self.tree.setColumnWidth(col, wd)
        self.tree.itemChanged.connect(lambda *_: self._summary_timer.start())
        hh.setSortIndicatorShown(True)
        hh.setSectionsClickable(True)
        hh.sortIndicatorChanged.connect(self._on_tree_sort_clicked)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        space = QAction(self.tree)
        space.setShortcut(QKeySequence(Qt.Key_Space))
        space.triggered.connect(self._toggle_selected)
        self.tree.addAction(space)
        self.grid = ThumbGrid(self.loader)
        self.view_stack = QStackedWidget()
        self.view_stack.addWidget(self.tree)
        self.view_stack.addWidget(self.grid)
        v.addWidget(self.view_stack, 1)
        view = 1 if self.s.extra.get("view") == "grid" else 0
        self.view_group.button(view).setChecked(True)
        self.view_stack.setCurrentIndex(view)

        bottom = QFrame()
        bottom.setObjectName("card")
        bl = QHBoxLayout(bottom)
        bl.setContentsMargins(14, 10, 10, 10)
        bl.setSpacing(10)
        lbl = QLabel("저장 위치")
        lbl.setObjectName("muted")
        bl.addWidget(lbl)
        self.dest_lbl = QLabel(self.s.last_save_dir)
        self.dest_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        bl.addWidget(self.dest_lbl, 1)
        ch = QPushButton("변경")
        ch.clicked.connect(self._choose_dest)
        bl.addWidget(ch)
        self.keep_struct = QCheckBox("폴더 구조 유지")
        self.keep_struct.setChecked(self.s.keep_folder_structure)
        self.keep_struct.toggled.connect(self._keep_struct_changed)
        bl.addWidget(self.keep_struct)
        self.sel_lbl = QLabel("")
        self.sel_lbl.setObjectName("muted")
        bl.addWidget(self.sel_lbl)
        self.start_btn = QPushButton("다운로드 시작")
        self.start_btn.setObjectName("primary")
        self.start_btn.setMinimumHeight(38)
        self.start_btn.setMinimumWidth(140)
        self.start_btn.setEnabled(False)
        self.start_btn.setCursor(Qt.PointingHandCursor)
        self.start_btn.clicked.connect(self._start_download)
        bl.addWidget(self.start_btn)
        v.addWidget(bottom)
        return w

    def _build_dl_page(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)
        head = QHBoxLayout()
        t = QLabel("다운로드")
        t.setObjectName("sectionTitle")
        head.addWidget(t)
        self.total_lbl = QLabel("")
        self.total_lbl.setObjectName("muted")
        head.addSpacing(8)
        head.addWidget(self.total_lbl)
        head.addStretch(1)
        for text, fn in (("모두 재개", self.svc.manager.resume_all),
                         ("모두 일시정지", self.svc.manager.pause_all),
                         ("완료 항목 지우기", self.svc.manager.clear_finished)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            head.addWidget(b)
        v.addLayout(head)

        self.dl = QTreeWidget()
        self.dl.setRootIsDecorated(False)
        self.dl.setHeaderLabels(["이름", "크기", "진행률", "속도", "남은 시간", "상태", ""])
        self.dl.setUniformRowHeights(True)
        self.dl.setTextElideMode(Qt.ElideMiddle)
        self.dl.setAlternatingRowColors(True)
        self.dl.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.dl.setItemDelegateForColumn(2, ProgressDelegate(self.dl))
        hh = self.dl.header()
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for col, wd in ((1, 90), (2, 200), (3, 100), (4, 90), (5, 170), (6, 104)):
            hh.setSectionResizeMode(col, QHeaderView.Fixed)
            self.dl.setColumnWidth(col, wd)
        # 머리글을 누르면 정렬 (처음엔 추가한 순서)
        hh.setSortIndicator(-1, Qt.AscendingOrder)
        hh.setSortIndicatorShown(True)
        self.dl.setSortingEnabled(True)
        self.dl.setContextMenuPolicy(Qt.CustomContextMenu)
        self.dl.customContextMenuRequested.connect(self._dl_menu)
        self.dl.itemDoubleClicked.connect(lambda it, _: self._open_folder([it.data(0, ITEM_ROLE)]))
        v.addWidget(self.dl, 1)

        self.empty_dl = QLabel("다운로드할 파일을 선택하면 여기에 표시됩니다")
        self.empty_dl.setObjectName("muted")
        self.empty_dl.setAlignment(Qt.AlignCenter)
        v.addWidget(self.empty_dl)
        return w

    # ================================================================ 공통
    def _switch_page(self, i: int):
        if self.pages.currentIndex() != i:
            self.pages.setCurrentIndex(i)
            self._fades[i].play()
        self.seg_group.button(i).setChecked(True)

    def _toast(self, text: str, error: bool = False):
        self.msg.setText(text)
        self.msg.setStyleSheet(f"color: {style.ERR if error else style.MUTED};")
        self.msg.show()
        if not error:
            QTimer.singleShot(6000, lambda: self.msg.text() == text and self.msg.hide())

    # ================================================================ 계정
    def _update_account(self):
        a = self.svc.auth
        if a.logged_in:
            self.account_btn.setText(a.email or "로그인됨")
            self.account_btn.setStyleSheet(f"border-color: {style.OK}55;")
        else:
            self.account_btn.setText("구글 로그인")
            self.account_btn.setStyleSheet("")

    def _account_clicked(self):
        a = self.svc.auth
        if a.logged_in:
            m = QMenu(self)
            info = m.addAction(f"{a.name or ''} {a.email}".strip())
            info.setEnabled(False)
            m.addSeparator()
            out = m.addAction("로그아웃")
            act = m.exec(self.account_btn.mapToGlobal(self.account_btn.rect().bottomLeft()))
            if act == out:
                a.logout()
                self._update_account()
            return
        if not a.has_client():
            r = QMessageBox.question(
                self, "OAuth 클라이언트 필요",
                "구글 로그인에는 Google Cloud에서 만든 OAuth 클라이언트(데스크톱 앱) JSON이 필요합니다.\n"
                "지금 client_secret.json 파일을 불러올까요?")
            if r != QMessageBox.Yes:
                return
            from ..auth import import_client_secret
            p, _ = QFileDialog.getOpenFileName(self, "OAuth 클라이언트 JSON", "", "JSON (*.json)")
            if not p:
                return
            try:
                import_client_secret(p)
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, "불러오기 실패", str(e))
                return
        self.account_btn.setEnabled(False)
        self.account_btn.setText("브라우저에서 로그인하세요")
        self.login_spin.start()

        def run():
            try:
                a.login()
                self.bridge.login_done.emit("")
            except Exception as e:  # noqa: BLE001
                self.bridge.login_done.emit(str(e) or e.__class__.__name__)

        threading.Thread(target=run, daemon=True).start()

    def _on_login_done(self, err: str):
        self.login_spin.stop()
        self.account_btn.setEnabled(True)
        self._update_account()
        if err:
            self._toast(f"로그인 실패: {err}", error=True)
        else:
            self._toast(f"{self.svc.auth.email} 계정으로 로그인했어요")

    def _open_settings(self):
        if SettingsDialog(self.s, self).exec():
            self._toast("설정을 저장했어요. 새 다운로드부터 적용돼요.")

    # ================================================================ 목록 불러오기
    def _on_link_edited(self, text: str):
        pasted = len(text) - len(self._prev_text) > 15
        self._prev_text = text
        if pasted and parse_link(text):
            self._load()

    def _load(self):
        text = self.link.text().strip()
        link = parse_link(text)
        if link is None:
            self._toast("구글 드라이브 링크가 아니에요. 공유 링크를 그대로 붙여넣어 주세요.", error=True)
            return
        if self._cancel:
            self._cancel.set()
        self._cancel = threading.Event()
        cancel = self._cancel
        self._req += 1
        req = self._req
        self.msg.hide()
        self._switch_page(0)
        self.grid.set_root(None)
        self.tree.clear()
        self._id_to_items.clear()
        self.root_item = None
        self.root_lbl.setText("목록 불러오는 중")
        self.count_lbl.setText("")
        self.list_spin.start()
        self.load_btn.setEnabled(False)
        self.start_btn.setEnabled(False)
        self.sel_lbl.setText("")
        api = self.svc.api()

        def run():
            try:
                prog = lambda n: self.bridge.count.emit(n, req)  # noqa: E731
                if api.usable:
                    root = list_api(api, link, prog, cancel)
                else:
                    lk = link
                    if link.kind == "unknown":
                        lk = DriveLink("folder", link.id, link.resource_key)
                        try:
                            root = list_public(lk, prog, cancel)
                        except DownloadError:
                            root = list_public(DriveLink("file", link.id, link.resource_key), prog, cancel)
                    else:
                        root = list_public(lk, prog, cancel)
                self.bridge.listed.emit(root, "", req)
                if not api.usable and not cancel.is_set():
                    files = [f for f in root.walk_files() if f.size is None]
                    probe_public_sizes(files, lambda it: self.bridge.size_found.emit(it.id, it.size), cancel)
            except Exception as e:  # noqa: BLE001
                msg = str(e) or e.__class__.__name__
                if not api.usable and not self.svc.auth.logged_in:
                    msg += "  (비공개 폴더라면 오른쪽 위에서 구글 로그인 후 다시 불러오세요)"
                self.bridge.listed.emit(None, msg, req)

        threading.Thread(target=run, daemon=True).start()

    def _on_count(self, n: int, req: int):
        if req == self._req:
            self.count_lbl.setText(f"{n:,}개 찾음")

    def _on_listed(self, root: DriveItem | None, err: str, req: int):
        if req != self._req:
            return
        self.list_spin.stop()
        self.load_btn.setEnabled(True)
        if err or root is None:
            self.root_lbl.setText("목록을 불러오지 못했어요")
            self.count_lbl.setText("")
            self._toast(err, error=True)
            return
        self.root_item = root
        self.grid.set_root(None)
        self.tree.blockSignals(True)
        self.tree.clear()
        self._id_to_items.clear()
        top = self._make_item(root, None)
        self.tree.setSortingEnabled(False)
        self.tree.addTopLevelItem(top)
        top.setExpanded(True)
        self.tree.blockSignals(False)
        files = list(root.walk_files())
        self.root_lbl.setText(root.name)
        mode = "" if root.mode == "api" else " · 공개 링크 모드"
        self.count_lbl.setText(f"파일 {len(files):,}개{mode}")
        self._recalc_folder_sizes()
        self._apply_sort()
        self.grid.set_root(top)
        self._update_summary()
        self._apply_filter(self.filter.text())
        # 영상·이미지가 대부분인 폴더는 처음 한 번 썸네일 보기로 보여준다
        if "view" not in self.s.extra and files:
            from .grid import IMAGE_EXT, is_video
            media = sum(1 for f in files if is_video(f) or _ext(f.name).lower() in IMAGE_EXT)
            if media / len(files) >= 0.6:
                self._set_view(1)

    def _make_item(self, it: DriveItem, parent: QTreeWidgetItem | None) -> QTreeWidgetItem:
        q = FileItem(parent) if parent is not None else FileItem()
        q.setText(0, it.name)
        if it.modified:
            q.setText(4, it.modified[:10])
            q.setForeground(4, QColor(style.MUTED))
            q.setTextAlignment(4, Qt.AlignCenter)
        q.setData(0, ITEM_ROLE, it)
        q.setTextAlignment(2, Qt.AlignRight | Qt.AlignVCenter)
        q.setTextAlignment(3, Qt.AlignCenter)
        if it.is_folder:
            q.setFlags(q.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate)
            q.setText(3, "폴더")
            q.setForeground(3, QColor(style.MUTED))
            for c in it.children:
                self._make_item(c, q)
            q.setCheckState(0, Qt.Checked if it.children else Qt.Unchecked)
        else:
            q.setText(1, info_text(it))
            q.setText(2, fmt_size(it.size))
            q.setText(3, _ext(it.name))
            for col in (1, 2, 3):
                q.setForeground(col, QColor(style.MUTED))
            self._id_to_items.setdefault(it.id, []).append(q)
            if it.unsupported:
                q.setFlags(q.flags() & ~Qt.ItemIsUserCheckable & ~Qt.ItemIsEnabled)
                q.setToolTip(0, "구글 형식이라 받을 수 없는 항목입니다")
            else:
                q.setFlags(q.flags() | Qt.ItemIsUserCheckable)
                q.setCheckState(0, Qt.Checked)
                if it.export_mime:
                    q.setToolTip(0, "구글 문서 → 오피스 파일로 내보내기")
        return q

    def _on_size(self, fid: str, size):
        for q in self._id_to_items.get(fid, []):
            q.setText(2, fmt_size(size))
        self.grid.refresh_checks(sizes_changed=True)
        self._folder_timer.start()
        self._summary_timer.start()

    def _recalc_folder_sizes(self):
        def rec(q: QTreeWidgetItem) -> int:
            it: DriveItem = q.data(0, ITEM_ROLE)
            if not it.is_folder:
                return it.size or 0
            total = sum(rec(q.child(i)) for i in range(q.childCount()))
            it._tot = total
            q.setText(2, fmt_size(total) if total else "-")
            return total

        self.tree.blockSignals(True)
        for i in range(self.tree.topLevelItemCount()):
            rec(self.tree.topLevelItem(i))
        self.tree.blockSignals(False)
        if self.s.sort_key == "size" and self.tree.isSortingEnabled():
            # 공개 링크 모드는 용량이 나중에 채워지므로 다시 정렬
            self.tree.sortItems(self.tree.sortColumn(), self.tree.header().sortIndicatorOrder())
            self.grid.refresh_checks(True)

    # ================================================================ 선택
    def _iter_file_items(self):
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            q = stack.pop()
            it: DriveItem = q.data(0, ITEM_ROLE)
            if it.is_folder:
                stack.extend(q.child(i) for i in range(q.childCount()))
            else:
                yield q, it

    def _selected(self):
        out = []
        for q, it in self._iter_file_items():
            if it.unsupported or q.checkState(0) != Qt.Checked:
                continue
            parts = []
            p = q.parent()
            while p is not None:
                parts.append(p.data(0, ITEM_ROLE).name)
                p = p.parent()
            out.append((it, os.path.join(*reversed(parts)) if parts else ""))
        return out

    def _update_summary(self):
        sel = self._selected()
        total = sum(it.size or 0 for it, _ in sel)
        unknown = sum(1 for it, _ in sel if it.size is None)
        if sel:
            extra = f" (+{unknown}개 용량 확인 중)" if unknown and self.root_item and self.root_item.mode == "public" else ""
            self.sel_lbl.setText(f"{len(sel):,}개 · {fmt_size(total)}{extra}")
        else:
            self.sel_lbl.setText("선택 없음" if self.root_item else "")
        self.start_btn.setEnabled(bool(sel))
        self.grid.refresh_checks()

    def _check_all(self, on: bool):
        state = Qt.Checked if on else Qt.Unchecked
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setCheckState(0, state)

    def _toggle_selected(self):
        items = self.tree.selectedItems()
        if not items:
            return
        new = Qt.Unchecked if items[0].checkState(0) == Qt.Checked else Qt.Checked
        for q in items:
            if q.flags() & Qt.ItemIsUserCheckable:
                q.setCheckState(0, new)

    def _tree_menu(self, pos):
        items = self.tree.selectedItems()
        if not items:
            return
        m = QMenu(self)
        a1 = m.addAction("선택 항목 체크")
        a2 = m.addAction("선택 항목 체크 해제")
        m.addSeparator()
        a3 = m.addAction("모두 펼치기")
        a4 = m.addAction("모두 접기")
        act = m.exec(self.tree.viewport().mapToGlobal(pos))
        if act in (a1, a2):
            for q in items:
                if q.flags() & Qt.ItemIsUserCheckable:
                    q.setCheckState(0, Qt.Checked if act == a1 else Qt.Unchecked)
        elif act == a3:
            self.tree.expandAll()
        elif act == a4:
            self.tree.collapseAll()

    def _apply_sort(self, save: bool = False):
        key = self.sort_combo.currentData() or "name"
        desc = self.s.sort_desc
        self.sort_dir.setIcon(make_icon("desc" if desc else "asc"))
        self.sort_dir.setToolTip("내림차순" if desc else "오름차순")
        order = Qt.DescendingOrder if desc else Qt.AscendingOrder
        hh = self.tree.header()
        hh.blockSignals(True)
        self.tree.setSortingEnabled(True)
        self.tree.sortItems(SORT_COLS[key], order)
        hh.setSortIndicator(SORT_COLS[key], order)
        hh.blockSignals(False)
        self.grid.set_sort(key, desc)
        if save:
            self.s.sort_key = key
            self.s.save()

    def _flip_sort(self):
        self.s.sort_desc = not self.s.sort_desc
        self._apply_sort(save=True)

    def _on_tree_sort_clicked(self, col: int, order):
        key = COL_KEYS.get(col, "name")
        self.s.sort_desc = order == Qt.DescendingOrder
        if key in dict(SORT_KEYS):
            i = [k for k, _ in SORT_KEYS].index(key)
            self.sort_combo.blockSignals(True)
            self.sort_combo.setCurrentIndex(i)
            self.sort_combo.blockSignals(False)
            self.s.sort_key = key
        self.sort_dir.setIcon(make_icon("desc" if self.s.sort_desc else "asc"))
        self.grid.set_sort(key if key in dict(SORT_KEYS) else "name", self.s.sort_desc)
        self.s.save()

    def _set_view(self, i: int):
        self.view_group.button(i).setChecked(True)
        if self.view_stack.currentIndex() != i:
            self.view_stack.setCurrentIndex(i)
        self.s.extra["view"] = "grid" if i == 1 else "list"
        self.s.save()

    def _apply_filter(self, text: str):
        self.grid.set_filter(text)
        text = text.strip().lower()

        def rec(q: QTreeWidgetItem) -> bool:
            it: DriveItem = q.data(0, ITEM_ROLE)
            if it.is_folder:
                vis = False
                for i in range(q.childCount()):
                    vis = rec(q.child(i)) or vis
                vis = vis or not text
                q.setHidden(not vis and q.parent() is not None)
                if text and vis:
                    q.setExpanded(True)
                return vis
            vis = not text or text in it.name.lower()
            q.setHidden(not vis)
            return vis

        for i in range(self.tree.topLevelItemCount()):
            rec(self.tree.topLevelItem(i))

    # ================================================================ 다운로드 시작
    def _choose_dest(self):
        d = QFileDialog.getExistingDirectory(self, "저장 위치", self.s.last_save_dir)
        if d:
            self.s.last_save_dir = os.path.normpath(d)
            self.dest_lbl.setText(self.s.last_save_dir)
            self.s.save()

    def _keep_struct_changed(self, on: bool):
        self.s.keep_folder_structure = on
        self.s.save()

    def _start_download(self):
        if not self.root_item:
            return
        dest = self.s.last_save_dir
        try:
            os.makedirs(dest, exist_ok=True)
        except OSError as e:
            self._toast(f"저장 위치를 만들 수 없어요: {e}", error=True)
            return
        n = self.svc.enqueue(self.root_item, self._selected(), dest)
        self._switch_page(1)
        self._refresh_downloads()
        self._toast(f"{n:,}개 파일을 다운로드 목록에 추가했어요")

    # ================================================================ 다운로드 목록
    def _refresh_downloads(self):
        tasks = self.svc.manager.snapshot()
        uids = {t.uid for t in tasks}
        for uid in list(self._dl_rows):
            if uid not in uids:
                q = self._dl_rows.pop(uid)
                idx = self.dl.indexOfTopLevelItem(q)
                if idx >= 0:
                    self.dl.takeTopLevelItem(idx)
        active = queued = done = 0
        for t in tasks:
            q = self._dl_rows.get(t.uid)
            if q is None:
                q = DlItem([t.name, "", "", "", "", "", ""])
                q.setData(0, ITEM_ROLE, t.uid)
                q.setData(0, SORT_ROLE, None)
                q.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
                q.setTextAlignment(3, Qt.AlignRight | Qt.AlignVCenter)
                q.setTextAlignment(4, Qt.AlignRight | Qt.AlignVCenter)
                self.dl.addTopLevelItem(q)
                self._dl_rows[t.uid] = q
                acts = RowActions(t.uid)
                acts.clicked.connect(self._row_action)
                self.dl.setItemWidget(q, 6, acts)
            prog = (t.done_bytes / t.size) if t.size else (1.0 if t.status in ("done", "skipped") else 0.0)
            if t.status == "verifying" and t.size:
                prog = t.verify_bytes / t.size
            q.setData(2, PROGRESS_ROLE, prog)
            q.setData(2, STATUS_ROLE, t.status)
            q.setData(1, SORT_ROLE, t.size or 0)
            q.setData(2, SORT_ROLE, round(prog, 3))
            q.setData(3, SORT_ROLE, round(t.speed / 1e5))
            q.setData(5, SORT_ROLE, ["downloading", "starting", "verifying", "waiting", "queued", "paused",
                                     "error", "canceled", "done", "skipped"].index(t.status)
                      if t.status in STATUS_LABEL else 99)
            w = self.dl.itemWidget(q, 6)
            if isinstance(w, RowActions):
                w.set_status(t.status)
            q.setText(1, fmt_size(t.size))
            running = t.status in ("downloading", "starting", "verifying")
            q.setText(3, fmt_speed(t.speed) if running else "")
            eta = ((t.size - t.done_bytes) / t.speed) if (running and t.size and t.speed > 1) else None
            q.setText(4, fmt_eta(eta))
            q.setData(4, SORT_ROLE, int(eta) if eta else 10 ** 9)
            label = STATUS_LABEL.get(t.status, t.status)
            if t.status == "downloading" and t.connections:
                label = f"{label} · {t.connections}연결"
            if t.status == "error":
                label = "오류: " + (t.error.split(".")[0].split(" (")[0] if t.error else "")
            elif t.status == "waiting":
                import time as _time
                left = max(0, int((t.retry_at - _time.time()) // 60) + 1)
                label = f"구글 제한 · {left}분 후 재시도"
            q.setText(5, label)
            color = {"done": style.OK, "skipped": style.OK, "error": style.ERR, "waiting": style.WARN,
                     "canceled": style.MUTED,
                     "paused": style.MUTED, "queued": style.MUTED}.get(t.status, style.TEXT)
            q.setForeground(5, QColor(color))
            tip = t.dest + (f"\n{t.error}" if t.error else "") + (f"\n{t.note}" if t.note else "")
            q.setToolTip(0, tip)
            q.setToolTip(5, tip)
            if running:
                active += 1
            elif t.status in ("queued", "waiting"):
                queued += 1
            elif t.status in ("done", "skipped"):
                done += 1
        n_open = active + queued
        self.seg_dl.setText(f"다운로드 · {n_open}" if n_open else "다운로드")
        spd = self.svc.manager.total_speed()
        parts = []
        if spd > 1:
            parts.append(f"전체 {fmt_speed(spd)}")
        if active:
            parts.append(f"진행 {active}")
        if queued:
            parts.append(f"대기 {queued}")
        if done:
            parts.append(f"완료 {done}")
        self.total_lbl.setText(" · ".join(parts))
        self.empty_dl.setVisible(not tasks)
        self.dl.setVisible(bool(tasks))

    def _dl_menu(self, pos):
        uids = [q.data(0, ITEM_ROLE) for q in self.dl.selectedItems()]
        if not uids:
            return
        m = QMenu(self)
        a_pause = m.addAction("일시정지")
        a_resume = m.addAction("재개 / 다시 시도")
        a_cancel = m.addAction("취소 (받던 임시 파일 삭제)")
        a_open = m.addAction("폴더 열기")
        m.addSeparator()
        a_rm = m.addAction("목록에서 삭제")
        act = m.exec(self.dl.viewport().mapToGlobal(pos))
        mgr = self.svc.manager
        if act == a_pause:
            mgr.pause(uids)
        elif act == a_resume:
            mgr.resume(uids)
        elif act == a_cancel:
            if self._confirm_discard(uids, "취소"):
                mgr.cancel(uids)
        elif act == a_open:
            self._open_folder(uids)
        elif act == a_rm:
            if self._confirm_discard(uids, "삭제"):
                mgr.remove(uids)
        self._refresh_downloads()

    def _row_action(self, action: str, uid: str):
        mgr = self.svc.manager
        t = mgr.get(uid)
        if not t:
            return
        if action == "toggle":
            if t.status in RUNNING or t.status in ("queued", "waiting"):
                mgr.pause([uid])
            elif t.status in ("done", "skipped"):
                self._open_folder([uid])
            else:
                mgr.resume([uid])
        elif action == "cancel":
            if self._confirm_discard([uid], "취소"):
                mgr.cancel([uid])
        elif action == "delete":
            if self._confirm_discard([uid], "삭제"):
                mgr.remove([uid])
        self._refresh_downloads()

    def _confirm_discard(self, uids, verb: str) -> bool:
        """1GB 넘게 받은 걸 버릴 땐 한 번 확인."""
        ts = [t for u in uids if (t := self.svc.manager.get(u)) and t.status in UNFINISHED]
        got = sum(t.done_bytes for t in ts)
        if got < 1024 ** 3:
            return True
        r = QMessageBox.question(
            self, f"다운로드 {verb}",
            f"{len(ts)}개 파일에서 받은 {fmt_size(got)}가 삭제되고 다시 받으려면 처음부터 받아야 해요.\n{verb}할까요?")
        return r == QMessageBox.Yes

    def _ask_leftovers(self):
        """이전 실행에서 남은 임시 파일: 이어받을지 지울지."""
        left = self.svc.manager.partial_tasks()
        if not left:
            return
        got = sum(t.done_bytes for t in left)
        box = QMessageBox(self)
        box.setWindowTitle("받다 만 파일")
        box.setText(f"이전에 받다 만 파일 {len(left)}개가 있어요 (받은 양 {fmt_size(got)}).")
        keep = box.addButton("이어받기", QMessageBox.AcceptRole)
        drop = box.addButton("임시 파일 삭제", QMessageBox.DestructiveRole)
        box.setDefaultButton(keep)
        box.exec()
        if box.clickedButton() is drop:
            n = self.svc.manager.discard_unfinished()
            self._toast(f"받다 만 파일 {n}개를 정리했어요")
        else:
            self.svc.manager.resume([t.uid for t in left])
            self._switch_page(1)
        self._refresh_downloads()

    def _open_folder(self, uids):
        t = self.svc.manager.get(uids[0]) if uids else None
        if not t:
            return
        path = t.dest if os.path.exists(t.dest) else os.path.dirname(t.dest)
        if not os.path.exists(path):
            return
        if sys.platform == "win32":
            if os.path.isfile(path):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path) if os.path.isfile(path) else path])

    # ================================================================ 종료
    def closeEvent(self, e):
        mgr = self.svc.manager
        tasks = mgr.snapshot()
        active = [t for t in tasks if t.status in RUNNING]
        unfinished = [t for t in tasks if t.status in UNFINISHED]
        discard = False
        if unfinished and self.s.cleanup_on_exit:
            got = sum(t.done_bytes for t in unfinished)
            box = QMessageBox(self)
            box.setWindowTitle("종료")
            box.setText(f"다 받지 못한 파일이 {len(unfinished)}개 있어요 (받은 양 {fmt_size(got)}).\n"
                        "종료하면서 임시 파일을 삭제할까요?")
            b_del = box.addButton("삭제하고 종료", QMessageBox.DestructiveRole)
            b_keep = box.addButton("남겨두고 종료 (다음에 이어받기)", QMessageBox.AcceptRole)
            box.addButton("취소", QMessageBox.RejectRole)
            box.setDefaultButton(b_del)
            box.exec()
            if box.clickedButton() not in (b_del, b_keep):
                e.ignore()
                return
            discard = box.clickedButton() is b_del
        elif active:
            r = QMessageBox.question(
                self, "다운로드 중",
                "받고 있는 파일이 있어요. 종료하면 일시정지되고, 다음에 실행하면 이어받을 수 있어요.\n종료할까요?")
            if r != QMessageBox.Yes:
                e.ignore()
                return
        self.s.window_geometry = bytes(self.saveGeometry().toHex()).decode()
        self.s.save()
        self._dl_timer.stop()
        if self._cancel:
            self._cancel.set()
        self.loader.shutdown()
        self.svc.shutdown()   # 받던 연결을 모두 멈추고 파일을 닫은 뒤
        if discard:
            mgr.discard_unfinished()
        e.accept()

    def sizeHint(self):
        return QSize(1120, 760)
