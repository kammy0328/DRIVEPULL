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


# ---------------------------------------------------------------- 아이콘 (글꼴에 의존하지 않게 직접 그림)
def make_icon(kind: str, color: str = "#c9ced8", size: int = 16):
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QIcon, QPainterPath, QPixmap
    pm = QPixmap(size * 2, size * 2)   # 고해상도 화면 대응
    pm.setDevicePixelRatio(2.0)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    c = QColor(color)
    s = float(size)
    pen = QPen(c, 1.8)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    if kind == "pause":
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawRoundedRect(QRectF(s * 0.25, s * 0.2, s * 0.17, s * 0.6), 1.5, 1.5)
        p.drawRoundedRect(QRectF(s * 0.58, s * 0.2, s * 0.17, s * 0.6), 1.5, 1.5)
    elif kind == "play":
        path = QPainterPath(QPointF(s * 0.3, s * 0.18))
        path.lineTo(s * 0.3, s * 0.82)
        path.lineTo(s * 0.82, s * 0.5)
        path.closeSubpath()
        p.setPen(QPen(c, 1.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(c)
        p.drawPath(path)
    elif kind == "retry":
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawArc(QRectF(s * 0.2, s * 0.2, s * 0.6, s * 0.6), 60 * 16, 290 * 16)
        path = QPainterPath(QPointF(s * 0.62, s * 0.1))
        path.lineTo(s * 0.78, s * 0.27)
        path.lineTo(s * 0.56, s * 0.34)
        p.drawPath(path)
    elif kind == "cancel":
        p.setPen(pen)
        p.drawLine(QPointF(s * 0.27, s * 0.27), QPointF(s * 0.73, s * 0.73))
        p.drawLine(QPointF(s * 0.73, s * 0.27), QPointF(s * 0.27, s * 0.73))
    elif kind == "trash":
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawLine(QPointF(s * 0.18, s * 0.28), QPointF(s * 0.82, s * 0.28))
        p.drawLine(QPointF(s * 0.4, s * 0.16), QPointF(s * 0.6, s * 0.16))
        p.drawRoundedRect(QRectF(s * 0.27, s * 0.28, s * 0.46, s * 0.56), 2, 2)
        p.drawLine(QPointF(s * 0.43, s * 0.42), QPointF(s * 0.43, s * 0.72))
        p.drawLine(QPointF(s * 0.57, s * 0.42), QPointF(s * 0.57, s * 0.72))
    elif kind == "folder":
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        path = QPainterPath(QPointF(s * 0.15, s * 0.3))
        path.lineTo(s * 0.15, s * 0.8)
        path.lineTo(s * 0.85, s * 0.8)
        path.lineTo(s * 0.85, s * 0.38)
        path.lineTo(s * 0.5, s * 0.38)
        path.lineTo(s * 0.42, s * 0.25)
        path.lineTo(s * 0.15, s * 0.25)
        path.closeSubpath()
        p.drawPath(path)
    elif kind in ("prev", "next"):
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        path = QPainterPath()
        if kind == "next":
            path.moveTo(s * 0.22, s * 0.2)
            path.lineTo(s * 0.22, s * 0.8)
            path.lineTo(s * 0.66, s * 0.5)
            p.drawRect(QRectF(s * 0.68, s * 0.2, s * 0.12, s * 0.6))
        else:
            path.moveTo(s * 0.78, s * 0.2)
            path.lineTo(s * 0.78, s * 0.8)
            path.lineTo(s * 0.34, s * 0.5)
            p.drawRect(QRectF(s * 0.2, s * 0.2, s * 0.12, s * 0.6))
        path.closeSubpath()
        p.drawPath(path)
    elif kind in ("volume", "muted"):
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        path = QPainterPath(QPointF(s * 0.12, s * 0.38))
        path.lineTo(s * 0.3, s * 0.38)
        path.lineTo(s * 0.52, s * 0.18)
        path.lineTo(s * 0.52, s * 0.82)
        path.lineTo(s * 0.3, s * 0.62)
        path.lineTo(s * 0.12, s * 0.62)
        path.closeSubpath()
        p.drawPath(path)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        if kind == "volume":
            p.drawArc(QRectF(s * 0.42, s * 0.3, s * 0.3, s * 0.4), -50 * 16, 100 * 16)
            p.drawArc(QRectF(s * 0.4, s * 0.16, s * 0.48, s * 0.68), -50 * 16, 100 * 16)
        else:
            p.drawLine(QPointF(s * 0.64, s * 0.36), QPointF(s * 0.88, s * 0.64))
            p.drawLine(QPointF(s * 0.88, s * 0.36), QPointF(s * 0.64, s * 0.64))
    elif kind in ("asc", "desc"):
        p.setPen(pen)
        y1, y2 = (s * 0.2, s * 0.8) if kind == "asc" else (s * 0.8, s * 0.2)
        p.drawLine(QPointF(s * 0.5, y1), QPointF(s * 0.5, y2))
        p.drawLine(QPointF(s * 0.5, y1), QPointF(s * 0.3, y1 + (s * 0.22 if kind == "asc" else -s * 0.22)))
        p.drawLine(QPointF(s * 0.5, y1), QPointF(s * 0.7, y1 + (s * 0.22 if kind == "asc" else -s * 0.22)))
    p.end()
    return QIcon(pm)


# ---------------------------------------------------------------- 정렬
_NUM = __import__("re").compile(r"(\d+)")


def natural_key(name: str):
    """A2 < A10 처럼 숫자를 숫자로 비교 (클립 이름 A001_C002 정렬용)."""
    return [int(x) if x.isdigit() else x.lower() for x in _NUM.split(name)]
