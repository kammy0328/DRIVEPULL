"""썸네일 보기: 폴더 단위 카드 그리드. 체크 상태는 목록 보기(트리)와 공유한다."""
from __future__ import annotations

import os

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QKeySequence, QPainter, QPainterPath, QPen, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QPushButton, QSlider, QStyle, QStyledItemDelegate, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from . import style
from .widgets import fmt_size, natural_key

TREE_ROLE = Qt.UserRole + 20
ITEM_ROLE = Qt.UserRole + 1   # main_window 과 같은 값 (트리 항목의 DriveItem)

VIDEO_EXT = {"mov", "mp4", "mxf", "avi", "mkv", "m4v", "webm", "r3d", "braw", "mts", "m2ts", "ari", "crm", "wmv"}
IMAGE_EXT = {"jpg", "jpeg", "png", "tif", "tiff", "exr", "dpx", "psd", "heic", "webp", "gif", "bmp", "cr3", "arw", "dng"}


def is_video(it) -> bool:
    return it.mime.startswith("video/") or os.path.splitext(it.name)[1].lstrip(".").lower() in VIDEO_EXT


def fmt_dur(ms: int) -> str:
    s = ms // 1000
    if s >= 3600:
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def res_badge(w: int, h: int) -> str:
    m = max(w, h)
    if m >= 7600:
        return "8K"
    if m >= 5000:
        return "6K"
    if m >= 3800:
        return "4K"
    if m >= 2500:
        return "2.5K"
    if m >= 1900:
        return "FHD"
    if m >= 1200:
        return "HD"
    return ""


def info_text(it) -> str:
    """목록 보기 '정보' 열에도 쓰는 해상도·길이 문자열."""
    parts = []
    if it.width and it.height:
        parts.append(f"{it.width}×{it.height}")
    if it.duration_ms:
        parts.append(fmt_dur(it.duration_ms))
    return " · ".join(parts)


def sort_value(it, key: str):
    """정렬 기준값. 폴더 크기는 _recalc_folder_sizes 가 계산해 둔 _tot 사용."""
    if key == "size":
        return getattr(it, "_tot", 0) if it.is_folder else (it.size or 0)
    if key == "type":
        return ("" if it.is_folder else os.path.splitext(it.name)[1].lower(), natural_key(it.name))
    if key == "modified":
        return it.modified or ""
    if key == "info":
        return (it.duration_ms, it.width * it.height)
    return natural_key(it.name)


