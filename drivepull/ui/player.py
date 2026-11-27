"""영상 미리보기 플레이어 (원본을 받으면서 재생)."""
from __future__ import annotations

import os

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel, QSlider, QStyle, QToolButton,
                               QVBoxLayout, QWidget)

from . import style
from .grid import fmt_dur
from .widgets import Spinner, fmt_size, make_icon

MIME = {".mov": "video/quicktime", ".mp4": "video/mp4", ".m4v": "video/mp4", ".mxf": "application/mxf",
        ".mkv": "video/x-matroska", ".webm": "video/webm", ".avi": "video/x-msvideo"}


class _NoAudio:
    def __init__(self):
        self._m = False

    def setVolume(self, v):
        pass

    def setMuted(self, m):
        self._m = m

    def isMuted(self):
        return self._m


class SeekSlider(QSlider):
    """누른 위치로 바로 이동하는 슬라이더."""

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            v = QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), int(e.position().x()), self.width())
            self.setValue(v)
            self.sliderMoved.emit(v)
        super().mousePressEvent(e)


class PlayerDialog(QDialog):
    closed = Signal()

    def __init__(self, stream, source_for, parent=None):
        super().__init__(parent)
        self.stream = stream            # StreamServer
        self.source_for = source_for    # DriveItem -> 다운로드 소스
        self.url = ""
        self.item = None
        self.tree_item = None
        self.playlist: list = []
        self._last_bytes = 0
        self.setWindowTitle("미리보기")
        self.resize(1100, 720)
        self.setMinimumSize(640, 420)
        self.setStyleSheet(f"QDialog {{ background: #000; }} QLabel {{ color: {style.TEXT}; }}")

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.video = QVideoWidget()
        self.video.setStyleSheet("background: #000;")
        v.addWidget(self.video, 1)

        # 버퍼링 표시 (영상 위)
        self.overlay = QWidget(self.video)
        ol = QHBoxLayout(self.overlay)
        ol.setContentsMargins(14, 8, 14, 8)
        self.spin = Spinner(18)
        self.buf_lbl = QLabel("불러오는 중")
        ol.addWidget(self.spin)
        ol.addWidget(self.buf_lbl)
        self.overlay.setStyleSheet("background: rgba(0,0,0,170); border-radius: 10px;")
        self.overlay.hide()

        bar = QWidget()
        bar.setObjectName("pbar")
        bar.setAttribute(Qt.WA_StyledBackground, True)
        bar.setStyleSheet(f"#pbar {{ background: {style.PANEL}; border-top: 1px solid {style.BORDER}; }}")
        bl = QVBoxLayout(bar)
        bl.setContentsMargins(14, 8, 14, 10)
        bl.setSpacing(6)
        self.seek = SeekSlider(Qt.Horizontal)
        self.seek.setRange(0, 0)
        self.seek.sliderMoved.connect(lambda v: self.player.setPosition(v))
        self.seek.setStyleSheet(
            f"QSlider::groove:horizontal {{ height: 4px; background: #2a2f38; border-radius: 2px; }}"
            f"QSlider::sub-page:horizontal {{ background: {style.ACCENT}; border-radius: 2px; }}"
            "QSlider::handle:horizontal { width: 12px; margin: -5px 0; border-radius: 6px; background: white; }")
        bl.addWidget(self.seek)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.prev_btn = self._tool("prev", "이전 영상 (↑)", self.play_prev)
        self.play_btn = self._tool("pause", "재생/일시정지 (Space)", self.toggle)
        self.next_btn = self._tool("next", "다음 영상 (↓)", self.play_next)
        for b in (self.prev_btn, self.play_btn, self.next_btn):
            row.addWidget(b)
        self.time_lbl = QLabel("0:00 / 0:00")
        row.addSpacing(6)
        row.addWidget(self.time_lbl)
        row.addSpacing(12)
        self.name_lbl = QLabel("")
        self.name_lbl.setStyleSheet("font-weight: 700;")
        row.addWidget(self.name_lbl, 1)
        self.info_lbl = QLabel("")
        self.info_lbl.setStyleSheet(f"color: {style.MUTED};")
        row.addWidget(self.info_lbl)
        self.pick = QCheckBox("다운로드에 포함")
        self.pick.toggled.connect(self._pick_toggled)
        row.addSpacing(10)
        row.addWidget(self.pick)
        self.mute_btn = self._tool("volume", "음소거 (M)", self.toggle_mute)
        row.addWidget(self.mute_btn)
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(80)
        self.vol.setFixedWidth(90)
        self.vol.valueChanged.connect(lambda x: self.audio.setVolume(x / 100))
        row.addWidget(self.vol)
        bl.addLayout(row)
        v.addWidget(bar)

        self.player = QMediaPlayer()
        if os.environ.get("DRIVEPULL_NO_AUDIO"):   # 오디오 장치 없는 테스트 환경용
            self.audio = _NoAudio()
        else:
            self.audio = QAudioOutput()
            self.audio.setVolume(0.8)
            self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.player.durationChanged.connect(lambda d: self.seek.setRange(0, d))
        self.player.positionChanged.connect(self._on_pos)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.errorOccurred.connect(self._on_error)

        self._rate_timer = QTimer(self, interval=250, timeout=self._update_rate)
        self._auto_paused = False
        self._tick = 0
        for key, fn in ((Qt.Key_Space, self.toggle), (Qt.Key_Left, lambda: self.skip(-5000)),
                        (Qt.Key_Right, lambda: self.skip(5000)), (Qt.Key_Up, self.play_prev),
                        (Qt.Key_Down, self.play_next), (Qt.Key_M, self.toggle_mute),
                        (Qt.Key_F, self.toggle_full), (Qt.Key_Escape, self._esc)):
            QShortcut(QKeySequence(key), self, fn)

    def _tool(self, icon, tip, fn):
        b = QToolButton()
        b.setAutoRaise(True)
        b.setIcon(make_icon(icon, "#e8eaef", 18))
        b.setIconSize(QSize(18, 18))
        b.setFixedSize(34, 30)
        b.setToolTip(tip)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(fn)
        return b

    # ---- 재생 대상
    def open_item(self, tree_item, playlist: list):
        """tree_item: 목록의 QTreeWidgetItem (DriveItem 보관), playlist: 같은 폴더의 영상 항목들."""
        from .main_window import ITEM_ROLE
        self.player.stop()
        if self.url:
            self.stream.release(self.url)
        self.tree_item = tree_item
        self.item = it = tree_item.data(0, ITEM_ROLE)
        self.playlist = playlist
        ext = os.path.splitext(it.name)[1].lower()
        self.url = self.stream.register(lambda: self.source_for(it), it.size, MIME.get(ext, "video/quicktime"))
        self._last_bytes = 0
        self._auto_paused = False
        self.name_lbl.setText(it.name)
        self.setWindowTitle(f"미리보기 · {it.name}")
        parts = []
        if it.width:
            parts.append(f"{it.width}×{it.height}")
        if it.size:
            parts.append(fmt_size(it.size))
        if it.size and it.duration_ms:
            mbps = it.size * 8 / (it.duration_ms / 1000) / 1e6
            parts.append(f"{mbps:,.0f} Mbps")
            self._mbps = mbps
        else:
            self._mbps = 0
        self._base_info = " · ".join(parts)
        self.info_lbl.setText(self._base_info)
        self.pick.blockSignals(True)
        self.pick.setChecked(tree_item.checkState(0) == Qt.Checked)
        self.pick.blockSignals(False)
        i = self._index()
        self.prev_btn.setEnabled(i > 0)
        self.next_btn.setEnabled(0 <= i < len(playlist) - 1)
        self._show_overlay("원본에 연결 중")
        self.player.setSource(QUrl(self.url))
        self.player.play()
        self._rate_timer.start()

    def _index(self) -> int:
        try:
            return self.playlist.index(self.tree_item)
        except ValueError:
            return -1

    def play_prev(self):
        i = self._index()
        if i > 0:
            self.open_item(self.playlist[i - 1], self.playlist)

    def play_next(self):
        i = self._index()
        if 0 <= i < len(self.playlist) - 1:
            self.open_item(self.playlist[i + 1], self.playlist)

    # ---- 조작
    def toggle(self):
        if self._auto_paused:   # 버퍼링 대기 중 정지 누르면 그대로 정지 상태 유지
            self._auto_paused = False
            self.overlay.hide()
            self.spin.stop()
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def skip(self, ms: int):
        self.player.setPosition(max(0, min(self.player.duration(), self.player.position() + ms)))

    def toggle_mute(self):
        self.audio.setMuted(not self.audio.isMuted())
        self.mute_btn.setIcon(make_icon("muted" if self.audio.isMuted() else "volume", "#e8eaef", 18))

    def toggle_full(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def _esc(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.close()

    def _pick_toggled(self, on: bool):
        if self.tree_item is not None:
            self.tree_item.setCheckState(0, Qt.Checked if on else Qt.Unchecked)

    # ---- 상태 표시
    def _on_pos(self, pos: int):
        if not self.seek.isSliderDown():
            self.seek.setValue(pos)
        self.time_lbl.setText(f"{fmt_dur(pos)} / {fmt_dur(self.player.duration())}")

    def _on_state(self, st):
        self.play_btn.setIcon(make_icon("pause" if st == QMediaPlayer.PlayingState else "play", "#e8eaef", 18))

    def _on_status(self, st):
        if st in (QMediaPlayer.LoadingMedia, QMediaPlayer.StalledMedia):
            self._show_overlay("버퍼링 중")
        elif st in (QMediaPlayer.BufferedMedia, QMediaPlayer.LoadedMedia, QMediaPlayer.EndOfMedia):
            self.overlay.hide()
            self.spin.stop()

    def _on_error(self, _err, msg):
        e = self.stream.entry_for(self.url)
        detail = e.error if e and e.error else msg
        self._show_overlay(f"재생할 수 없어요: {detail}", spinning=False)

    def _show_overlay(self, text: str, spinning: bool = True):
        self.buf_lbl.setText(text)
        self.overlay.adjustSize()
        self.overlay.move((self.video.width() - self.overlay.width()) // 2,
                          (self.video.height() - self.overlay.height()) // 2)
        self.overlay.show()
        self.overlay.raise_()
        self.spin.start() if spinning else self.spin.stop()

    PREBUF_SEC = 3.0      # 버퍼가 바닥나면 이만큼 쌓일 때까지 멈췄다가 재생 (자잘하게 끊기지 않게)
    LOW_SEC = 0.4

    def _update_rate(self):
        e = self.stream.entry_for(self.url)
        if not e:
            return
        pf = e.pf
        dur = self.player.duration()
        byterate = (e.size / (dur / 1000)) if (e.size and dur > 0) else 0.0
        if pf is not None and byterate and pf.byterate != byterate:
            pf.set_byterate(byterate)
        if pf is None or not byterate:
            return
        pos_bytes = int(self.player.position() / 1000 * byterate)
        ahead_sec = pf.ahead_of(pos_bytes) / byterate
        remaining = max(0.0, (dur - self.player.position()) / 1000)
        target = min(self.PREBUF_SEC, remaining)
        playing = self.player.playbackState() == QMediaPlayer.PlayingState
        if playing and not self._auto_paused and ahead_sec < min(self.LOW_SEC, target) and remaining > 1:
            self._auto_paused = True
            self.player.pause()
        if self._auto_paused:
            pct = int(min(1.0, ahead_sec / target if target else 1) * 100)
            msg = f"버퍼링 {pct}% · 수신 {pf.rate * 8 / 1e6:,.0f} Mbps / 원본 {byterate * 8 / 1e6:,.0f} Mbps"
            if pf.rate and pf.rate < byterate * 0.95:
                msg += "\n회선이 원본보다 느려서 중간중간 버퍼링이 필요해요"
            self._show_overlay(msg)
            if ahead_sec >= target:
                self._auto_paused = False
                self.overlay.hide()
                self.spin.stop()
                self.player.play()
        self._tick += 1
        if self._tick % 4 == 0:
            self.info_lbl.setText(
                f"{self._base_info} · 수신 {pf.rate * 8 / 1e6:,.0f} / 원본 {byterate * 8 / 1e6:,.0f} Mbps"
                f" · 버퍼 {ahead_sec:,.1f}초 · 연결 {len(pf.workers)}")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.overlay.isVisible():
            self._show_overlay(self.buf_lbl.text(), self.spin.isVisible())

    def closeEvent(self, e):
        self._rate_timer.stop()
        self.player.stop()
        self.player.setSource(QUrl())
        if self.url:
            self.stream.release(self.url)
            self.url = ""
        self.closed.emit()
        super().closeEvent(e)
