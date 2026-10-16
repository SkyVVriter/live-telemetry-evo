"""PTT input binding: keyboard keys and joystick/wheel buttons.

Keyboard capture uses ``pynput`` (cross-platform, background-capable).
Wheel/joystick polling uses exactly one backend, from the
``wheel_backend`` setting (default ``winmm``):

- **winmm** (``joyGetPosEx``): read-only, cannot touch FFB. The generic
  32-button mapper entry is typically the Moza R16. Safe default.
  DWORD mask = 32 buttons (``joyGetPosEx`` cannot see 33–128).
- **rawhid**: Windows Raw Input + ``HidP_GetUsages``. 128 buttons.
  Never ``CreateFile`` / ``WriteFile`` / XInput / DirectInput Acquire.
- **DirectInput8**: ``DISCL_NONEXCLUSIVE | DISCL_BACKGROUND`` only,
  128 buttons. Never exclusive, never sends effects. Can invert FFB.
- **pygame** (SDL2): joystick-only init, HIDAPI/raw-input off. Can
  still disturb FFB on some wheels — opt-in only.
- **off**: keyboard PTT only.

No automatic fallback to pygame/DirectInput: that inverted R16 FFB in
iRacing under test.12. Devices are re-enumerated on each
``list_joysticks()`` call (hotplug).
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
from .ffb_safe import DEFAULT_WHEEL_BACKEND, resolve_wheel_backend
from .pygame_joy import FakeJoyBackend, get_pygame_worker  # noqa: F401


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


def list_joysticks_winmm(*, verbose: bool = True) -> list[WinmmJoystick]:
    funcs = _get_winmm()
    if funcs is None:
        return []
    joyGetNumDevs, joyGetDevCapsW, joyGetPosEx = funcs
    num = joyGetNumDevs()
    if verbose:
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
                if verbose:
                    log(f"[ptt/input] winmm [{i}]: {name} ({caps.wNumButtons} btn)")
        else:
            info = _JOYINFOEX()
            info.dwSize = ctypes.sizeof(_JOYINFOEX)
            info.dwFlags = _JOY_RETURNBUTTONS
            pos_rc = joyGetPosEx(i, ctypes.byref(info)) if joyGetPosEx else 999
            if pos_rc == _JOYERR_NOERROR and verbose:
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
# Unified joystick listing — one backend, never auto-fallback (FFB-safe)
# ---------------------------------------------------------------------------

@dataclass
class JoystickInfo:
    """Unified joystick descriptor across backends."""
    index: int
    name: str
    buttons: int
    backend: str
    vid_pid: str = ""


def list_joysticks(backend: str | None = None) -> list[dict]:
    """Enumerate joysticks for one backend (default: the saved setting)."""
    chosen = resolve_wheel_backend(backend)
    log(f"[ptt/input] listing wheel backend={chosen}")
    if chosen == "off":
        log("[ptt/input] wheel polling off")
        return []
    if chosen == "winmm":
        result = _list_winmm()
    elif chosen == "rawhid":
        result = _list_rawhid()
    elif chosen == "dinput":
        result = _list_dinput()
    elif chosen == "pygame":
        result = _list_pygame()
    else:
        result = []
    if result:
        log(f"[ptt/input] {chosen}: {len(result)} device(s)")
        for j in result:
            extra = f", {j.get('vid_pid', '')}" if j.get("vid_pid") else ""
            log(f"[ptt/input]   [{j['index']}] {j['name']} "
                f"({j['buttons']} btn{extra})")
        return result
    log(f"[ptt/input] {chosen}: no devices")
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


def _list_pygame() -> list[dict]:
    """Read the pygame worker cache. Never calls pygame on this thread."""
    try:
        worker = get_pygame_worker()
        worker.ensure_started()
        return worker.cached_devices()
    except Exception as e:
        log(f"[ptt/input] pygame list failed: {e}")
        return []


def _list_rawhid() -> list[dict]:
    """Raw Input enumeration — no CreateFile, no FFB writes."""
    try:
        from .rawhid import list_devices_rawhid
        return list_devices_rawhid()
    except (ImportError, OSError) as e:
        log(f"[ptt/input] rawhid unavailable: {e}")
        return []


def run_diagnostics(active_backend: str | None = None) -> str:
    """Run backends and return a diagnostic report string.

    pygame is started only when it is the active wheel backend so a
    diagnostics click cannot open the wheel via HIDAPI and invert FFB.
    DirectInput enumeration does not Acquire / open devices.
    """
    active = resolve_wheel_backend(active_backend)
    lines = [
        "=== Joystick Diagnostics ===",
        "",
        f"Active wheel backend: {active} (default={DEFAULT_WHEEL_BACKEND})",
        "FFB policy: winmm is read-only (cannot touch FFB, 32 buttons).",
        "rawhid uses WM_INPUT + RIDI_PREPARSEDDATA (no CreateFile, 128 buttons).",
        "DirectInput uses DISCL_NONEXCLUSIVE|DISCL_BACKGROUND only;",
        "never exclusive, never effects / autocenter / DIPROP_FFGAIN.",
        "pygame inits joystick only (no haptic; HIDAPI=0, RAWINPUT=0)",
        "and is not started unless backend=pygame.",
        "",
    ]

    lines.append("--- DirectInput8 (enumerate only, no Acquire) ---")
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
    lines.append("--- winmm (joyGetDevCapsW, read-only) ---")
    joys = list_joysticks_winmm()
    if joys:
        for j in joys:
            note = ""
            if j.num_buttons >= 32:
                note = "  # likely the wheel (R16 maps here as 32 buttons)"
            lines.append(
                f"  [{j.index}] {j.name} ({j.num_buttons} buttons){note}")
    else:
        lines.append("  (no devices)")

    lines.append("")
    lines.append("--- rawhid (Raw Input, FFB-safe, 128 buttons) ---")
    try:
        from .rawhid import list_devices_rawhid
        raw_devs = list_devices_rawhid()
        if raw_devs:
            for d in raw_devs:
                extra = f", {d.get('vid_pid', '')}" if d.get("vid_pid") else ""
                lines.append(
                    f"  [{d['index']}] {d['name']} "
                    f"({d['buttons']} buttons{extra})")
        else:
            lines.append("  (no devices)")
    except (ImportError, OSError) as e:
        lines.append(f"  ERROR: {e}")

    lines.append("")
    lines.append("--- pygame (SDL2) ---")
    if active != "pygame":
        lines.append(
            "  (not started — FFB-safe; select Wheel polling backend "
            "= pygame to probe)")
    else:
        try:
            worker = get_pygame_worker()
            worker.ensure_started()
            devs = worker.cached_devices()
            if devs:
                for d in devs:
                    lines.append(
                        f"  [{d['index']}] {d['name']} ({d['buttons']} btn)")
            else:
                lines.append("  (worker running; no snapshot yet)")
        except (ImportError, Exception) as e:
            lines.append(f"  ERROR: {e}")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# InputListener — keyboard + joystick
# ---------------------------------------------------------------------------

ASSIGN_TIMEOUT_S = 20.0
_STOP_JOIN_S = 0.25


class InputListener:
    """Listens for keyboard and joystick input for PTT activation.

    Joystick backends run on a worker thread. The assign callback must
    be non-blocking (queued Qt signal). ``stop()`` never joins longer
    than ``_STOP_JOIN_S`` so the GUI thread cannot freeze.
    """

    def __init__(self, joy_backend=None, wheel_backend=None) -> None:
        self._binding: Optional[PTTBinding] = None
        self._on_press: Optional[Callable[[], None]] = None
        self._on_release: Optional[Callable[[], None]] = None
        self._assign_callback: Optional[Callable[[PTTBinding], None]] = None
        self._keyboard_listener = None
        self._joy_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._pressed = False
        self._joy_backend = joy_backend
        self._wheel_backend = resolve_wheel_backend(wheel_backend)
        self._assign_timer: Optional[threading.Timer] = None
        self._subscribed_pygame = False
        self._subscribed_rawhid = False

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
        self._cancel_assign_timer()
        self._unsubscribe_pygame()
        self._unsubscribe_rawhid()
        self._stop_keyboard_listener()
        t = self._joy_thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=_STOP_JOIN_S)
            self._joy_thread = None
        elif t is threading.current_thread():
            self._joy_thread = None

    def start_assign(self, callback: Callable[[PTTBinding], None],
                     timeout_s: float = ASSIGN_TIMEOUT_S) -> None:
        self._assign_callback = callback
        self._stop.clear()
        self._cancel_assign_timer()
        if timeout_s and timeout_s > 0:
            self._assign_timer = threading.Timer(
                timeout_s, self._on_assign_timeout)
            self._assign_timer.daemon = True
            self._assign_timer.start()
        self._start_keyboard_listener()
        self._start_joystick_listener()

    def stop_assign(self) -> None:
        self._assign_callback = None
        self.stop()

    def _cancel_assign_timer(self) -> None:
        t = self._assign_timer
        self._assign_timer = None
        if t is not None:
            t.cancel()

    def _on_assign_timeout(self) -> None:
        if self._assign_callback is None:
            return
        log(f"[ptt/input] assign timed out after {ASSIGN_TIMEOUT_S:.0f}s")
        self._assign_callback = None
        self._stop.set()

    def _deliver_assign(self, binding: PTTBinding) -> None:
        """Hand the binding to the caller without joining any thread."""
        cb = self._assign_callback
        self._assign_callback = None
        self._cancel_assign_timer()
        self._stop.set()
        if cb is not None:
            cb(binding)

    def _unsubscribe_pygame(self) -> None:
        if not self._subscribed_pygame:
            return
        self._subscribed_pygame = False
        backend = self._joy_backend or get_pygame_worker()
        try:
            backend.unsubscribe(self._on_joy_edge)
        except Exception:
            pass

    def _unsubscribe_rawhid(self) -> None:
        if not self._subscribed_rawhid:
            return
        self._subscribed_rawhid = False
        try:
            from .rawhid import get_rawhid_worker
            backend = self._joy_backend or get_rawhid_worker()
            backend.unsubscribe(self._on_joy_edge)
        except Exception:
            pass

    def _handle_joy_edge(self, joy_id: int, button_id: int, pressed: bool,
                         *, name: str = "", backend: str = "") -> None:
        """Press/release for one button. Logs matching PTT edges once."""
        tag = backend or "joy"
        if pressed and self._assign_callback is not None:
            extra = f" name={name!r}" if name else ""
            log(f"[ptt/input] {tag} assign joy={joy_id} btn={button_id}{extra}")
            self._deliver_assign(PTTBinding(
                type=BindingType.JOYSTICK,
                joy_id=joy_id, button_id=button_id))
            return
        if not (self._binding
                and self._binding.type == BindingType.JOYSTICK
                and self._binding.joy_id == joy_id
                and self._binding.button_id == button_id):
            return
        if pressed:
            if not self._pressed:
                self._pressed = True
                log(f"[ptt] down btn={button_id}")
                if self._on_press:
                    self._on_press()
        else:
            if self._pressed:
                self._pressed = False
                log("[ptt] up")
                if self._on_release:
                    self._on_release()

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
            self._deliver_assign(PTTBinding(
                type=BindingType.KEYBOARD, key_name=key_name))
            return

        if (self._binding and self._binding.type == BindingType.KEYBOARD
                and self._binding.key_name == key_name):
            if not self._pressed:
                self._pressed = True
                log(f"[ptt] down key={key_name}")
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
                log("[ptt] up")
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
        if self._joy_backend is not None:
            self._subscribe_poll_loop(self._joy_backend, "injected")
            return
        backend = self._wheel_backend
        log(f"[ptt/input] poll wheel backend={backend} "
            f"(background={'yes' if self._on_press else 'assign'})")
        if backend == "off":
            return
        if backend == "winmm":
            self._winmm_poll_loop()
            return
        if backend == "rawhid":
            self._rawhid_poll_loop()
            return
        if backend == "dinput":
            joys = list_joysticks(backend="dinput")
            dinput_devs = [j.get("_dinput_dev") for j in joys
                           if j.get("_dinput_dev") is not None]
            opened = False
            try:
                from .dinput import dinput_any_open
                opened = bool(dinput_devs) and dinput_any_open(dinput_devs)
            except (ImportError, OSError) as e:
                log(f"[ptt/input] DirectInput8 unavailable for poll: {e}")
            if opened:
                self._dinput_poll_loop(joys, dinput_devs)
                return
            log("[ptt/input] DirectInput8 not pollable — "
                "not falling back (FFB-safe)")
            return
        if backend == "pygame":
            self._pygame_poll_loop()
            return
        log(f"[ptt/input] unknown wheel backend {backend!r}")

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
                    if now != was:
                        self._handle_joy_edge(
                            idx, btn_id, now, backend="dinput")
                        if self._stop.is_set():
                            return
                prev[idx] = buttons
            self._stop.wait(0.01)

    def _winmm_poll_loop(self):
        prev_buttons: dict[int, int] = {}
        last_enum = 0.0
        winmm_joys: list[WinmmJoystick] = []
        last_sig: tuple | None = None
        while not self._stop.is_set():
            now = time.monotonic()
            if not winmm_joys or (now - last_enum) >= 3.0:
                winmm_joys = list_joysticks_winmm(verbose=False)
                last_enum = now
                sig = tuple((j.index, j.name, j.num_buttons) for j in winmm_joys)
                if sig != last_sig:
                    last_sig = sig
                    if winmm_joys:
                        log(f"[ptt/input] winmm poll: {len(winmm_joys)} "
                            f"device(s), up to "
                            f"{max(j.num_buttons for j in winmm_joys)} buttons "
                            f"(joyGetPosEx mask=32)")
                    else:
                        log("[ptt/input] winmm: no devices yet; retrying")
            for joy in winmm_joys:
                buttons = _read_buttons_winmm(joy.index)
                prev = prev_buttons.get(joy.index, 0)
                changed = buttons ^ prev
                if changed:
                    n_btn = min(joy.num_buttons, _MAX_JOYSTICK_BUTTONS)
                    for btn in range(n_btn):
                        mask = 1 << btn
                        if not (changed & mask):
                            continue
                        now_pressed = bool(buttons & mask)
                        self._handle_joy_edge(
                            joy.index, btn, now_pressed,
                            name=joy.name, backend="winmm")
                        if self._stop.is_set():
                            return
                prev_buttons[joy.index] = buttons
            self._stop.wait(0.01)

    def _rawhid_poll_loop(self):
        try:
            from .rawhid import get_rawhid_worker
            backend = self._joy_backend or get_rawhid_worker()
            self._subscribe_poll_loop(backend, "rawhid")
        except Exception as e:
            log(f"[ptt/input] rawhid poll init failed: {e}")

    def _pygame_poll_loop(self):
        backend = self._joy_backend or get_pygame_worker()
        self._subscribe_poll_loop(backend, "pygame")

    def _subscribe_poll_loop(self, backend, tag: str) -> None:
        try:
            backend.ensure_started()
            backend.subscribe(self._on_joy_edge)
            if tag == "rawhid":
                self._subscribed_rawhid = True
            else:
                self._subscribed_pygame = True
        except Exception as e:
            log(f"[ptt/input] {tag} poll init failed: {e}")
            return
        log(f"[ptt/input] {tag} poll: subscribed to dedicated worker")
        while not self._stop.is_set():
            self._stop.wait(0.05)
        if tag == "rawhid":
            self._unsubscribe_rawhid()
        else:
            self._unsubscribe_pygame()

    def _on_joy_edge(self, device: int, name: str, button: int,
                     pressed: bool) -> None:
        """Called on a worker thread. Must not block or join."""
        state = "pressed" if pressed else "released"
        log(f"[ptt/input] event device={device} "
            f"name={name!r} button={button} {state}")
        self._handle_joy_edge(
            device, button, pressed, name=name, backend="joy")
