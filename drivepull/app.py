"""DRIVEPULL 실행 진입점."""
from __future__ import annotations

import os
import sys
import threading

from PySide6.QtCore import QByteArray, QObject, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .config import APP_NAME, Settings, app_dir
from .service import Service
from .ui.main_window import MainWindow
from .ui.style import CHECK_SVG, build_qss


def resource(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    return os.path.join(base, "assets", name)


class _AuthLoaded(QObject):
    done = Signal()


def selftest_player(path: str) -> int:
    """빌드된 exe 에 영상 디코더(FFmpeg 플러그인)가 들어갔는지 CI에서 확인. 성공 0."""
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtMultimedia import QMediaPlayer, QVideoSink
    app = QApplication(sys.argv)
    p = QMediaPlayer()
    sink = QVideoSink()
    p.setVideoSink(sink)
    frames = [0]
    sink.videoFrameChanged.connect(lambda f: frames.__setitem__(0, frames[0] + 1))
    p.setSource(QUrl.fromLocalFile(os.path.abspath(path)))
    p.play()
    result = [3]

    def check():
        if frames[0] > 5 and p.duration() > 0:
            result[0] = 0
            app.quit()

    t = QTimer()
    t.timeout.connect(check)
    t.start(200)
    QTimer.singleShot(15000, app.quit)
    app.exec()
    try:
        with open(os.path.abspath(path) + ".selftest.txt", "w", encoding="utf-8") as f:
            f.write(f"frames={frames[0]} duration={p.duration()} result={result[0]}\n")
    except OSError:
        pass
    return result[0]


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--selftest-player":
        return selftest_player(sys.argv[2])
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("diap.drivepull")
        except Exception:  # noqa: BLE001
            pass
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    check = app_dir() / "check.svg"
    check.write_text(CHECK_SVG, encoding="utf-8")
    app.setStyleSheet(build_qss(str(check)))
    icon = resource("icon.ico")
    if os.path.exists(icon):
        app.setWindowIcon(QIcon(icon))

    settings = Settings.load()
    svc = Service(settings)
    win = MainWindow(svc, settings)
    if settings.window_geometry:
        try:
            win.restoreGeometry(QByteArray.fromHex(settings.window_geometry.encode()))
        except Exception:  # noqa: BLE001
            pass
    win.show()

    # 저장된 로그인 토큰은 백그라운드에서 확인 (오프라인이어도 창이 바로 뜨게)
    sig = _AuthLoaded()
    sig.done.connect(win._update_account)
    threading.Thread(target=lambda: (svc.auth.load(), sig.done.emit()), daemon=True).start()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