class CardDelegate(QStyledItemDelegate):
    toggled = Signal(object)          # QTreeWidgetItem
    play = Signal(object)             # QTreeWidgetItem (영상)
    META_H = 46
    PAD = 6

    def __init__(self, grid: "ThumbGrid"):
        super().__init__(grid.view)
        self.grid = grid

    def sizeHint(self, opt, idx):
        w = self.grid.eff_w
        return QSize(w + 2 * self.PAD, int(w * 9 / 16) + self.META_H + 2 * self.PAD)

    def _rects(self, r: QRect):
        card = QRectF(r).adjusted(self.PAD, self.PAD, -self.PAD, -self.PAD)
        th = QRectF(card.left(), card.top(), card.width(), card.width() * 9 / 16)
        box = QRectF(th.left() + 8, th.top() + 8, 20, 20)
        return card, th, box

    def paint(self, p: QPainter, opt, idx):
        q: QTreeWidgetItem = idx.data(TREE_ROLE)
        if q is None:
            return
        it = q.data(0, ITEM_ROLE)
        state = q.checkState(0)
        checkable = bool(q.flags() & Qt.ItemIsUserCheckable) and bool(q.flags() & Qt.ItemIsEnabled)
        selected = bool(opt.state & QStyle.State_Selected)
        hover = bool(opt.state & QStyle.State_MouseOver)
        card, th, box = self._rects(opt.rect)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)

        # 카드 배경
        p.setPen(QPen(QColor(style.ACCENT if selected else ("#3a404b" if hover else style.BORDER)),
                      2 if selected else 1))
        p.setBrush(QColor(style.PANEL))
        p.drawRoundedRect(card, 10, 10)

        # 썸네일 영역 (위쪽 모서리만 둥글게)
        cpath = QPainterPath()
        cpath.addRoundedRect(card.adjusted(1, 1, -1, -1), 9, 9)
        tpath = QPainterPath()
        tpath.addRect(th)
        p.setClipPath(cpath.intersected(tpath))
        p.fillRect(th, QColor("#0a0b0d"))
        preview = self.grid.preview_of(it) if it.is_folder else it
        img = self.grid.loader.get(preview.id) if preview is not None else None
        ext = os.path.splitext(it.name)[1].lstrip(".").upper()[:5]
        if img is not None and not img.isNull():
            s = img.size().scaled(th.size().toSize(), Qt.KeepAspectRatio)
            target = QRectF(th.center().x() - s.width() / 2, th.center().y() - s.height() / 2,
                            s.width(), s.height())
            p.drawImage(target, img)
            if it.is_folder:
                p.fillRect(th, QColor(0, 0, 0, 130))
                self._folder_glyph(p, th, small=True)
        elif it.is_folder:
            self._folder_glyph(p, th)
        else:
            f = QFont(p.font())
            f.setPixelSize(max(14, int(th.height() / 5)))
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor("#3a404b"))
            p.drawText(th, Qt.AlignCenter, ext or "FILE")
        if checkable and state == Qt.Unchecked and not it.is_folder:
            p.fillRect(th, QColor(0, 0, 0, 120))   # 선택 안 된 건 어둡게
        if not checkable and not it.is_folder:
            p.fillRect(th, QColor(0, 0, 0, 160))

        # 배지
        small = QFont(p.font())
        small.setPixelSize(11)
        small.setBold(True)
        p.setFont(small)
        if not it.is_folder and it.duration_ms:
            self._badge(p, fmt_dur(it.duration_ms), th.right() - 8, th.bottom() - 8, align_right=True)
        if not it.is_folder and getattr(it, "blocked", False):
            p.save()
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(style.ERR))
            fm = QFontMetrics(p.font())
            txt = "다운로드 불가"
            r = QRectF(th.left() + 36, th.top() + 9, fm.horizontalAdvance(txt) + 12, 18)
            p.drawRoundedRect(r, 5, 5)
            p.setPen(QColor("white"))
            p.drawText(r, Qt.AlignCenter, txt)
            p.restore()
        rb = res_badge(it.width, it.height)
        if rb:
            self._badge(p, rb, th.right() - 8, th.top() + 8, align_right=True, top=True)
        if not it.is_folder and is_video(it) and (hover or img is None):
            self._play_icon(p, th, strong=hover)   # 가운데 재생 버튼 (누르면 미리보기)
        p.setClipping(False)

        # 체크박스
        if checkable or it.is_folder:
            self._checkbox(p, box, state, hover)

        # 이름과 정보
        name_f = QFont(p.font())
        name_f.setPixelSize(12)
        name_f.setBold(True)
        p.setFont(name_f)
        p.setPen(QColor(style.TEXT if (state != Qt.Unchecked or it.is_folder) else style.MUTED))
        tr = QRectF(card.left() + 10, th.bottom() + 6, card.width() - 20, 18)
        p.drawText(tr, Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(name_f).elidedText(it.name, Qt.ElideMiddle, int(tr.width())))
        meta_f = QFont(p.font())
        meta_f.setPixelSize(11)
        meta_f.setBold(False)
        p.setFont(meta_f)
        p.setPen(QColor(style.MUTED))
        if it.is_folder:
            meta = self.grid.folder_meta(it)
        else:
            meta = " · ".join(x for x in (ext, fmt_size(it.size) if it.size is not None else "",
                                          f"{it.width}×{it.height}" if it.width else "") if x)
        mr = QRectF(tr.left(), tr.bottom() + 1, tr.width(), 16)
        p.drawText(mr, Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(meta_f).elidedText(meta, Qt.ElideRight, int(mr.width())))
        p.restore()

    @staticmethod
    def _badge(p, text, x, y, align_right=True, top=False):
        fm = QFontMetrics(p.font())
        w = fm.horizontalAdvance(text) + 12
        h = 18
        r = QRectF(x - w if align_right else x, y if top else y - h, w, h)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 175))
        p.drawRoundedRect(r, 5, 5)
        p.setPen(QColor("#ffffff"))
        p.drawText(r, Qt.AlignCenter, text)

    @staticmethod
    def _play_icon(p, th, strong=False):
        c = th.center()
        r = min(th.width(), th.height()) * 0.11
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 150) if strong else QColor(255, 255, 255, 60))
        p.drawEllipse(c, r * 1.6, r * 1.6)
        path = QPainterPath()
        path.moveTo(c.x() - r * 0.55, c.y() - r * 0.8)
        path.lineTo(c.x() - r * 0.55, c.y() + r * 0.8)
        path.lineTo(c.x() + r * 0.85, c.y())
        path.closeSubpath()
        p.setBrush(QColor(255, 255, 255, 200))
        p.drawPath(path)

    @staticmethod
    def _folder_glyph(p, th, small=False):
        w = th.width() * (0.14 if small else 0.30)
        h = w * 0.72
        if small:
            x, y = th.left() + 12, th.bottom() - h - 10
        else:
            x = th.center().x() - w / 2
            y = th.center().y() - h / 2
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#3d4f7a"))
        p.drawRoundedRect(QRectF(x, y, w * 0.45, h * 0.3), 4, 4)
        p.setBrush(QColor(style.ACCENT))
        p.drawRoundedRect(QRectF(x, y + h * 0.15, w, h * 0.85), 6, 6)

    @staticmethod
    def _checkbox(p, box, state, hover):
        p.setPen(QPen(QColor(255, 255, 255, 200 if hover else 140), 1.4))
        if state == Qt.Checked:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(style.ACCENT))
            p.drawRoundedRect(box, 5, 5)
            pen = QPen(QColor("white"), 2.2)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            path = QPainterPath(QPointF(box.left() + 5, box.center().y() + 0.5))
            path.lineTo(box.left() + 8.5, box.bottom() - 5.5)
            path.lineTo(box.right() - 4.5, box.top() + 6)
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
        elif state == Qt.PartiallyChecked:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#2c3a5e"))
            p.drawRoundedRect(box, 5, 5)
            p.setBrush(QColor("white"))
            p.drawRoundedRect(QRectF(box.left() + 5, box.center().y() - 1.2, box.width() - 10, 2.4), 1, 1)
        else:
            p.setBrush(QColor(0, 0, 0, 120))
            p.drawRoundedRect(box, 5, 5)

    def editorEvent(self, ev, model, opt, idx):
        if ev.type() == QEvent.MouseButtonRelease and ev.button() == Qt.LeftButton:
            _, _, box = self._rects(opt.rect)
            q = idx.data(TREE_ROLE)
            if box.adjusted(-6, -6, 6, 6).contains(ev.position()):
                if q is not None:
                    self.toggled.emit(q)
                return True
            _, th, _ = self._rects(opt.rect)
            r = min(th.width(), th.height()) * 0.18
            c = th.center()
            if q is not None and is_video(q.data(0, ITEM_ROLE)) and not q.data(0, ITEM_ROLE).is_folder \
                    and abs(ev.position().x() - c.x()) <= r and abs(ev.position().y() - c.y()) <= r:
                self.play.emit(q)
                return True
        return super().editorEvent(ev, model, opt, idx)


