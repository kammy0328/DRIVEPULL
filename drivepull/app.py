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


def main() -> int:
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
