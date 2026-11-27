"""표 열 너비: 드래그 조절, 경계 더블클릭으로 내용에 맞춤, 너비 기억."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import (QHeaderView, QMenu, QStyle, QTreeWidget, QTreeWidgetItem,
                               QTreeWidgetItemIterator)

FIT_MAX = 1400   # 아주 긴 이름이라도 이보다 넓게는 안 늘림


class Columns(QObject):
    def __init__(self, view: QTreeWidget, key: str, settings, defaults: dict[int, int],
                 fixed: tuple = (), fill_col: int = 0, skip_fit: tuple = ()):
        super().__init__(view)
        self.view = view
        self.key = f"cols_{key}"
        self.s = settings
        self.defaults = defaults
        self.fixed = set(fixed)
        self.fill_col = fill_col
        self.skip_fit = set(skip_fit)      # 진행률 막대처럼 글자가 아닌 열
        self._prog = False                 # 코드에서 바꾸는 중 (사용자 조절과 구분)
        hh = view.header()
        hh.setStretchLastSection(False)
        hh.setMinimumSectionSize(36)
        hh.setSectionsMovable(False)
        for c in range(view.columnCount()):
            hh.setSectionResizeMode(c, QHeaderView.Fixed if c in self.fixed else QHeaderView.Interactive)
        saved = self.s.extra.get(self.key) or {}
        # 이름 열을 직접 조절한 적이 없으면 창 크기에 맞춰 이름 열이 남는 폭을 채움
        self.user_fill = str(fill_col) in saved
        self._with_prog(lambda: [view.setColumnWidth(c, int(saved.get(str(c), w)))
                                 for c, w in defaults.items()])
        self._save_timer = QTimer(self, singleShot=True, interval=500, timeout=self._save)
        hh.sectionResized.connect(self._on_resized)
        hh.sectionHandleDoubleClicked.connect(lambda c: QTimer.singleShot(0, lambda: self.fit(c)))
        hh.setContextMenuPolicy(Qt.CustomContextMenu)
        hh.customContextMenuRequested.connect(self._menu)
        view.installEventFilter(self)
        QTimer.singleShot(0, self.fill)

    # ---- 내부
    def _with_prog(self, fn):
        self._prog = True
        try:
            fn()
        finally:
            self._prog = False

    def _on_resized(self, col: int, _old: int, _new: int):
        if self._prog:
            return
        if col == self.fill_col:
            self.user_fill = True
        self._save_timer.start()

    def _save(self):
        hh = self.view.header()
        widths = {str(c): hh.sectionSize(c) for c in range(self.view.columnCount()) if c not in self.fixed}
        if not self.user_fill:
            widths.pop(str(self.fill_col), None)
        self.s.extra[self.key] = widths
        self.s.save()

    def eventFilter(self, obj, ev):
        if obj is self.view and ev.type() == QEvent.Resize:
            QTimer.singleShot(0, self.fill)
        return False

    # ---- 공개
    def fill(self):
        """이름 열이 남은 폭을 채우게 (사용자가 직접 조절한 뒤엔 건드리지 않음)."""
        if self.user_fill:
            return
        hh = self.view.header()
        others = sum(hh.sectionSize(c) for c in range(self.view.columnCount()) if c != self.fill_col)
        w = max(200, self.view.viewport().width() - others - 2)
        self._with_prog(lambda: self.view.setColumnWidth(self.fill_col, w))

    def fit(self, col: int):
        """열을 가장 긴 글자에 맞춤 (화면 밖 줄까지 전부 확인)."""
        if col in self.fixed or col in self.skip_fit:
            return
        v = self.view
        fm = v.fontMetrics()
        st = v.style()
        hh = v.header()
        head = v.headerItem().text(col)
        best = fm.horizontalAdvance(head) + 34          # 정렬 화살표 자리
        check_w = st.pixelMetric(QStyle.PM_IndicatorWidth) + 10
        it = QTreeWidgetItemIterator(v)
        while it.value():
            q: QTreeWidgetItem = it.value()
            it += 1
            text = q.text(col)
            if not text and col != 0:
                continue
            w = fm.horizontalAdvance(text) + 18
            if col == 0:
                depth = 0
                p = q.parent()
                while p is not None:
                    depth += 1
                    p = p.parent()
                w += v.indentation() * (depth + (1 if v.rootIsDecorated() else 0))
                if q.flags() & Qt.ItemIsUserCheckable:
                    w += check_w
            best = max(best, w)
        best = min(FIT_MAX, best)
        if col == self.fill_col:
            self.user_fill = True
        hh.resizeSection(col, best)
        self._save_timer.start()

    def fit_all(self):
        for c in range(self.view.columnCount()):
            self.fit(c)

    def reset(self):
        self.s.extra.pop(self.key, None)
        self.s.save()
        self.user_fill = False
        self._with_prog(lambda: [self.view.setColumnWidth(c, w) for c, w in self.defaults.items()])
        self.fill()

    def _menu(self, pos):
        hh = self.view.header()
        col = hh.logicalIndexAt(pos)
        m = QMenu(self.view)
        a1 = m.addAction("이 열 내용에 맞추기") if col >= 0 and col not in self.fixed | self.skip_fit else None
        a2 = m.addAction("모든 열 내용에 맞추기")
        m.addSeparator()
        a3 = m.addAction("기본 너비로")
        act = m.exec(hh.mapToGlobal(pos))
        if act is None:
            return
        if act == a1:
            self.fit(col)
        elif act == a2:
            self.fit_all()
        elif act == a3:
            self.reset()
