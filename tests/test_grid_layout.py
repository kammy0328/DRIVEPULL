"""썸네일 보기가 어떤 창 폭·카드 크기에서도 레이아웃을 무한 반복 재계산(화면 떨림)하지 않는지."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")


def test_no_relayout_oscillation():
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QTreeWidgetItem

    from drivepull.drive import DriveItem
    import drivepull.ui.grid as G
    from drivepull.ui.main_window import ITEM_ROLE

    app = QApplication.instance() or QApplication([])

    class FakeLoader:
        from PySide6.QtCore import QObject, Signal

        class _S(QObject):
            loaded = __import__("PySide6.QtCore", fromlist=["Signal"]).Signal(str)
        _s = _S()
        loaded = _s.loaded

        def get(self, fid):
            return None

        def request(self, items):
            pass

    root_it = DriveItem(id="r", name="root", is_folder=True)
    root = QTreeWidgetItem()
    root.setData(0, ITEM_ROLE, root_it)
    for i in range(10):          # 이 개수가 스크롤바 경계에 걸리는 경우를 만든다
        it = DriveItem(id=f"f{i}", name=f"clip_{i}.mp4", size=1000)
        root_it.children.append(it)
        q = QTreeWidgetItem(root)
        q.setData(0, ITEM_ROLE, it)
        q.setFlags(q.flags() | Qt.ItemIsUserCheckable)
        q.setCheckState(0, Qt.Checked)

    g = G.ThumbGrid(FakeLoader())
    g.show()
    g.set_root(root)
    count = [0]
    orig = G.ThumbGrid._relayout

    def counting(self):
        before = self.eff_w
        orig(self)
        if self.eff_w != before:
            count[0] += 1

    G.ThumbGrid._relayout = counting
    try:
        bad = []
        for card in range(140, 401, 40):
            g.zoom.setValue(card)
            for w in range(700, 1900, 23):
                for h in (420, 560, 700):
                    g.resize(w, h)
                    for _ in range(3):
                        app.processEvents()
                    count[0] = 0
                    for _ in range(15):          # 안정 상태면 더 이상 재계산이 없어야 함
                        app.processEvents()
                    if count[0] > 0:
                        bad.append((card, w, h, count[0]))
        assert not bad, bad[:10]
    finally:
        G.ThumbGrid._relayout = orig
