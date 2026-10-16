"""FFB-safe Raw Input joystick backend (Windows).

Reads HID button reports via ``RegisterRawInputDevices`` + ``WM_INPUT``.
Enumeration and report parsing use ``GetRawInputDeviceInfo`` with
``RIDI_PREPARSEDDATA`` / ``RIDI_DEVICENAME`` — the HID device is never
opened with ``CreateFile``, never written to, and never acquired through
DirectInput or XInput. That is the FFB-safe path for >32 buttons
(winmm ``joyGetPosEx`` only returns a 32-bit mask).

SDL HIDAPI / SDL raw-input are *not* used here; those open the wheel
and inverted Moza R16 FFB under test.12.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import sys
import threading
from typing import Callable, Optional

from ..logbook import log

JoyEdgeCB = Callable[[int, str, int, bool], None]

# winuser.h
_RIDEV_INPUTSINK = 0x00000100
_RID_INPUT = 0x10000003
_RIDI_PREPARSEDDATA = 0x20000005
_RIDI_DEVICENAME = 0x20000007
_RIDI_DEVICEINFO = 0x2000000B
_RIM_TYPEHID = 2
_WM_INPUT = 0x00FF
_PM_REMOVE = 0x0001
_HWND_MESSAGE = ctypes.wintypes.HWND(-3)

# hidusage.h
_HID_USAGE_PAGE_GENERIC = 0x01
_HID_USAGE_GENERIC_JOYSTICK = 0x04
_HID_USAGE_GENERIC_GAMEPAD = 0x05
_HID_USAGE_GENERIC_MULTI_AXIS = 0x08

_HIDP_STATUS_SUCCESS = 0x00110000
_HidP_Input = 0

_MAX_BUTTONS = 128
_CLASS_NAME = "LTE_RawHidMsg"


class _RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", ctypes.wintypes.USHORT),
        ("usUsage", ctypes.wintypes.USHORT),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("hwndTarget", ctypes.wintypes.HWND),
    ]


class _RAWINPUTDEVICELIST(ctypes.Structure):
    _fields_ = [
        ("hDevice", ctypes.wintypes.HANDLE),
        ("dwType", ctypes.wintypes.DWORD),
    ]


class _RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", ctypes.wintypes.DWORD),
        ("dwSize", ctypes.wintypes.DWORD),
        ("hDevice", ctypes.wintypes.HANDLE),
        ("wParam", ctypes.wintypes.WPARAM),
    ]


class _RAWHID(ctypes.Structure):
    _fields_ = [
        ("dwSizeHid", ctypes.wintypes.DWORD),
        ("dwCount", ctypes.wintypes.DWORD),
        ("bRawData", ctypes.c_ubyte * 1),
    ]


class _RID_DEVICE_INFO_HID(ctypes.Structure):
    _fields_ = [
        ("dwVendorId", ctypes.wintypes.DWORD),
        ("dwProductId", ctypes.wintypes.DWORD),
        ("dwVersionNumber", ctypes.wintypes.DWORD),
        ("usUsagePage", ctypes.wintypes.USHORT),
        ("usUsage", ctypes.wintypes.USHORT),
    ]


class _RID_DEVICE_INFO(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [
            ("hid", _RID_DEVICE_INFO_HID),
            ("_pad", ctypes.c_ubyte * 32),
        ]
    _anonymous_ = ("u",)
    _fields_ = [
        ("cbSize", ctypes.wintypes.DWORD),
        ("dwType", ctypes.wintypes.DWORD),
        ("u", _U),
    ]


class _HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", ctypes.wintypes.USHORT),
        ("UsagePage", ctypes.wintypes.USHORT),
        ("InputReportByteLength", ctypes.wintypes.USHORT),
        ("OutputReportByteLength", ctypes.wintypes.USHORT),
        ("FeatureReportByteLength", ctypes.wintypes.USHORT),
        ("Reserved", ctypes.wintypes.USHORT * 17),
        ("NumberLinkCollectionNodes", ctypes.wintypes.USHORT),
        ("NumberInputButtonCaps", ctypes.wintypes.USHORT),
        ("NumberInputValueCaps", ctypes.wintypes.USHORT),
        ("NumberInputDataIndices", ctypes.wintypes.USHORT),
        ("NumberOutputButtonCaps", ctypes.wintypes.USHORT),
        ("NumberOutputValueCaps", ctypes.wintypes.USHORT),
        ("NumberOutputDataIndices", ctypes.wintypes.USHORT),
        ("NumberFeatureButtonCaps", ctypes.wintypes.USHORT),
        ("NumberFeatureValueCaps", ctypes.wintypes.USHORT),
        ("NumberFeatureDataIndices", ctypes.wintypes.USHORT),
    ]


class _HIDP_BUTTON_RANGE(ctypes.Structure):
    _fields_ = [
        ("UsageMin", ctypes.wintypes.USHORT),
        ("UsageMax", ctypes.wintypes.USHORT),
        ("StringMin", ctypes.wintypes.USHORT),
        ("StringMax", ctypes.wintypes.USHORT),
        ("DesignatorMin", ctypes.wintypes.USHORT),
        ("DesignatorMax", ctypes.wintypes.USHORT),
        ("DataIndexMin", ctypes.wintypes.USHORT),
        ("DataIndexMax", ctypes.wintypes.USHORT),
    ]


class _HIDP_BUTTON_CAPS(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("Range", _HIDP_BUTTON_RANGE),
                    ("NotRange", _HIDP_BUTTON_RANGE)]

    _anonymous_ = ("u",)
    _fields_ = [
        ("UsagePage", ctypes.wintypes.USHORT),
        ("ReportID", ctypes.c_ubyte),
        ("IsAlias", ctypes.c_ubyte),
        ("BitField", ctypes.wintypes.USHORT),
        ("LinkCollection", ctypes.wintypes.USHORT),
        ("LinkUsage", ctypes.wintypes.USHORT),
        ("LinkUsagePage", ctypes.wintypes.USHORT),
        ("IsRange", ctypes.c_ubyte),
        ("IsStringRange", ctypes.c_ubyte),
        ("IsDesignatorRange", ctypes.c_ubyte),
        ("IsAbsolute", ctypes.c_ubyte),
        ("Reserved", ctypes.wintypes.DWORD * 10),
        ("u", _U),
    ]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.wintypes.HWND),
        ("message", ctypes.wintypes.UINT),
        ("wParam", ctypes.wintypes.WPARAM),
        ("lParam", ctypes.wintypes.LPARAM),
        ("time", ctypes.wintypes.DWORD),
        ("pt_x", ctypes.c_long),
        ("pt_y", ctypes.c_long),
    ]


_HANDLE = ctypes.wintypes.HANDLE


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.UINT),
        ("style", ctypes.wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.wintypes.HINSTANCE),
        ("hIcon", _HANDLE),
        ("hCursor", _HANDLE),
        ("hbrBackground", _HANDLE),
        ("lpszMenuName", ctypes.wintypes.LPCWSTR),
        ("lpszClassName", ctypes.wintypes.LPCWSTR),
        ("hIconSm", _HANDLE),
    ]


def _win_apis():
    """Load user32/hid/kernel32. Returns None off Windows or on failure."""
    if sys.platform != "win32":
        return None
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        hid = ctypes.WinDLL("hid")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        return user32, hid, kernel32
    except OSError as e:
        log(f"[ptt/rawhid] dll load failed: {e}")
        return None


def rawhid_is_ffb_safe_source(text: str) -> bool:
    """True when *text* (module source) never opens/writes the HID device."""
    # Split so this helper's own source does not contain the call forms.
    needles = (
        ("CreateFile", "W("),
        ("CreateFile", "A("),
        ("WriteFile", "("),
        ("HidD_SetOutputReport", "("),
        ("HidD_SetFeature", "("),
        ("SendOutputReport", "("),
        ("XInputGetState", "("),
    )
    return not any("".join(parts) in text for parts in needles)


def _button_count_from_caps(caps_list) -> int:
    total = 0
    for cap in caps_list:
        if cap.IsRange:
            lo = int(cap.Range.UsageMin)
            hi = int(cap.Range.UsageMax)
            if hi >= lo:
                total += hi - lo + 1
        else:
            total += 1
    return min(_MAX_BUTTONS, total) if total else _MAX_BUTTONS


def _usage_to_button(usage: int) -> int:
    """HID button usages are 1-based; PTT bindings are 0-based."""
    if usage <= 0:
        return 0
    return min(_MAX_BUTTONS - 1, usage - 1)


def list_devices_rawhid() -> list[dict]:
    """Enumerate HID joysticks/gamepads via Raw Input (no CreateFile)."""
    apis = _win_apis()
    if apis is None:
        return []
    user32, hid, _kernel32 = apis
    n = ctypes.wintypes.UINT(0)
    user32.GetRawInputDeviceList.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.wintypes.UINT),
        ctypes.wintypes.UINT]
    user32.GetRawInputDeviceList.restype = ctypes.wintypes.UINT
    cb = ctypes.sizeof(_RAWINPUTDEVICELIST)
    rc = user32.GetRawInputDeviceList(None, ctypes.byref(n), cb)
    if rc == 0xFFFFFFFF or n.value == 0:
        return []
    arr = (_RAWINPUTDEVICELIST * n.value)()
    rc = user32.GetRawInputDeviceList(arr, ctypes.byref(n), cb)
    if rc == 0xFFFFFFFF:
        return []

    user32.GetRawInputDeviceInfoW.restype = ctypes.wintypes.UINT
    hid.HidP_GetCaps.restype = ctypes.c_ulong
    hid.HidP_GetButtonCaps.restype = ctypes.c_ulong

    result: list[dict] = []
    index = 0
    for i in range(rc):
        entry = arr[i]
        if entry.dwType != _RIM_TYPEHID:
            continue
        info = _RID_DEVICE_INFO()
        info.cbSize = ctypes.sizeof(_RID_DEVICE_INFO)
        sz = ctypes.wintypes.UINT(info.cbSize)
        user32.GetRawInputDeviceInfoW.argtypes = [
            ctypes.wintypes.HANDLE, ctypes.wintypes.UINT,
            ctypes.c_void_p, ctypes.POINTER(ctypes.wintypes.UINT)]
        if user32.GetRawInputDeviceInfoW(
                entry.hDevice, _RIDI_DEVICEINFO,
                ctypes.byref(info), ctypes.byref(sz)) == 0xFFFFFFFF:
            continue
        if info.dwType != _RIM_TYPEHID:
            continue
        page = int(info.hid.usUsagePage)
        usage = int(info.hid.usUsage)
        if page != _HID_USAGE_PAGE_GENERIC or usage not in (
                _HID_USAGE_GENERIC_JOYSTICK,
                _HID_USAGE_GENERIC_GAMEPAD,
                _HID_USAGE_GENERIC_MULTI_AXIS):
            continue

        name_sz = ctypes.wintypes.UINT(0)
        user32.GetRawInputDeviceInfoW(
            entry.hDevice, _RIDI_DEVICENAME, None, ctypes.byref(name_sz))
        name = f"HID {info.hid.dwVendorId:04X}:{info.hid.dwProductId:04X}"
        if name_sz.value:
            buf = ctypes.create_unicode_buffer(name_sz.value)
            if user32.GetRawInputDeviceInfoW(
                    entry.hDevice, _RIDI_DEVICENAME,
                    buf, ctypes.byref(name_sz)) != 0xFFFFFFFF:
                raw_name = buf.value.strip()
                if raw_name:
                    name = raw_name.split("\\")[-1] or raw_name

        n_buttons = _MAX_BUTTONS
        prep_sz = ctypes.wintypes.UINT(0)
        user32.GetRawInputDeviceInfoW(
            entry.hDevice, _RIDI_PREPARSEDDATA, None, ctypes.byref(prep_sz))
        if prep_sz.value:
            prep = (ctypes.c_ubyte * prep_sz.value)()
            if user32.GetRawInputDeviceInfoW(
                    entry.hDevice, _RIDI_PREPARSEDDATA,
                    prep, ctypes.byref(prep_sz)) != 0xFFFFFFFF:
                caps = _HIDP_CAPS()
                if hid.HidP_GetCaps(prep, ctypes.byref(caps)) == _HIDP_STATUS_SUCCESS:
                    n_caps = ctypes.wintypes.USHORT(caps.NumberInputButtonCaps)
                    if n_caps.value:
                        btn_caps = (_HIDP_BUTTON_CAPS * n_caps.value)()
                        if hid.HidP_GetButtonCaps(
                                _HidP_Input, btn_caps, ctypes.byref(n_caps),
                                prep) == _HIDP_STATUS_SUCCESS:
                            n_buttons = _button_count_from_caps(
                                list(btn_caps)[:n_caps.value]) or n_buttons

        vid_pid = f"{info.hid.dwVendorId:04X}:{info.hid.dwProductId:04X}"
        result.append({
            "index": index,
            "name": name,
            "buttons": n_buttons,
            "backend": "rawhid",
            "vid_pid": vid_pid,
            "_hdevice": int(entry.hDevice) if entry.hDevice else 0,
        })
        index += 1
    return result


class RawHidWorker:
    """Owns a message-only HWND and pumps WM_INPUT on one daemon thread.

    Never CreateFile / WriteFile / DirectInput Acquire / XInput.
    """

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._subs: list[JoyEdgeCB] = []
        self._devices: list[dict] = []
        self._prev: dict[int, set[int]] = {}
        self._hdev_to_idx: dict[int, int] = {}

    def ensure_started(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="ptt-rawhid", daemon=True)
            self._thread.start()

    def cached_devices(self) -> list[dict]:
        with self._lock:
            return [dict(d) for d in self._devices]

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

    def _emit(self, device: int, name: str, button: int, pressed: bool) -> None:
        with self._lock:
            subs = list(self._subs)
        for cb in subs:
            try:
                cb(device, name, button, pressed)
            except Exception as e:
                log(f"[ptt/rawhid] subscriber error: {e}")

    def _refresh_devices(self) -> None:
        devs = list_devices_rawhid()
        mapping: dict[int, int] = {}
        for d in devs:
            h = int(d.get("_hdevice") or 0)
            if h:
                mapping[h] = d["index"]
        with self._lock:
            self._devices = [{k: v for k, v in d.items() if k != "_hdevice"}
                             for d in devs]
            self._hdev_to_idx = mapping
        if devs:
            log(f"[ptt/rawhid] {len(devs)} device(s)")
            for d in devs:
                log(f"[ptt/rawhid]   [{d['index']}] {d['name']} "
                    f"({d['buttons']} btn, {d.get('vid_pid', '')})")
        else:
            log("[ptt/rawhid] no HID joystick/gamepad devices")

    def _run(self) -> None:
        apis = _win_apis()
        if apis is None:
            return
        user32, hid, kernel32 = apis
        try:
            hwnd = _create_message_hwnd(user32, kernel32)
            if not hwnd:
                log("[ptt/rawhid] no HWND; cannot register Raw Input")
                return
            if not _register_raw_input(user32, hwnd):
                log("[ptt/rawhid] RegisterRawInputDevices failed")
                return
            log("[ptt/rawhid] worker started (Raw Input, no CreateFile, "
                "no FFB writes)")
            self._refresh_devices()
            self._pump(user32, hid, hwnd)
        except Exception as e:
            log(f"[ptt/rawhid] worker failed: {e}")
        finally:
            log("[ptt/rawhid] worker stopped")

    def _pump(self, user32, hid, hwnd: int) -> None:
        user32.PeekMessageW.argtypes = [
            ctypes.POINTER(_MSG), ctypes.wintypes.HWND,
            ctypes.wintypes.UINT, ctypes.wintypes.UINT,
            ctypes.wintypes.UINT]
        user32.PeekMessageW.restype = ctypes.wintypes.BOOL
        user32.TranslateMessage.argtypes = [ctypes.POINTER(_MSG)]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(_MSG)]
        msg = _MSG()
        last_enum = 0.0
        import time
        while not self._stop.is_set():
            while user32.PeekMessageW(
                    ctypes.byref(msg), ctypes.wintypes.HWND(hwnd),
                    0, 0, _PM_REMOVE):
                if msg.message == _WM_INPUT:
                    self._on_wm_input(user32, hid, int(msg.lParam))
                else:
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
            now = time.monotonic()
            if now - last_enum > 5.0:
                self._refresh_devices()
                last_enum = now
            self._stop.wait(0.005)

    def _on_wm_input(self, user32, hid, lparam: int) -> None:
        size = ctypes.wintypes.UINT(0)
        user32.GetRawInputData.argtypes = [
            ctypes.wintypes.HANDLE, ctypes.wintypes.UINT,
            ctypes.c_void_p, ctypes.POINTER(ctypes.wintypes.UINT),
            ctypes.wintypes.UINT]
        user32.GetRawInputData.restype = ctypes.wintypes.UINT
        hdr_sz = ctypes.sizeof(_RAWINPUTHEADER)
        if user32.GetRawInputData(
                ctypes.wintypes.HANDLE(lparam), _RID_INPUT,
                None, ctypes.byref(size), hdr_sz) == 0xFFFFFFFF:
            return
        if size.value == 0:
            return
        buf = (ctypes.c_ubyte * size.value)()
        if user32.GetRawInputData(
                ctypes.wintypes.HANDLE(lparam), _RID_INPUT,
                buf, ctypes.byref(size), hdr_sz) == 0xFFFFFFFF:
            return
        header = _RAWINPUTHEADER.from_buffer(buf)
        if header.dwType != _RIM_TYPEHID:
            return
        hdev = int(header.hDevice) if header.hDevice else 0
        with self._lock:
            idx = self._hdev_to_idx.get(hdev)
            devices = list(self._devices)
        if idx is None or idx >= len(devices):
            return
        name = devices[idx].get("name", "")
        # RAWHID follows the header; bRawData is the HID report.
        hid_off = ctypes.sizeof(_RAWINPUTHEADER)
        if hid_off + 8 > size.value:
            return
        dw_size_hid = int.from_bytes(bytes(buf[hid_off:hid_off + 4]), "little")
        dw_count = int.from_bytes(bytes(buf[hid_off + 4:hid_off + 8]), "little")
        report = bytes(buf[hid_off + 8:hid_off + 8 + dw_size_hid * max(dw_count, 1)])
        if not report:
            return
        pressed = _buttons_from_report(user32, hid, hdev, report)
        prev = self._prev.get(idx, set())
        if pressed == prev:
            return
        added = pressed - prev
        removed = prev - pressed
        self._prev[idx] = pressed
        for btn in sorted(added):
            self._emit(idx, name, btn, True)
        for btn in sorted(removed):
            self._emit(idx, name, btn, False)


def _buttons_from_report(user32, hid, hdev: int, report: bytes) -> set[int]:
    """Parse pressed button usages via HidP_GetUsages (preparsed data)."""
    prep_sz = ctypes.wintypes.UINT(0)
    user32.GetRawInputDeviceInfoW(
        ctypes.wintypes.HANDLE(hdev), _RIDI_PREPARSEDDATA,
        None, ctypes.byref(prep_sz))
    if not prep_sz.value:
        return set()
    prep = (ctypes.c_ubyte * prep_sz.value)()
    if user32.GetRawInputDeviceInfoW(
            ctypes.wintypes.HANDLE(hdev), _RIDI_PREPARSEDDATA,
            prep, ctypes.byref(prep_sz)) == 0xFFFFFFFF:
        return set()
    hid.HidP_MaxUsageListLength.restype = ctypes.c_ulong
    hid.HidP_GetUsages.restype = ctypes.c_ulong
    max_len = hid.HidP_MaxUsageListLength(_HidP_Input, 0, prep)
    if not max_len:
        return set()
    usages = (ctypes.wintypes.USHORT * max_len)()
    usage_len = ctypes.c_ulong(max_len)
    report_buf = (ctypes.c_ubyte * len(report)).from_buffer_copy(report)
    rc = hid.HidP_GetUsages(
        _HidP_Input, 0, 0, usages, ctypes.byref(usage_len),
        prep, report_buf, len(report))
    if rc != _HIDP_STATUS_SUCCESS:
        return set()
    out: set[int] = set()
    for i in range(int(usage_len.value)):
        out.add(_usage_to_button(int(usages[i])))
    return out


_wndproc_ref = None
_hwnd_class_atom = 0


def _create_message_hwnd(user32, kernel32) -> int:
    global _wndproc_ref, _hwnd_class_atom
    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_longlong, ctypes.wintypes.HWND, ctypes.wintypes.UINT,
        ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM)
    user32.DefWindowProcW.argtypes = [
        ctypes.wintypes.HWND, ctypes.wintypes.UINT,
        ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
    user32.DefWindowProcW.restype = ctypes.c_longlong
    if _wndproc_ref is None:
        _wndproc_ref = WNDPROC(user32.DefWindowProcW)

    kernel32.GetModuleHandleW.argtypes = [ctypes.wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = ctypes.wintypes.HMODULE
    hinstance = kernel32.GetModuleHandleW(None)

    if not _hwnd_class_atom:
        user32.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
        user32.RegisterClassExW.restype = ctypes.wintypes.ATOM
        wc = _WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
        wc.lpfnWndProc = ctypes.cast(_wndproc_ref, ctypes.c_void_p)
        wc.hInstance = hinstance
        wc.lpszClassName = _CLASS_NAME
        atom = user32.RegisterClassExW(ctypes.byref(wc))
        err = ctypes.get_last_error()
        if atom:
            _hwnd_class_atom = atom
        elif err != 1410:
            log(f"[ptt/rawhid] RegisterClassExW failed: {err}")

    user32.CreateWindowExW.argtypes = [
        ctypes.wintypes.DWORD, ctypes.wintypes.LPCWSTR, ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.wintypes.HWND, ctypes.wintypes.HMENU,
        ctypes.wintypes.HINSTANCE, ctypes.wintypes.LPVOID]
    user32.CreateWindowExW.restype = ctypes.wintypes.HWND
    hwnd = user32.CreateWindowExW(
        0, _CLASS_NAME, "LTE RawHid",
        0, 0, 0, 0, 0,
        _HWND_MESSAGE, None, hinstance, None)
    if hwnd:
        return int(hwnd)
    log(f"[ptt/rawhid] CreateWindowExW failed: {ctypes.get_last_error()}")
    return 0


def _register_raw_input(user32, hwnd: int) -> bool:
    user32.RegisterRawInputDevices.argtypes = [
        ctypes.POINTER(_RAWINPUTDEVICE), ctypes.wintypes.UINT,
        ctypes.wintypes.UINT]
    user32.RegisterRawInputDevices.restype = ctypes.wintypes.BOOL
    devices = (_RAWINPUTDEVICE * 3)()
    for i, usage in enumerate((
            _HID_USAGE_GENERIC_JOYSTICK,
            _HID_USAGE_GENERIC_GAMEPAD,
            _HID_USAGE_GENERIC_MULTI_AXIS)):
        devices[i].usUsagePage = _HID_USAGE_PAGE_GENERIC
        devices[i].usUsage = usage
        devices[i].dwFlags = _RIDEV_INPUTSINK
        devices[i].hwndTarget = ctypes.wintypes.HWND(hwnd)
    ok = bool(user32.RegisterRawInputDevices(
        devices, 3, ctypes.sizeof(_RAWINPUTDEVICE)))
    if not ok:
        log(f"[ptt/rawhid] RegisterRawInputDevices GetLastError="
            f"{ctypes.get_last_error()}")
    return ok


class FakeRawHidBackend:
    """Test double: emit edges without Win32 Raw Input."""

    def __init__(self, devices: list[dict] | None = None) -> None:
        self._subs: list[JoyEdgeCB] = []
        self._devices = devices or [{
            "index": 0, "name": "MOZA R16 Ultra Base",
            "buttons": 128, "backend": "rawhid", "vid_pid": "346E:0000",
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


_WORKER: Optional[RawHidWorker] = None
_WORKER_LOCK = threading.Lock()


def get_rawhid_worker() -> RawHidWorker:
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is None:
            _WORKER = RawHidWorker()
        return _WORKER
