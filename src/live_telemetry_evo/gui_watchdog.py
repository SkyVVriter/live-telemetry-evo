"""GUI-stall watchdog: dump every thread stack if Qt stops pumping.

A QTimer on the GUI thread re-arms ``faulthandler.dump_traceback_later``
every ~0.75 s. If the GUI thread freezes for more than ``STALL_S``
seconds the timer cannot pet the dog, and faulthandler writes all
thread stacks to ``session.log``.
"""
from __future__ import annotations

import faulthandler
from typing import Optional

from .logbook import get_log_path, log

STALL_S = 3.0
_PET_MS = 750

_hang_fp = None
_timer = None


def _hang_file():
    """Append handle to session.log for faulthandler (must stay open)."""
    global _hang_fp
    if _hang_fp is not None:
        return _hang_fp
    path = get_log_path()
    if path is None:
        return None
    try:
        _hang_fp = open(path, "a", encoding="utf-8", buffering=1)
        return _hang_fp
    except OSError as e:
        log(f"[watchdog] cannot open log for hang dumps: {e}")
        return None


def _arm() -> None:
    fp = _hang_file()
    if fp is None:
        return
    try:
        faulthandler.cancel_dump_traceback_later()
    except Exception:
        pass
    try:
        faulthandler.dump_traceback_later(STALL_S, repeat=False, file=fp)
    except Exception as e:
        log(f"[watchdog] dump_traceback_later failed: {e}")


def start_gui_watchdog(parent) -> Optional[object]:
    """Start the pet-timer on ``parent`` (a QObject on the GUI thread)."""
    global _timer
    try:
        from PySide6.QtCore import QTimer
    except Exception as e:
        log(f"[watchdog] Qt unavailable: {e}")
        return None
    _arm()
    timer = QTimer(parent)
    timer.setInterval(_PET_MS)
    timer.timeout.connect(_arm)
    timer.start()
    _timer = timer
    log(f"[watchdog] GUI hang dump armed ({STALL_S:.0f}s, "
        "stacks → session.log)")
    return timer
