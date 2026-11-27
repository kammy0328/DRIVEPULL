"""다운로드 중 PC 절전 방지 (Windows). 화면 꺼짐은 막지 않고 시스템 절전만 막는다."""
from __future__ import annotations

import sys

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
_state = False


def keep_awake(on: bool) -> bool:
    """상태가 바뀔 때만 호출해도 되고, 같은 값을 여러 번 불러도 안전. 성공하면 True."""
    global _state
    if on == _state:
        return True
    if sys.platform != "win32":
        _state = on
        return True
    try:
        import ctypes
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
        ok = ctypes.windll.kernel32.SetThreadExecutionState(flags) != 0
    except Exception:  # noqa: BLE001
        return False
    if ok:
        _state = on
    return ok


def is_awake_held() -> bool:
    return _state
