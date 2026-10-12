"""Dedicated pygame joystick worker.

All ``pygame`` / SDL calls (init, ``event.pump``, ``get_button``) run on
one daemon thread. The GUI thread never touches pygame — it only reads a
cached device list or receives press/release callbacks (which must be
non-blocking, e.g. a queued Qt signal).

No ``pygame.display.set_mode``: creating an SDL window while Qt owns the
Win32 message loop freezes the app.

FFB-safe: only ``pygame.joystick.init`` (never the full pygame init or
haptic). HIDAPI / raw-input / WGI hints are forced off so SDL does not
open a Moza wheel and send reports that invert iRacing FFB.
"""
from __future__ import annotations

import os
import threading
from typing import Callable, Optional

from ..logbook import log
from .ffb_safe import apply_sdl_ffb_safe_hints

apply_sdl_ffb_safe_hints()

JoyEdgeCB = Callable[[int, str, int, bool], None]


class PygameJoyWorker:
    """Owns the pygame joystick module on a single background thread."""

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._subs: list[JoyEdgeCB] = []
        self._devices: list[dict] = []
        self._started = False

    def ensure_started(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="pygame-joy", daemon=True)
            self._thread.start()
            self._started = True

    def cached_devices(self) -> list[dict]:
        with self._lock:
            return list(self._devices)

    def subscribe(self, cb: JoyEdgeCB) -> None:
        with self._lock:
            if cb not in self._subs:
                self._subs.append(cb)

    def unsubscribe(self, cb: JoyEdgeCB) -> None:
        with self._lock:
            if cb in self._subs:
                self._subs.remove(cb)

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        apply_sdl_ffb_safe_hints()
        os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")
        try:
            import pygame
            # Joystick only. Full pygame init would start haptic and invert FFB.
            pygame.joystick.init()
        except (ImportError, Exception) as e:
            log(f"[ptt/pygame] init failed: {e}")
            return
        haptic_on = False
        try:
            haptic = getattr(pygame, "haptic", None)
            if haptic is not None and hasattr(haptic, "get_init"):
                haptic_on = bool(haptic.get_init())
                if haptic_on and hasattr(haptic, "quit"):
                    haptic.quit()
                    haptic_on = bool(haptic.get_init())
        except Exception:
            haptic_on = False
        log("[ptt/pygame] worker started (joystick only, no display, "
            f"haptic={'on' if haptic_on else 'off'}, HIDAPI=0, RAWINPUT=0)")
        sticks: list = []
        prev: dict[tuple[int, int], bool] = {}
        try:
            while not self._stop.is_set():
                try:
                    pygame.event.pump()
                    n = pygame.joystick.get_count()
                    if len(sticks) != n:
                        sticks = []
                        snapshot = []
                        for i in range(n):
                            j = pygame.joystick.Joystick(i)
                            j.init()
                            sticks.append(j)
                            snapshot.append({
                                "index": i,
                                "name": j.get_name(),
                                "buttons": j.get_numbuttons(),
                                "backend": "pygame",
                            })
                        with self._lock:
                            self._devices = snapshot
                        log(f"[ptt/pygame] {n} device(s): "
                            + ", ".join(f"{d['name']} ({d['buttons']} btn)"
                                        for d in snapshot))
                    subs: list[JoyEdgeCB]
                    with self._lock:
                        subs = list(self._subs)
                    for ji, joy in enumerate(sticks):
                        name = ""
                        try:
                            name = joy.get_name()
                            nbtn = joy.get_numbuttons()
                        except Exception:
                            continue
                        for bi in range(nbtn):
                            try:
                                pressed = bool(joy.get_button(bi))
                            except Exception:
                                continue
                            key = (ji, bi)
                            was = prev.get(key, False)
                            if pressed != was:
                                for cb in subs:
                                    try:
                                        cb(ji, name, bi, pressed)
                                    except Exception as e:
                                        log(f"[ptt/pygame] subscriber error: {e}")
                            prev[key] = pressed
                except Exception as e:
                    log(f"[ptt/pygame] poll error: {e}")
                self._stop.wait(0.02)
        finally:
            log("[ptt/pygame] worker stopped")


class FakeJoyBackend:
    """Test double: emit edges from any thread without pygame."""

    def __init__(self, devices: list[dict] | None = None) -> None:
        self._subs: list[JoyEdgeCB] = []
        self._devices = devices or [{
            "index": 0, "name": "MOZA R16 Ultra Base",
            "buttons": 128, "backend": "pygame",
        }]
        self.started = False

    def ensure_started(self) -> None:
        self.started = True

    def cached_devices(self) -> list[dict]:
        return list(self._devices)

    def subscribe(self, cb: JoyEdgeCB) -> None:
        if cb not in self._subs:
            self._subs.append(cb)

    def unsubscribe(self, cb: JoyEdgeCB) -> None:
        if cb in self._subs:
            self._subs.remove(cb)

    def stop(self) -> None:
        self._subs.clear()

    def emit(self, device: int, button: int, pressed: bool,
             name: str = "MOZA R16 Ultra Base") -> None:
        for cb in list(self._subs):
            cb(device, name, button, pressed)


_WORKER: Optional[PygameJoyWorker] = None
_WORKER_LOCK = threading.Lock()


def get_pygame_worker() -> PygameJoyWorker:
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is None:
            _WORKER = PygameJoyWorker()
        return _WORKER
