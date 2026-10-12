"""PTT input binding: keyboard keys and joystick/wheel buttons.

Keyboard capture uses ``pynput`` (cross-platform, background-capable).
Joystick/wheel capture uses three backends in priority order:

1. **DirectInput8** (``dinput.py``): raw ctypes COM, sees all HID game
   controllers (Moza, Fanatec, Thrustmaster …), background-safe via
   ``DISCL_BACKGROUND | DISCL_NONEXCLUSIVE``, supports 128 buttons.
2. **pygame** (SDL2): fallback when DirectInput cannot poll (it sees
   128 buttons on devices like the Moza R16). Used on non-Windows too.
3. **winmm** (``joyGetPosEx``): legacy Windows Multimedia API, up to
   16 devices × 32 buttons. Last resort for older mapper devices.

Devices are freshly enumerated each time ``list_joysticks()`` is called
(handles hotplug).
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
# winmm.dll joystick backend (Windows only, legacy devices)
# ---------------------------------------------------------------------------
_MAXPNAMELEN = 32
_MAX_JOYSTICK_BUTTONS = 32
_JOYERR_NOERROR = 0
_JOY_RETURNBUTTONS = 0x00000080


_MAX_JOYSTICKOEMVXDNAME = 260

class _JOYCAPSW(ctypes.Structure):
    _fields_ = [
        ("wMid", ctypes.wintypes.WORD),
        ("wPid", ctypes.wintypes.WORD),
        ("szPname", ctypes.wintypes.WCHAR * _MAXPNAMELEN),
        ("wXmin", ctypes.wintypes.UINT), ("wXmax", ctypes.wintypes.UINT),
        ("wYmin", ctypes.wintypes.UINT), ("wYmax", ctypes.wintypes.UINT),
        ("wZmin", ctypes.wintypes.UINT), ("wZmax", ctypes.wintypes.UINT),
        ("wNumButtons", ctypes.wintypes.UINT),
        ("wPeriodMin", ctypes.wintypes.UINT),
        ("wPeriodMax", ctypes.wintypes.UINT),
        ("wRmin", ctypes.wintypes.UINT), ("wRmax", ctypes.wintypes.UINT),
        ("wUmin", ctypes.wintypes.UINT), ("wUmax", ctypes.wintypes.UINT),
        ("wVmin", ctypes.wintypes.UINT), ("wVmax", ctypes.wintypes.UINT),
        ("wCaps", ctypes.wintypes.UINT), ("wMaxAxes", ctypes.wintypes.UINT),
        ("wNumAxes", ctypes.wintypes.UINT),
        ("wMaxButtons", ctypes.wintypes.UINT),
        ("szRegKey", ctypes.wintypes.WCHAR * _MAXPNAMELEN),
        ("szOEMVxD", ctypes.wintypes.WCHAR * _MAX_JOYSTICKOEMVXDNAME),
    ]


class _JOYINFOEX(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("dwXpos", ctypes.wintypes.DWORD), ("dwYpos", ctypes.wintypes.DWORD),
        ("dwZpos", ctypes.wintypes.DWORD), ("dwRpos", ctypes.wintypes.DWORD),
        ("dwUpos", ctypes.wintypes.DWORD), ("dwVpos", ctypes.wintypes.DWORD),
        ("dwButtons", ctypes.wintypes.DWORD),
        ("dwButtonNumber", ctypes.wintypes.DWORD),
        ("dwPOV", ctypes.wintypes.DWORD),
        ("dwReserved1", ctypes.wintypes.DWORD),
        ("dwReserved2", ctypes.wintypes.DWORD),
    ]


@dataclass
class WinmmJoystick:
    index: int
    name: str
    num_buttons: int


_winmm_funcs = None

def _get_winmm():
    global _winmm_funcs
    if _winmm_funcs is None:
        if sys.platform != "win32":
            _winmm_funcs = False
            return None
        try:
            winmm = ctypes.WinDLL("winmm", use_last_error=True)
            jgnd = winmm.joyGetNumDevs
            jgnd.restype = ctypes.wintypes.UINT
            jgdc = winmm.joyGetDevCapsW
            jgdc.argtypes = [ctypes.wintypes.UINT,
                             ctypes.POINTER(_JOYCAPSW), ctypes.wintypes.UINT]
            jgdc.restype = ctypes.wintypes.UINT
            jgpe = winmm.joyGetPosEx
            jgpe.argtypes = [ctypes.wintypes.UINT,
                             ctypes.POINTER(_JOYINFOEX)]
            jgpe.restype = ctypes.wintypes.UINT
            _winmm_funcs = (jgnd, jgdc, jgpe)
        except (OSError, AttributeError):
            _winmm_funcs = False
    return _winmm_funcs if _winmm_funcs is not False else None


def list_joysticks_winmm() -> list[WinmmJoystick]:
    funcs = _get_winmm()
    if funcs is None:
        return []
    joyGetNumDevs, joyGetDevCapsW, joyGetPosEx = funcs
    num = joyGetNumDevs()
    log(f"[ptt/input] winmm: joyGetNumDevs={num}, "
        f"sizeof(JOYCAPSW)={ctypes.sizeof(_JOYCAPSW)}, "
        f"sizeof(JOYINFOEX)={ctypes.sizeof(_JOYINFOEX)}")
    result = []
    for i in range(min(num, 16)):
        caps = _JOYCAPSW()
        rc = joyGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(_JOYCAPSW))
        if rc == _JOYERR_NOERROR:
            name = caps.szPname.strip("\x00").strip()
            if name:
                result.append(WinmmJoystick(
                    i, name, min(caps.wNumButtons, _MAX_JOYSTICK_BUTTONS)))
                log(f"[ptt/input] winmm [{i}]: {name} ({caps.wNumButtons} btn)")
        else:
            info = _JOYINFOEX()
            info.dwSize = ctypes.sizeof(_JOYINFOEX)
            info.dwFlags = _JOY_RETURNBUTTONS
            pos_rc = joyGetPosEx(i, ctypes.byref(info)) if joyGetPosEx else 999
            if pos_rc == _JOYERR_NOERROR:
                log(f"[ptt/input] winmm [{i}]: caps failed (rc={rc}) "
                    f"but joyGetPosEx OK (buttons=0x{info.dwButtons:08X})")
    return result


def _read_buttons_winmm(joy_id: int) -> int:
    funcs = _get_winmm()
    if funcs is None:
        return 0
    _, _, joyGetPosEx = funcs
    info = _JOYINFOEX()
    info.dwSize = ctypes.sizeof(_JOYINFOEX)
    info.dwFlags = _JOY_RETURNBUTTONS
    if joyGetPosEx(joy_id, ctypes.byref(info)) == _JOYERR_NOERROR:
        return info.dwButtons
    return 0


# ---------------------------------------------------------------------------
# Unified joystick listing: DI8 (if pollable) -> pygame -> winmm
# ---------------------------------------------------------------------------

@dataclass
class JoystickInfo:
    """Unified joystick descriptor across backends."""
    index: int
    name: str
    buttons: int
    backend: str
    vid_pid: str = ""


def list_joysticks() -> list[dict]:
    """Enumerate joysticks: DI8 if it can poll, else pygame, else winmm."""
    result = _list_dinput()
    if result and _dinput_can_poll(result):
        log(f"[ptt/input] DirectInput8: {len(result)} device(s)")
        for j in result:
            log(f"[ptt/input]   [{j['index']}] {j['name']} "
                f"({j['buttons']} btn, {j.get('vid_pid', '')})")
        return result
    if result:
        log(f"[ptt/input] DirectInput8 enumerated {len(result)} device(s) "
            "but none can be polled — falling back to pygame")

    result = _list_pygame()
    if result:
        log(f"[ptt/input] pygame: {len(result)} device(s)")
        for j in result:
            log(f"[ptt/input]   [{j['index']}] {j['name']} ({j['buttons']} btn)")
        return result

    result = _list_winmm()
    if result:
        log(f"[ptt/input] winmm: {len(result)} device(s)")
        for j in result:
            log(f"[ptt/input]   [{j['index']}] {j['name']} ({j['buttons']} btn)")
        return result

    log("[ptt/input] no joystick backend found devices")
    return []


def _dinput_can_poll(listed: list[dict]) -> bool:
    """True if at least one DI device can be opened, or none need probing."""
    real = [j.get("_dinput_dev") for j in listed if j.get("_dinput_dev") is not None]
    if not real:
        return bool(listed)
    try:
        from .dinput import dinput_any_open
        return dinput_any_open(real)
    except (ImportError, OSError) as e:
        log(f"[ptt/input] DirectInput8 open probe failed: {e}")
        return False


def _list_dinput() -> list[dict]:
    try:
        from .dinput import list_devices_dinput
        devs = list_devices_dinput()
        return [{"index": i, "name": d.display_name,
                 "buttons": d.num_buttons, "backend": "dinput8",
                 "vid_pid": d.vid_pid, "_dinput_dev": d}
                for i, d in enumerate(devs)]
    except (ImportError, OSError) as e:
        log(f"[ptt/input] DirectInput8 unavailable: {e}")
        return []


def _list_winmm() -> list[dict]:
    joys = list_joysticks_winmm()
    return [{"index": j.index, "name": j.name,
             "buttons": j.num_buttons, "backend": "winmm"}
            for j in joys]


def _ensure_pygame():
    """Init pygame joystick, allowing background events without a flashy window."""
    import os
    os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")
    import pygame
    pygame.init()
    try:
        pygame.display.init()
        flags = getattr(pygame, "HIDDEN", 0) | getattr(pygame, "NOFRAME", 0)
        if flags:
            pygame.display.set_mode((1, 1), flags)
    except Exception as e:
        log(f"[ptt/input] pygame display init skipped: {e}")
    pygame.joystick.init()
    return pygame


def _list_pygame() -> list[dict]:
    try:
        pygame = _ensure_pygame()
        result = []
        for i in range(pygame.joystick.get_count()):
            j = pygame.joystick.Joystick(i)
            j.init()
            result.append({"index": i, "name": j.get_name(),
                           "buttons": j.get_numbuttons(),
                           "backend": "pygame"})
        return result
    except (ImportError, Exception) as e:
        log(f"[ptt/input] pygame list failed: {e}")
        return []


def run_diagnostics() -> str:
    """Run all backends and return a diagnostic report string."""
    lines = ["=== Joystick Diagnostics ===", ""]

    lines.append("--- DirectInput8 ---")
    try:
        from .dinput import list_devices_dinput
        devs = list_devices_dinput()
        if devs:
            for d in devs:
                lines.append(
                    f"  {d.display_name} (VID:PID={d.vid_pid}, "
                    f"type=0x{d.dev_type:04X})")
        else:
            lines.append("  (no devices)")
    except (ImportError, OSError) as e:
        lines.append(f"  ERROR: {e}")

    lines.append("")
    lines.append("--- winmm (joyGetDevCapsW) ---")
    joys = list_joysticks_winmm()
    if joys:
        for j in joys:
            lines.append(f"  [{j.index}] {j.name} ({j.num_buttons} buttons)")
    else:
        lines.append("  (no devices)")

    lines.append("")
    lines.append("--- pygame (SDL2) ---")
    try:
        pygame = _ensure_pygame()
        n = pygame.joystick.get_count()
        if n:
            for i in range(n):
                j = pygame.joystick.Joystick(i)
                j.init()
                lines.append(f"  [{i}] {j.get_name()} ({j.get_numbuttons()} btn)")
        else:
            lines.append("  (no devices)")
    except (ImportError, Exception) as e:
        lines.append(f"  ERROR: {e}")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# InputListener — keyboard + joystick
# ---------------------------------------------------------------------------

class InputListener:
    """Listens for keyboard and joystick input for PTT activation."""

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
        self._on_press = on_press
        self._on_release = on_release
        self._stop.clear()
        self._start_keyboard_listener()
        self._start_joystick_listener()

    def stop(self) -> None:
        self._stop.set()
        self._stop_keyboard_listener()
        t = self._joy_thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
            self._joy_thread = None
        elif t is threading.current_thread():
            self._joy_thread = None

    def start_assign(self, callback: Callable[[PTTBinding], None]) -> None:
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
            from pynput import keyboard
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

    # ---- joystick polling ----

    def _start_joystick_listener(self) -> None:
        if self._joy_thread is not None:
            return
        self._joy_thread = threading.Thread(
            target=self._joystick_loop, name="ptt-joystick", daemon=True)
        self._joy_thread.start()

    def _joystick_loop(self) -> None:
        joys = list_joysticks()
        if not joys:
            return

        backend = joys[0].get("backend")
        if backend == "dinput8":
            dinput_devs = [j.get("_dinput_dev") for j in joys
                           if j.get("backend") == "dinput8"]
            opened = False
            try:
                from .dinput import dinput_any_open
                real = [d for d in dinput_devs if d is not None]
                opened = bool(real) and dinput_any_open(real)
            except (ImportError, OSError) as e:
                log(f"[ptt/input] DirectInput8 unavailable for poll: {e}")
            if opened:
                self._dinput_poll_loop(joys, dinput_devs)
                return
            log("[ptt/input] DirectInput8 poll failed — falling back to pygame")
            self._pygame_poll_loop()
            return
        if backend == "winmm":
            self._winmm_poll_loop(joys)
            return
        self._pygame_poll_loop()

    def _dinput_poll_loop(self, joys, dinput_devs):
        from .dinput import read_device_buttons
        prev: dict[int, list[bool]] = {}
        while not self._stop.is_set():
            for idx, dev in enumerate(dinput_devs):
                if dev is None:
                    continue
                buttons = read_device_buttons(dev)
                if not buttons:
                    continue
                old = prev.get(idx, [False] * len(buttons))
                for btn_id, (now, was) in enumerate(zip(buttons, old)):
                    if now and not was:
                        if self._assign_callback is not None:
                            b = PTTBinding(type=BindingType.JOYSTICK,
                                           joy_id=idx, button_id=btn_id)
                            cb = self._assign_callback
                            self._assign_callback = None
                            self.stop()
                            cb(b)
                            return
                        if (self._binding
                                and self._binding.type == BindingType.JOYSTICK
                                and self._binding.joy_id == idx
                                and self._binding.button_id == btn_id):
                            self._pressed = True
                            if self._on_press:
                                self._on_press()
                    elif not now and was:
                        if (self._binding
                                and self._binding.type == BindingType.JOYSTICK
                                and self._binding.joy_id == idx
                                and self._binding.button_id == btn_id):
                            self._pressed = False
                            if self._on_release:
                                self._on_release()
                prev[idx] = buttons
            self._stop.wait(0.01)

    def _winmm_poll_loop(self, joys):
        winmm_joys = list_joysticks_winmm()
        if not winmm_joys:
            return
        prev_buttons: dict[int, int] = {}
        while not self._stop.is_set():
            for joy in winmm_joys:
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
                                b = PTTBinding(type=BindingType.JOYSTICK,
                                               joy_id=joy.index,
                                               button_id=btn)
                                cb = self._assign_callback
                                self._assign_callback = None
                                self.stop()
                                cb(b)
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

    def _pygame_poll_loop(self):
        try:
            pygame = _ensure_pygame()
        except (ImportError, Exception) as e:
            log(f"[ptt/input] pygame poll init failed: {e}")
            return
        sticks = []
        try:
            for i in range(pygame.joystick.get_count()):
                joy = pygame.joystick.Joystick(i)
                joy.init()
                sticks.append(joy)
        except Exception as e:
            log(f"[ptt/input] pygame joystick init failed: {e}")
            return
        if not sticks:
            log("[ptt/input] pygame poll: 0 devices")
            return
        log(f"[ptt/input] pygame poll: {len(sticks)} device(s), "
            f"{sticks[0].get_numbuttons()} btn on [0]")
        prev_states: dict[tuple[int, int], bool] = {}
        err_logged = False
        while not self._stop.is_set():
            try:
                pygame.event.pump()
                for ji, joy in enumerate(sticks):
                    nbtn = joy.get_numbuttons()
                    for bi in range(nbtn):
                        pressed = bool(joy.get_button(bi))
                        key = (ji, bi)
                        was = prev_states.get(key, False)
                        if pressed == was:
                            prev_states[key] = pressed
                            continue
                        name = ""
                        try:
                            name = joy.get_name()
                        except Exception:
                            name = f"joy{ji}"
                        state = "pressed" if pressed else "released"
                        log(f"[ptt/input] pygame event device={ji} "
                            f"name={name!r} button={bi} {state}")
                        if pressed and not was:
                            if self._assign_callback is not None:
                                b = PTTBinding(type=BindingType.JOYSTICK,
                                               joy_id=ji, button_id=bi)
                                cb = self._assign_callback
                                self._assign_callback = None
                                log(f"[ptt/input] pygame assign device={ji} "
                                    f"name={name!r} button={bi}")
                                self.stop()
                                cb(b)
                                return
                            if (self._binding
                                    and self._binding.type == BindingType.JOYSTICK
                                    and self._binding.joy_id == ji
                                    and self._binding.button_id == bi):
                                self._pressed = True
                                if self._on_press:
                                    self._on_press()
                        elif not pressed and was:
                            if (self._binding
                                    and self._binding.type == BindingType.JOYSTICK
                                    and self._binding.joy_id == ji
                                    and self._binding.button_id == bi):
                                self._pressed = False
                                if self._on_release:
                                    self._on_release()
                        prev_states[key] = pressed
            except Exception as e:
                if not err_logged:
                    log(f"[ptt/input] pygame poll error: {e}")
                    err_logged = True
            self._stop.wait(0.02)
