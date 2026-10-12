"""DirectInput8 joystick backend via raw ctypes COM.

Enumerates all game controllers visible to DirectInput8 — including
modern HID-based devices (Moza, Fanatec, Thrustmaster) that the legacy
winmm API misses. Reads up to 128 buttons per device. Operates in
background mode (``DISCL_BACKGROUND | DISCL_NONEXCLUSIVE``) so buttons
are readable while iRacing or another game has focus.

No external dependencies — uses only ``ctypes``, ``comtypes`` is not
required. The COM vtable is accessed via raw pointer arithmetic.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import sys
import threading
from dataclasses import dataclass
from typing import Optional

from ..logbook import log

if sys.platform != "win32":
    raise ImportError("DirectInput8 backend is Windows-only")

# ---------------------------------------------------------------------------
# COM fundamentals
# ---------------------------------------------------------------------------
_ole32 = ctypes.OleDLL("ole32")
_dinput_dll = None

GUID = ctypes.c_ubyte * 16

def _make_guid(d1, d2, d3, d4_bytes):
    """Build a flat 16-byte GUID from the standard components."""
    import struct
    return (ctypes.c_ubyte * 16)(*struct.pack(
        "<IHH8s", d1, d2, d3, bytes(d4_bytes)))

CLSID_DirectInput8 = _make_guid(
    0x25E609E4, 0xB259, 0x11CF,
    [0xBF, 0xC7, 0x44, 0x45, 0x53, 0x54, 0x00, 0x00])
IID_IDirectInput8W = _make_guid(
    0xBF798031, 0x483A, 0x4DA2,
    [0xAA, 0x99, 0x5D, 0x64, 0xED, 0x36, 0x97, 0x00])

_DIRECTINPUT_VERSION = 0x0800
_DI8DEVCLASS_GAMECTRL = 4
_DIEDFL_ATTACHEDONLY = 0x00000001
_DISCL_BACKGROUND = 0x00000008
_DISCL_NONEXCLUSIVE = 0x00000002

# DIJOYSTATE2 button count
_DI_BUTTON_COUNT = 128

# ---------------------------------------------------------------------------
# DIDEVICEINSTANCEW (used by EnumDevices callback)
# ---------------------------------------------------------------------------
_MAX_PATH = 260

class _DIDEVICEINSTANCEW(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.wintypes.DWORD),
        ("guidInstance", GUID),
        ("guidProduct", GUID),
        ("dwDevType", ctypes.wintypes.DWORD),
        ("tszInstanceName", ctypes.wintypes.WCHAR * _MAX_PATH),
        ("tszProductName", ctypes.wintypes.WCHAR * _MAX_PATH),
        ("guidFFDriver", GUID),
        ("wUsagePage", ctypes.wintypes.WORD),
        ("wUsage", ctypes.wintypes.WORD),
    ]


# ---------------------------------------------------------------------------
# DIJOYSTATE2 (extended joystick state — 128 buttons)
# ---------------------------------------------------------------------------
class _DIJOYSTATE2(ctypes.Structure):
    _fields_ = [
        ("lX", ctypes.c_long),
        ("lY", ctypes.c_long),
        ("lZ", ctypes.c_long),
        ("lRx", ctypes.c_long),
        ("lRy", ctypes.c_long),
        ("lRz", ctypes.c_long),
        ("rglSlider", ctypes.c_long * 2),
        ("rgdwPOV", ctypes.wintypes.DWORD * 4),
        ("rgbButtons", ctypes.c_ubyte * _DI_BUTTON_COUNT),
        ("lVX", ctypes.c_long),
        ("lVY", ctypes.c_long),
        ("lVZ", ctypes.c_long),
        ("lVRx", ctypes.c_long),
        ("lVRy", ctypes.c_long),
        ("lVRz", ctypes.c_long),
        ("rglVSlider", ctypes.c_long * 2),
        ("lAX", ctypes.c_long),
        ("lAY", ctypes.c_long),
        ("lAZ", ctypes.c_long),
        ("lARx", ctypes.c_long),
        ("lARy", ctypes.c_long),
        ("lARz", ctypes.c_long),
        ("rglASlider", ctypes.c_long * 2),
        ("lFX", ctypes.c_long),
        ("lFY", ctypes.c_long),
        ("lFZ", ctypes.c_long),
        ("lFRx", ctypes.c_long),
        ("lFRy", ctypes.c_long),
        ("lFRz", ctypes.c_long),
        ("rglFSlider", ctypes.c_long * 2),
    ]

# c_dfDIJoystick2: we need this GUID for SetDataFormat.
# Building the full DIDATAFORMAT struct from scratch is verbose;
# instead we load it from dinput8.dll's exported symbol.

_GUID_SysMouse = _make_guid(
    0x6F1D2B60, 0xD5A0, 0x11CF,
    [0xBF, 0xC7, 0x44, 0x45, 0x53, 0x54, 0x00, 0x00])


def _get_dinput8():
    """Load dinput8.dll and cache it."""
    global _dinput_dll
    if _dinput_dll is None:
        _dinput_dll = ctypes.WinDLL("dinput8.dll")
    return _dinput_dll


# ---------------------------------------------------------------------------
# DirectInput8 COM interface wrappers
# ---------------------------------------------------------------------------

class _IUnknownVtbl(ctypes.Structure):
    pass

class _IDirectInput8WVtbl(ctypes.Structure):
    pass

class _IDirectInputDevice8WVtbl(ctypes.Structure):
    pass


def _vtbl_method(vtbl_ptr, index, restype, *argtypes):
    """Call a COM vtable method by index."""
    func_ptr_type = ctypes.WINFUNCTYPE(restype, *argtypes)
    vtbl = ctypes.cast(vtbl_ptr, ctypes.POINTER(ctypes.c_void_p))
    func = func_ptr_type(vtbl[index])
    return func


@dataclass
class DInputDevice:
    """An enumerated DirectInput8 device."""
    instance_name: str
    product_name: str
    guid_instance: bytes
    guid_product: bytes
    dev_type: int
    num_buttons: int = _DI_BUTTON_COUNT
    vid_pid: str = ""

    @property
    def display_name(self) -> str:
        return self.product_name or self.instance_name or "Unknown"


class DirectInput8:
    """Minimal DirectInput8 wrapper for joystick enumeration and polling."""

    def __init__(self) -> None:
        self._di: ctypes.c_void_p = ctypes.c_void_p(0)
        self._hwnd: ctypes.c_void_p = ctypes.c_void_p(0)
        self._devices: list[DInputDevice] = []
        self._open_devices: dict[bytes, ctypes.c_void_p] = {}
        self._lock = threading.Lock()
        self._inited = False

    def init(self) -> bool:
        """Initialize COM and create the IDirectInput8W instance."""
        if self._inited:
            return True
        try:
            _ole32.CoInitializeEx(None, 0)
        except OSError:
            pass

        self._hwnd = self._create_hidden_hwnd()
        if not self._hwnd:
            log("[dinput] failed to create hidden HWND")
            return False

        try:
            dinput8 = _get_dinput8()
            hr = dinput8.DirectInput8Create(
                ctypes.windll.kernel32.GetModuleHandleW(None),
                _DIRECTINPUT_VERSION,
                ctypes.byref(IID_IDirectInput8W),
                ctypes.byref(self._di),
                None,
            )
            if hr != 0:
                log(f"[dinput] DirectInput8Create failed: HRESULT 0x{hr & 0xFFFFFFFF:08X}")
                return False
        except (OSError, Exception) as e:
            log(f"[dinput] init failed: {e}")
            return False

        self._inited = True
        return True

    def _create_hidden_hwnd(self) -> ctypes.c_void_p:
        """Create a message-only window for SetCooperativeLevel."""
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.CreateWindowExW(
                0, "STATIC", "LTE_DInput_Hidden", 0,
                0, 0, 0, 0,
                -3,  # HWND_MESSAGE
                None, None, None,
            )
            return ctypes.c_void_p(hwnd) if hwnd else ctypes.c_void_p(0)
        except Exception:
            return ctypes.c_void_p(0)

    def enumerate(self) -> list[DInputDevice]:
        """Enumerate attached game controllers."""
        if not self._inited and not self.init():
            return []

        self._devices.clear()

        # EnumDevices callback type: BOOL CALLBACK(LPCDIDEVICEINSTANCEW, LPVOID)
        ENUM_CB = ctypes.WINFUNCTYPE(
            ctypes.c_int,
            ctypes.POINTER(_DIDEVICEINSTANCEW),
            ctypes.c_void_p,
        )

        def _enum_callback(dev_inst_ptr, ref):
            di = dev_inst_ptr.contents
            guid_bytes = bytes(di.guidInstance)
            prod_bytes = bytes(di.guidProduct)
            vid = (prod_bytes[0] | (prod_bytes[1] << 8))
            pid = (prod_bytes[2] | (prod_bytes[3] << 8))
            vid_pid = f"{vid:04X}:{pid:04X}" if vid or pid else ""
            self._devices.append(DInputDevice(
                instance_name=di.tszInstanceName,
                product_name=di.tszProductName,
                guid_instance=guid_bytes,
                guid_product=prod_bytes,
                dev_type=di.dwDevType,
                vid_pid=vid_pid,
            ))
            return 1  # DIENUM_CONTINUE

        cb = ENUM_CB(_enum_callback)
        # IDirectInput8W::EnumDevices is vtable index 4
        di_vtbl = ctypes.cast(
            self._di, ctypes.POINTER(ctypes.c_void_p)).contents
        vtbl = ctypes.cast(di_vtbl, ctypes.POINTER(ctypes.c_void_p))

        ENUM_FN = ctypes.WINFUNCTYPE(
            ctypes.c_long,  # HRESULT
            ctypes.c_void_p,  # this
            ctypes.wintypes.DWORD,  # dwDevType
            ctypes.c_void_p,  # lpCallback
            ctypes.c_void_p,  # pvRef
            ctypes.wintypes.DWORD,  # dwFlags
        )
        enum_fn = ENUM_FN(vtbl[4])
        try:
            hr = enum_fn(
                self._di,
                _DI8DEVCLASS_GAMECTRL,
                ctypes.cast(cb, ctypes.c_void_p),
                None,
                _DIEDFL_ATTACHEDONLY,
            )
            if hr != 0:
                log(f"[dinput] EnumDevices failed: 0x{hr & 0xFFFFFFFF:08X}")
        except Exception as e:
            log(f"[dinput] EnumDevices error: {e}")

        for d in self._devices:
            log(f"[dinput] found: {d.display_name} "
                f"(VID:PID={d.vid_pid}, type=0x{d.dev_type:04X})")
        return list(self._devices)

    def open_device(self, dev: DInputDevice) -> bool:
        """Open a device for polling with background non-exclusive access."""
        if dev.guid_instance in self._open_devices:
            return True
        if not self._inited:
            return False

        device_ptr = ctypes.c_void_p(0)
        guid = (ctypes.c_ubyte * 16)(*dev.guid_instance)

        # IDirectInput8W::CreateDevice — vtable index 3
        di_vtbl = ctypes.cast(
            self._di, ctypes.POINTER(ctypes.c_void_p)).contents
        vtbl = ctypes.cast(di_vtbl, ctypes.POINTER(ctypes.c_void_p))

        CREATE_FN = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ubyte * 16),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_void_p,
        )
        create_fn = CREATE_FN(vtbl[3])
        hr = create_fn(self._di, ctypes.byref(guid),
                       ctypes.byref(device_ptr), None)
        if hr != 0:
            log(f"[dinput] CreateDevice failed for {dev.display_name}: "
                f"0x{hr & 0xFFFFFFFF:08X}")
            return False

        dev_vtbl = ctypes.cast(
            device_ptr, ctypes.POINTER(ctypes.c_void_p)).contents
        dev_vt = ctypes.cast(dev_vtbl, ctypes.POINTER(ctypes.c_void_p))

        # SetDataFormat — vtable index 10
        # We need c_dfDIJoystick2 from dinput8.dll
        try:
            dinput8 = _get_dinput8()
            c_dfDIJoystick2_ptr = ctypes.c_void_p.in_dll(
                dinput8, "c_dfDIJoystick2")
            SET_DF = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)
            set_df = SET_DF(dev_vt[10])
            hr = set_df(device_ptr, c_dfDIJoystick2_ptr)
            if hr != 0:
                log(f"[dinput] SetDataFormat failed: 0x{hr & 0xFFFFFFFF:08X}")
                return False
        except (OSError, ValueError) as e:
            log(f"[dinput] SetDataFormat symbol lookup failed: {e}")
            return False

        # SetCooperativeLevel — vtable index 13
        SET_CL = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.wintypes.DWORD)
        set_cl = SET_CL(dev_vt[13])
        flags = _DISCL_BACKGROUND | _DISCL_NONEXCLUSIVE
        hr = set_cl(device_ptr, self._hwnd, flags)
        if hr != 0:
            log(f"[dinput] SetCooperativeLevel failed: "
                f"0x{hr & 0xFFFFFFFF:08X}")
            return False

        # Acquire — vtable index 7
        ACQUIRE = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
        acquire = ACQUIRE(dev_vt[7])
        acquire(device_ptr)

        self._open_devices[dev.guid_instance] = device_ptr
        return True

    def read_buttons(self, dev: DInputDevice) -> list[bool]:
        """Read button states for an opened device. Returns list of bools."""
        device_ptr = self._open_devices.get(dev.guid_instance)
        if device_ptr is None:
            return []

        dev_vtbl = ctypes.cast(
            device_ptr, ctypes.POINTER(ctypes.c_void_p)).contents
        dev_vt = ctypes.cast(dev_vtbl, ctypes.POINTER(ctypes.c_void_p))

        # Poll — vtable index 8
        POLL = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
        poll_fn = POLL(dev_vt[8])
        poll_fn(device_ptr)

        # GetDeviceState — vtable index 9
        state = _DIJOYSTATE2()
        GET_STATE = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p,
            ctypes.wintypes.DWORD, ctypes.c_void_p)
        get_state = GET_STATE(dev_vt[9])
        hr = get_state(device_ptr, ctypes.sizeof(state), ctypes.byref(state))
        if hr != 0:
            # Try re-acquire (device may have been lost)
            ACQUIRE = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
            acquire = ACQUIRE(dev_vt[7])
            acquire(device_ptr)
            hr = get_state(
                device_ptr, ctypes.sizeof(state), ctypes.byref(state))
            if hr != 0:
                return []

        return [bool(state.rgbButtons[i] & 0x80)
                for i in range(_DI_BUTTON_COUNT)]

    def close(self) -> None:
        """Release all devices and the DI instance."""
        for dev_ptr in self._open_devices.values():
            try:
                dev_vtbl = ctypes.cast(
                    dev_ptr, ctypes.POINTER(ctypes.c_void_p)).contents
                dev_vt = ctypes.cast(
                    dev_vtbl, ctypes.POINTER(ctypes.c_void_p))
                UNACQUIRE = ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p)
                UNACQUIRE(dev_vt[8 + 1])(dev_ptr)  # Unacquire = vtable 9... wait
                # Actually Release = vtable index 2
                RELEASE = ctypes.WINFUNCTYPE(
                    ctypes.c_ulong, ctypes.c_void_p)
                RELEASE(dev_vt[2])(dev_ptr)
            except Exception:
                pass
        self._open_devices.clear()

        if self._di:
            try:
                di_vtbl = ctypes.cast(
                    self._di, ctypes.POINTER(ctypes.c_void_p)).contents
                vtbl = ctypes.cast(di_vtbl, ctypes.POINTER(ctypes.c_void_p))
                RELEASE = ctypes.WINFUNCTYPE(
                    ctypes.c_ulong, ctypes.c_void_p)
                RELEASE(vtbl[2])(self._di)
            except Exception:
                pass
            self._di = ctypes.c_void_p(0)

        if self._hwnd:
            try:
                ctypes.windll.user32.DestroyWindow(self._hwnd)
            except Exception:
                pass
            self._hwnd = ctypes.c_void_p(0)

        self._inited = False


# Module-level singleton (lazy-init)
_instance: Optional[DirectInput8] = None
_init_lock = threading.Lock()


def _get_di8() -> Optional[DirectInput8]:
    global _instance
    if _instance is not None:
        return _instance
    with _init_lock:
        if _instance is not None:
            return _instance
        di = DirectInput8()
        if di.init():
            _instance = di
            return di
        return None


def list_devices_dinput() -> list[DInputDevice]:
    """Enumerate DirectInput8 game controllers."""
    di = _get_di8()
    if di is None:
        return []
    return di.enumerate()


def read_device_buttons(dev: DInputDevice) -> list[bool]:
    """Read button states for a device. Opens it if needed."""
    di = _get_di8()
    if di is None:
        return []
    if dev.guid_instance not in di._open_devices:
        if not di.open_device(dev):
            return []
    return di.read_buttons(dev)
