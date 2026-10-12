"""PTT input binding: keyboard keys and joystick/wheel buttons.

Keyboard capture uses ``pynput`` (cross-platform, background-capable).
Joystick/wheel capture uses the Windows Multimedia ``winmm.dll`` API
(``joyGetNumDevs`` / ``joyGetDevCapsW`` / ``joyGetPosEx``) as the
primary backend — it needs no SDL, no display surface, no focus, and
detects DirectInput devices (Moza, Fanatec, Thrustmaster, Logitech)
while the app is in the background. Falls back to ``pygame`` on
non-Windows platforms.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import struct
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from ..logbook import log


class BindingType(Enum):
    KEYBOARD = "keyboard"
    JOYSTICK = "joystick"


@dataclass
class PTTBinding:
    """A bound PTT control."""
    type: BindingType
    key_name: str = ""
    joy_id: int = -1
    button_id: int = -1

    @property
    def display_name(self) -> str:
        if self.type == BindingType.KEYBOARD:
            return f"Key: {self.key_name}"
        return f"Joy {self.joy_id} Btn {self.button_id}"

    def to_dict(self) -> dict:
        return {
            "type": self.type.value,
            "key_name": self.key_name,
            "joy_id": self.joy_id,
            "button_id": self.button_id,
        }

    @staticmethod
    def from_dict(d: dict) -> "PTTBinding":
        return PTTBinding(
            type=BindingType(d.get("type", "keyboard")),
            key_name=d.get("key_name", ""),
            joy_id=d.get("joy_id", -1),
            button_id=d.get("button_id", -1),
        )


# ---------------------------------------------------------------------------
# winmm.dll joystick backend (Windows only)
# ---------------------------------------------------------------------------

_MAXPNAMELEN = 32
_MAX_JOYSTICK_BUTTONS = 32
_JOYERR_NOERROR = 0
_JOY_RETURNBUTTONS = 0x00000080
_JOY_RETURNALL = 0x000000FF


class _JOYCAPSW(ctypes.Structure):
    _fields_ = [
        ("wMid", ctypes.wintypes.WORD),
        ("wPid", ctypes.wintypes.WORD),
        ("szPname", ctypes.wintypes.WCHAR * _MAXPNAMELEN),
        ("wXmin", ctypes.wintypes.UINT),
        ("wXmax", ctypes.wintypes.UINT),
        ("wYmin", ctypes.wintypes.UINT),
        ("wYmax", ctypes.wintypes.UINT),
        ("wZmin", ctypes.wintypes.UINT),
        ("wZmax", ctypes.wintypes.UINT),
        ("wNumButtons", ctypes.wintypes.UINT),
        ("wPeriodMin", ctypes.wintypes.UINT),
        ("wPeriodMax", ctypes.wintypes.UINT),
        ("wRmin", ctypes.wintypes.UINT),
        ("wRmax", ctypes.wintypes.UINT),
        ("wUmin", ctypes.wintypes.UINT),
        ("wUmax", ctypes.wintypes.UINT),
        ("wVmin", ctypes.wintypes.UINT),
        ("wVmax", ctypes.wintypes.UINT),
        ("wCaps", ctypes.wintypes.UINT),
        ("wMaxAxes", ctypes.wintypes.UINT),
        ("wNumAxes", ctypes.wintypes.UINT),
        ("wMaxButtons", ctypes.wintypes.UINT),
        ("szRegKey", ctypes.wintypes.WCHAR * _MAXPNAMELEN),
        ("szOEMVxD", ctypes.wintypes.WCHAR * _MAXPNAMELEN),
    ]


class _JOYINFOEX(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("dwXpos", ctypes.wintypes.DWORD),
        ("dwYpos", ctypes.wintypes.DWORD),
        ("dwZpos", ctypes.wintypes.DWORD),
        ("dwRpos", ctypes.wintypes.DWORD),
        ("dwUpos", ctypes.wintypes.DWORD),
        ("dwVpos", ctypes.wintypes.DWORD),
        ("dwButtons", ctypes.wintypes.DWORD),
        ("dwButtonNumber", ctypes.wintypes.DWORD),
        ("dwPOV", ctypes.wintypes.DWORD),
        ("dwReserved1", ctypes.wintypes.DWORD),
        ("dwReserved2", ctypes.wintypes.DWORD),
    ]


@dataclass
class WinmmJoystick:
    """A joystick discovered via the winmm API."""
    index: int
    name: str
    num_buttons: int


def _load_winmm():
    """Load winmm.dll and set up function prototypes. Returns
    ``(joyGetNumDevs, joyGetDevCapsW, joyGetPosEx)`` or ``None``."""
    if sys.platform != "win32":
        return None
    try:
        winmm = ctypes.WinDLL("winmm", use_last_error=True)
        joyGetNumDevs = winmm.joyGetNumDevs
        joyGetNumDevs.restype = ctypes.wintypes.UINT

        joyGetDevCapsW = winmm.joyGetDevCapsW
        joyGetDevCapsW.argtypes = [
            ctypes.wintypes.UINT,
            ctypes.POINTER(_JOYCAPSW),
            ctypes.wintypes.UINT,
        ]
        joyGetDevCapsW.restype = ctypes.wintypes.UINT

        joyGetPosEx = winmm.joyGetPosEx
        joyGetPosEx.argtypes = [
            ctypes.wintypes.UINT,
            ctypes.POINTER(_JOYINFOEX),
        ]
        joyGetPosEx.restype = ctypes.wintypes.UINT

        return joyGetNumDevs, joyGetDevCapsW, joyGetPosEx
    except (OSError, AttributeError) as e:
        log(f"[ptt/input] winmm.dll load failed: {e}")
        return None


_winmm_funcs = None


def _get_winmm():
    global _winmm_funcs
    if _winmm_funcs is None:
        _winmm_funcs = _load_winmm() or False
    return _winmm_funcs if _winmm_funcs is not False else None


def list_joysticks_winmm() -> list[WinmmJoystick]:
    """Enumerate joysticks via winmm. Works in background, no SDL."""
    funcs = _get_winmm()
    if funcs is None:
        return []
    joyGetNumDevs, joyGetDevCapsW, _joyGetPosEx = funcs
    num = joyGetNumDevs()
    result: list[WinmmJoystick] = []
    for i in range(min(num, 16)):
        caps = _JOYCAPSW()
        if joyGetDevCapsW(i, ctypes.byref(caps),
                          ctypes.sizeof(_JOYCAPSW)) == _JOYERR_NOERROR:
            name = caps.szPname.strip("\x00").strip()
            if name:
                result.append(WinmmJoystick(
                    index=i, name=name,
                    num_buttons=min(caps.wNumButtons, _MAX_JOYSTICK_BUTTONS),
                ))
    return result


def _read_buttons_winmm(joy_id: int) -> int:
    """Read the button bitmask for joystick ``joy_id``. Returns 0 on error."""
    funcs = _get_winmm()
    if funcs is None:
        return 0
    _joyGetNumDevs, _joyGetDevCapsW, joyGetPosEx = funcs
    info = _JOYINFOEX()
    info.dwSize = ctypes.sizeof(_JOYINFOEX)
    info.dwFlags = _JOY_RETURNBUTTONS
    if joyGetPosEx(joy_id, ctypes.byref(info)) == _JOYERR_NOERROR:
        return info.dwButtons
    return 0


# ---------------------------------------------------------------------------
# Unified joystick listing (winmm primary, pygame fallback)
# ---------------------------------------------------------------------------

def list_joysticks() -> list[dict[str, str | int]]:
    """Return available joysticks/wheels with name and button count."""
    joys = list_joysticks_winmm()
    if joys:
        log(f"[ptt/input] winmm: {len(joys)} joystick(s) detected")
        for j in joys:
            log(f"[ptt/input]   [{j.index}] {j.name} ({j.num_buttons} buttons)")
        return [{"index": j.index, "name": j.name, "buttons": j.num_buttons}
                for j in joys]
    try:
        import pygame  # type: ignore[import-untyped]
        pygame.init()
        pygame.joystick.init()
        result = []
        for i in range(pygame.joystick.get_count()):
            j = pygame.joystick.Joystick(i)
            j.init()
            result.append({
                "index": i,
                "name": j.get_name(),
                "buttons": j.get_numbuttons(),
            })
        if result:
            log(f"[ptt/input] pygame: {len(result)} joystick(s) detected")
        return result
    except (ImportError, Exception):
        pass
    log("[ptt/input] no joystick backend available")
    return []


# ---------------------------------------------------------------------------
# InputListener — keyboard + joystick
# ---------------------------------------------------------------------------

class InputListener:
    """Listens for keyboard and joystick input for PTT activation.

    Two modes of operation:

    1. **Assign mode**: captures the next key/button press and returns it
       via a callback, then exits assign mode.
    2. **Active mode**: fires ``on_press`` / ``on_release`` callbacks when
       the bound control is pressed/released.
    """

    def __init__(self) -> None:
        self._binding: Optional[PTTBinding] = None
        self._on_press: Optional[Callable[[], None]] = None
        self._on_release: Optional[Callable[[], None]] = None
        self._assign_callback: Optional[Callable[[PTTBinding], None]] = None
        self._keyboard_listener = None
        self._joy_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._pressed = False

    @property
    def binding(self) -> Optional[PTTBinding]:
        return self._binding

    def set_binding(self, binding: Optional[PTTBinding]) -> None:
        self._binding = binding

    def start(self, on_press: Callable[[], None],
              on_release: Callable[[], None]) -> None:
        """Start listening for the bound control."""
        self._on_press = on_press
        self._on_release = on_release
        self._stop.clear()
        self._start_keyboard_listener()
        self._start_joystick_listener()

    def stop(self) -> None:
        self._stop.set()
        self._stop_keyboard_listener()
        if self._joy_thread is not None:
            self._joy_thread.join(timeout=2.0)
            self._joy_thread = None

    def start_assign(self, callback: Callable[[PTTBinding], None]) -> None:
        """Enter assign mode: next key/button press is captured and
        passed to ``callback`` as a ``PTTBinding``."""
        self._assign_callback = callback
        self._stop.clear()
        self._start_keyboard_listener()
        self._start_joystick_listener()

    def stop_assign(self) -> None:
        self._assign_callback = None
        self.stop()

    # ---- keyboard (pynput) ----

    def _start_keyboard_listener(self) -> None:
        self._stop_keyboard_listener()
        try:
            from pynput import keyboard  # type: ignore[import-untyped]
            self._keyboard_listener = keyboard.Listener(
                on_press=self._on_key_press,
                on_release=self._on_key_release,
            )
            self._keyboard_listener.start()
        except (ImportError, Exception) as e:
            log(f"[ptt/input] keyboard listener unavailable: {e}")

    def _stop_keyboard_listener(self) -> None:
        if self._keyboard_listener is not None:
            try:
                self._keyboard_listener.stop()
            except Exception:
                pass
            self._keyboard_listener = None

    def _on_key_press(self, key) -> None:
        try:
            key_name = key.char if hasattr(key, "char") and key.char else str(key)
        except AttributeError:
            key_name = str(key)
        key_name = key_name.replace("Key.", "")

        if self._assign_callback is not None:
            binding = PTTBinding(type=BindingType.KEYBOARD, key_name=key_name)
            cb = self._assign_callback
            self._assign_callback = None
            self.stop()
            cb(binding)
            return

        if (self._binding and self._binding.type == BindingType.KEYBOARD
                and self._binding.key_name == key_name):
            if not self._pressed:
                self._pressed = True
                if self._on_press:
                    self._on_press()

    def _on_key_release(self, key) -> None:
        try:
            key_name = key.char if hasattr(key, "char") and key.char else str(key)
        except AttributeError:
            key_name = str(key)
        key_name = key_name.replace("Key.", "")

        if (self._binding and self._binding.type == BindingType.KEYBOARD
                and self._binding.key_name == key_name):
            if self._pressed:
                self._pressed = False
                if self._on_release:
                    self._on_release()

    # ---- joystick (winmm primary, pygame fallback) ----

    def _start_joystick_listener(self) -> None:
        if self._joy_thread is not None:
            return
        self._joy_thread = threading.Thread(
            target=self._joystick_loop, name="ptt-joystick", daemon=True)
        self._joy_thread.start()

    def _joystick_loop(self) -> None:
        if _get_winmm() is not None:
            self._winmm_loop()
        else:
            self._pygame_loop()

    def _winmm_loop(self) -> None:
        """Poll joystick buttons via winmm.dll (background-safe)."""
        joys = list_joysticks_winmm()
        if not joys:
            log("[ptt/input] winmm: no joysticks for polling")
            return
        prev_buttons: dict[int, int] = {}
        while not self._stop.is_set():
            for joy in joys:
                buttons = _read_buttons_winmm(joy.index)
                prev = prev_buttons.get(joy.index, 0)
                changed = buttons ^ prev
                if changed:
                    for btn in range(joy.num_buttons):
                        mask = 1 << btn
                        if not (changed & mask):
                            continue
                        now_pressed = bool(buttons & mask)
                        was_pressed = bool(prev & mask)
                        if now_pressed and not was_pressed:
                            if self._assign_callback is not None:
                                binding = PTTBinding(
                                    type=BindingType.JOYSTICK,
                                    joy_id=joy.index,
                                    button_id=btn,
                                )
                                cb = self._assign_callback
                                self._assign_callback = None
                                self.stop()
                                cb(binding)
                                return
                            if (self._binding
                                    and self._binding.type == BindingType.JOYSTICK
                                    and self._binding.joy_id == joy.index
                                    and self._binding.button_id == btn):
                                self._pressed = True
                                if self._on_press:
                                    self._on_press()
                        elif not now_pressed and was_pressed:
                            if (self._binding
                                    and self._binding.type == BindingType.JOYSTICK
                                    and self._binding.joy_id == joy.index
                                    and self._binding.button_id == btn):
                                self._pressed = False
                                if self._on_release:
                                    self._on_release()
                prev_buttons[joy.index] = buttons
            self._stop.wait(0.01)

    def _pygame_loop(self) -> None:
        """Fallback: poll via pygame (needs SDL, may have focus issues)."""
        try:
            import pygame  # type: ignore[import-untyped]
        except ImportError:
            log("[ptt/input] pygame not available for joystick input")
            return
        try:
            pygame.init()
            pygame.joystick.init()
        except Exception as e:
            log(f"[ptt/input] pygame init failed: {e}")
            return

        prev_states: dict[tuple[int, int], bool] = {}
        while not self._stop.is_set():
            try:
                pygame.event.pump()
                for joy_idx in range(pygame.joystick.get_count()):
                    joy = pygame.joystick.Joystick(joy_idx)
                    joy.init()
                    for btn_idx in range(joy.get_numbuttons()):
                        pressed = joy.get_button(btn_idx)
                        key = (joy_idx, btn_idx)
                        was_pressed = prev_states.get(key, False)

                        if pressed and not was_pressed:
                            if self._assign_callback is not None:
                                binding = PTTBinding(
                                    type=BindingType.JOYSTICK,
                                    joy_id=joy_idx,
                                    button_id=btn_idx,
                                )
                                cb = self._assign_callback
                                self._assign_callback = None
                                self.stop()
                                cb(binding)
                                return
                            if (self._binding
                                    and self._binding.type == BindingType.JOYSTICK
                                    and self._binding.joy_id == joy_idx
                                    and self._binding.button_id == btn_idx):
                                self._pressed = True
                                if self._on_press:
                                    self._on_press()

                        elif not pressed and was_pressed:
                            if (self._binding
                                    and self._binding.type == BindingType.JOYSTICK
                                    and self._binding.joy_id == joy_idx
                                    and self._binding.button_id == btn_idx):
                                self._pressed = False
                                if self._on_release:
                                    self._on_release()

                        prev_states[key] = pressed
            except Exception:
                pass
            self._stop.wait(0.02)
