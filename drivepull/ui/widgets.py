from __future__ import annotations

import math

from PySide6.QtCore import QRectF, Qt, QTimer, QVariantAnimation, QEasingCurve
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QStyledItemDelegate, QWidget, QStyle

from . import style

PROGRESS_ROLE = Qt.UserRole + 10
STATUS_ROLE = Qt.UserRole + 11


def fmt_size(n) -> str:
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return "-"


def fmt_speed(bps: float) -> str:
    if bps <= 1:
        return ""
    return fmt_size(bps) + "/s"


def fmt_eta(sec) -> str:
    if sec is None or sec <= 0 or math.isinf(sec):
        return ""
    sec = int(sec)
    if sec < 60:
        return f"{sec}초"
    if sec < 3600:
        return f"{sec // 60}분 {sec % 60:02d}초"
    return f"{sec // 3600}시간 {sec % 3600 // 60:02d}분"


class Spinner(QWidget):
    """로딩 표시용 회전 원호."""

    def __init__(self, size: int = 18, color: str = style.ACCENT, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._angle = 0
        self._color = QColor(color)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.hide()

    def start(self):
        self.show()
        self._timer.start(16)

    def stop(self):
        self._timer.stop()
        self.hide()

    def _tick(self):
        self._angle = (self._angle + 7) % 360
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(2, 2, self.width() - 4, self.height() - 4)
        pen = QPen(QColor(255, 255, 255, 30), 2.2)
        p.setPen(pen)
        p.drawEllipse(r)
        pen.setColor(self._color)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(r, int(-self._angle * 16), int(100 * 16))


class ProgressDelegate(QStyledItemDelegate):
    """표 안의 둥근 진행 막대."""

    COLORS = {
        "done": style.OK, "skipped": style.OK, "error": style.ERR, "paused": "#6b7280",
        "verifying": style.WARN, "waiting": style.WARN,
    }

    def paint(self, p: QPainter, opt, idx):
        if opt.state & QStyle.State_Selected:
            p.fillRect(opt.rect, QColor("#222a3d"))
        val = idx.data(PROGRESS_ROLE)
        status = idx.data(STATUS_ROLE) or ""
        if val is None:
            return
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(opt.rect).adjusted(6, 0, -6, 0)
        h = 6
        bar = QRectF(r.left(), r.center().y() - h / 2, r.width() - 44, h)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#262a32"))
        p.drawRoundedRect(bar, 3, 3)
        if val > 0:
            fill = QRectF(bar.left(), bar.top(), max(h, bar.width() * min(1.0, val)), h)
            p.setBrush(QColor(self.COLORS.get(status, style.ACCENT)))
            p.drawRoundedRect(fill, 3, 3)
        p.setPen(QColor(style.MUTED))
        p.drawText(QRectF(bar.right() + 6, opt.rect.top(), 40, opt.rect.height()),
                   Qt.AlignVCenter | Qt.AlignRight, f"{val * 100:.0f}%")
        p.restore()


class FadeIn:
    """페이지 전환 시 살짝 페이드인."""

    def __init__(self, widget: QWidget):
        from PySide6.QtWidgets import QGraphicsOpacityEffect
        self.w = widget
        self.eff = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(self.eff)
        self.anim = QVariantAnimation(widget)
        self.anim.setDuration(180)
        self.anim.setStartValue(0.0)
        self.anim.setEndValue(1.0)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)
        self.anim.valueChanged.connect(lambda v: self.eff.setOpacity(v))
        self.anim.finished.connect(lambda: self.eff.setOpacity(1.0))

    def play(self):
        self.anim.stop()
        self.anim.start()