class ThumbGrid(QWidget):
    navigated = Signal()
    play_requested = Signal(object)   # QTreeWidgetItem

    def __init__(self, loader, parent=None):
        super().__init__(parent)
        self.loader = loader
        self.card_w = 220
        self.root: QTreeWidgetItem | None = None
        self.current: QTreeWidgetItem | None = None
        self.filter_text = ""
        self.sort_key = "name"
        self.sort_desc = False
        self._meta: dict[int, str] = {}
        self._preview: dict[int, object] = {}
        self.eff_w = self.card_w

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        bar = QHBoxLayout()
        bar.setSpacing(2)
        self.crumbs = QHBoxLayout()
        self.crumbs.setSpacing(2)
        bar.addLayout(self.crumbs)
        bar.addStretch(1)
        zl = QLabel("크기")
        zl.setObjectName("muted")
        bar.addWidget(zl)
        self.zoom = QSlider(Qt.Horizontal)
        self.zoom.setRange(140, 400)
        self.zoom.setValue(self.card_w)
        self.zoom.setFixedWidth(140)
        self.zoom.valueChanged.connect(self._set_card_w)
        bar.addWidget(self.zoom)
        v.addLayout(bar)

        self.view = QListWidget()
        self.view.setViewMode(QListWidget.IconMode)
        self.view.setResizeMode(QListWidget.Adjust)
        self.view.setMovement(QListWidget.Static)
        self.view.setUniformItemSizes(True)
        self.view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.view.setMouseTracking(True)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.view.verticalScrollBar().setSingleStep(24)
        self.view.setStyleSheet(
            f"QListWidget {{ background: {style.BG}; border: none; }}"
            "QListWidget::item, QListWidget::item:selected, QListWidget::item:hover "
            "{ background: transparent; border: none; }")
        self.delegate = CardDelegate(self)
        self.view.setItemDelegate(self.delegate)
        self.delegate.toggled.connect(self._toggle_one)
        self.delegate.play.connect(self.play_requested.emit)
        self.view.itemDoubleClicked.connect(self._double)
        self.view.verticalScrollBar().valueChanged.connect(lambda *_: self._vis_timer.start())
        self.view.installEventFilter(self)   # 뷰 자체 크기 변화만 (스크롤바로 인한 뷰포트 변화는 무시)
        v.addWidget(self.view, 1)

        self.empty = QLabel("")
        self.empty.setObjectName("muted")
        self.empty.setAlignment(Qt.AlignCenter)
        v.addWidget(self.empty)
        self.empty.hide()

        self._vis_timer = QTimer(self, singleShot=True, interval=120, timeout=self._request_visible)
        loader.loaded.connect(self._on_loaded)
        QShortcut(QKeySequence(Qt.Key_Space), self.view, self._toggle_selected)
        QShortcut(QKeySequence(Qt.Key_Backspace), self.view, self.go_up)
        QShortcut(QKeySequence(Qt.Key_Return), self.view, self._enter_selected)

    # ---- 데이터
    def folder_meta(self, it) -> str:
        m = self._meta.get(id(it))
        if m is None:
            files = list(it.walk_files())
            m = self._meta[id(it)] = f"폴더 · 파일 {len(files):,}개 · {fmt_size(sum(f.size or 0 for f in files))}"
        return m

    def preview_of(self, folder):
        """폴더 카드에 보여줄 대표 썸네일 (안쪽 첫 파일)."""
        k = id(folder)
        if k not in self._preview:
            self._preview[k] = next((f for f in folder.walk_files() if f.thumb_url), None)
        return self._preview[k]

    def set_root(self, root: QTreeWidgetItem | None):
        self.root = root
        self._meta.clear()
        self._preview.clear()
        self.open_folder(root)

    def open_folder(self, q: QTreeWidgetItem | None):
        self.current = q
        self._rebuild()
        self._render_crumbs()
        self.navigated.emit()

    def set_filter(self, text: str):
        self.filter_text = text.strip().lower()
        self._rebuild()

    def go_up(self):
        if self.current is not None and self.current.parent() is not None:
            self.open_folder(self.current.parent())

    def _entries(self) -> list[QTreeWidgetItem]:
        q = self.current
        if q is None:
            return []
        it = q.data(0, ITEM_ROLE)
        if not it.is_folder:
            return [q]
        if self.filter_text:
            # 검색 중엔 하위 폴더까지 펼쳐서 맞는 파일만
            out, stack = [], [q]
            while stack:
                x = stack.pop()
                for i in range(x.childCount()):
                    c = x.child(i)
                    ci = c.data(0, ITEM_ROLE)
                    if ci.is_folder:
                        stack.append(c)
                    elif self.filter_text in ci.name.lower():
                        out.append(c)
            return self._sorted(out)
        return self._sorted([q.child(i) for i in range(q.childCount())])

    def _sorted(self, items):
        def key(c):
            return sort_value(c.data(0, ITEM_ROLE), self.sort_key)
        folders = sorted([c for c in items if c.data(0, ITEM_ROLE).is_folder], key=key, reverse=self.sort_desc)
        files = sorted([c for c in items if not c.data(0, ITEM_ROLE).is_folder], key=key, reverse=self.sort_desc)
        return folders + files

    def set_sort(self, key: str, desc: bool):
        if (key, desc) != (self.sort_key, self.sort_desc):
            self.sort_key, self.sort_desc = key, desc
            if self.current is not None:
                self._rebuild()

    def _rebuild(self):
        self.view.clear()
        entries = self._entries()
        for c in entries:
            li = QListWidgetItem()
            li.setData(TREE_ROLE, c)
            li.setToolTip(c.data(0, ITEM_ROLE).name)
            self.view.addItem(li)
        self.empty.setVisible(not entries and self.current is not None)
        self.view.setVisible(bool(entries) or self.current is None)
        self.empty.setText("검색 결과가 없어요" if self.filter_text else "빈 폴더예요")
        self.view.scrollToTop()
        QTimer.singleShot(0, self._request_visible)

    def _render_crumbs(self):
        while self.crumbs.count():
            w = self.crumbs.takeAt(0).widget()
            if w:
                w.deleteLater()
        chain = []
        q = self.current
        while q is not None:
            chain.append(q)
            q = q.parent()
        chain.reverse()
        for i, q in enumerate(chain):
            if i:
                sep = QLabel("›")
                sep.setObjectName("muted")
                self.crumbs.addWidget(sep)
            b = QPushButton(q.data(0, ITEM_ROLE).name)
            b.setObjectName("ghost")
            b.setCursor(Qt.PointingHandCursor)
            if i == len(chain) - 1:
                b.setStyleSheet(f"color: {style.TEXT}; font-weight: 700;")
            b.clicked.connect(lambda _=False, x=q: self.open_folder(x))
            self.crumbs.addWidget(b)

    def _set_card_w(self, w: int):
        self.card_w = w
        self._relayout()
        self._vis_timer.start()

    def _relayout(self):
        """카드가 가로 폭을 꽉 채우도록 실제 카드 폭을 계산."""
        pad = CardDelegate.PAD
        # 스크롤바가 보이든 안 보이든 같은 폭으로 계산해야 함.
        # (뷰포트 폭 기준이면: 카드 커짐 → 스크롤바 생김 → 폭 줄어 카드 작아짐 → 스크롤바 사라짐 → … 무한 반복으로 화면이 떨림)
        sb = self.view.style().pixelMetric(QStyle.PM_ScrollBarExtent)
        vw = self.view.width() - 2 * self.view.frameWidth() - sb - 10
        cols = max(1, vw // (self.card_w + 2 * pad))
        eff = max(120, vw // cols - 2 * pad)
        if eff != self.eff_w:
            self.eff_w = eff
            self.view.setGridSize(QSize())
            self.view.doItemsLayout()

    # ---- 썸네일 요청
    def _request_visible(self):
        vp = self.view.viewport().rect().adjusted(0, -400, 0, 800)   # 위아래 여유분 포함
        items = []
        for i in range(self.view.count()):
            li = self.view.item(i)
            if self.view.visualItemRect(li).intersects(vp):
                it = li.data(TREE_ROLE).data(0, ITEM_ROLE)
                if it.is_folder:
                    it = self.preview_of(it)
                if it is not None:
                    items.append(it)
        self.loader.request(items)

    def _on_loaded(self, _fid: str):
        self.view.viewport().update()

    def refresh_checks(self, sizes_changed: bool = False):
        if sizes_changed:
            self._meta.clear()
        self.view.viewport().update()

    # ---- 조작
    def _toggle_one(self, q: QTreeWidgetItem):
        if not (q.flags() & Qt.ItemIsUserCheckable) or not (q.flags() & Qt.ItemIsEnabled):
            return
        q.setCheckState(0, Qt.Unchecked if q.checkState(0) == Qt.Checked else Qt.Checked)
        self.view.viewport().update()

    def _toggle_selected(self):
        qs = [li.data(TREE_ROLE) for li in self.view.selectedItems()]
        qs = [q for q in qs if q.flags() & Qt.ItemIsUserCheckable and q.flags() & Qt.ItemIsEnabled]
        if not qs:
            return
        new = Qt.Unchecked if qs[0].checkState(0) == Qt.Checked else Qt.Checked
        for q in qs:
            q.setCheckState(0, new)
        self.view.viewport().update()

    def _double(self, li: QListWidgetItem):
        q = li.data(TREE_ROLE)
        it = q.data(0, ITEM_ROLE)
        if it.is_folder:
            self.open_folder(q)
        elif is_video(it):
            self.play_requested.emit(q)   # 영상은 더블클릭하면 재생
        else:
            self._toggle_one(q)

    def visible_videos(self) -> list:
        """지금 보이는 순서대로의 영상 항목 (플레이어 이전/다음용)."""
        out = []
        for i in range(self.view.count()):
            q = self.view.item(i).data(TREE_ROLE)
            it = q.data(0, ITEM_ROLE)
            if not it.is_folder and is_video(it):
                out.append(q)
        return out

    def _enter_selected(self):
        sel = self.view.selectedItems()
        if len(sel) == 1:
            self._double(sel[0])

    def eventFilter(self, obj, ev):
        if obj is self.view and ev.type() == QEvent.Resize:
            QTimer.singleShot(0, self._relayout)
            self._vis_timer.start()
        return False
