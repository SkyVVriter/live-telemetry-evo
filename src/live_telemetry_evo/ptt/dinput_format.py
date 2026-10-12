"""ctypes DIDATAFORMAT for DIJOYSTATE2 (128 buttons).

``c_dfDIJoystick2`` lives in ``dinput8.lib``, not ``dinput8.dll``, so it
cannot be looked up with ``in_dll``. This module builds the same layout
the Microsoft/Wine headers use, using fixed-width integers so the sizes
are identical on Windows and Linux.
"""
from __future__ import annotations

import ctypes
from typing import NamedTuple

# Windows LONG/DWORD are always 32-bit; do not use ctypes.c_long / c_ulong
# (those are 8 bytes on Linux x64 and would break size checks).
_LONG = ctypes.c_int32
_DWORD = ctypes.c_uint32
_BYTE = ctypes.c_ubyte

_DI_BUTTON_COUNT = 128
_DI_AXIS_COUNT = 8          # X Y Z Rx Ry Rz Slider0 Slider1
_DI_POV_COUNT = 4
_DI_EXTRA_AXIS_GROUPS = 3   # velocity, accel, force
_DF_JOYSTICK2_NUM_OBJS = (
    _DI_AXIS_COUNT + _DI_POV_COUNT + _DI_BUTTON_COUNT
    + _DI_AXIS_COUNT * _DI_EXTRA_AXIS_GROUPS
)  # 164

DIDFT_AXIS = 0x00000003
DIDFT_BUTTON = 0x0000000C
DIDFT_POV = 0x00000010
DIDFT_ANYINSTANCE = 0x00FFFF00
DIDFT_OPTIONAL = 0x80000000

DIDF_ABSAXIS = 0x00000001

DIDOI_ASPECTPOSITION = 0x00000100
DIDOI_ASPECTVELOCITY = 0x00000200
DIDOI_ASPECTACCEL = 0x00000300
DIDOI_ASPECTFORCE = 0x00000400

_TYPE_AXIS = DIDFT_OPTIONAL | DIDFT_AXIS | DIDFT_ANYINSTANCE
_TYPE_POV = DIDFT_OPTIONAL | DIDFT_POV | DIDFT_ANYINSTANCE
_TYPE_BUTTON = DIDFT_OPTIONAL | DIDFT_BUTTON | DIDFT_ANYINSTANCE

DIJOYSTATE2_SIZE = 272  # official sizeof(DIJOYSTATE2)


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(d1: int, d2: int, d3: int, d4: list[int]) -> _GUID:
    g = _GUID()
    g.Data1 = d1
    g.Data2 = d2
    g.Data3 = d3
    g.Data4 = (ctypes.c_ubyte * 8)(*d4)
    return g


# dinput.h DEFINE_GUID values — keep alive for pguid pointers.
GUID_XAxis = _guid(0xA36D02E0, 0xC9F3, 0x11CF, [0xBF, 0xC7, 0x44, 0x45, 0x53, 0x54, 0x00, 0x00])
GUID_YAxis = _guid(0xA36D02E1, 0xC9F3, 0x11CF, [0xBF, 0xC7, 0x44, 0x45, 0x53, 0x54, 0x00, 0x00])
GUID_ZAxis = _guid(0xA36D02E2, 0xC9F3, 0x11CF, [0xBF, 0xC7, 0x44, 0x45, 0x53, 0x54, 0x00, 0x00])
GUID_RxAxis = _guid(0xA36D02F4, 0xC9F3, 0x11CF, [0xBF, 0xC7, 0x44, 0x45, 0x53, 0x54, 0x00, 0x00])
GUID_RyAxis = _guid(0xA36D02F5, 0xC9F3, 0x11CF, [0xBF, 0xC7, 0x44, 0x45, 0x53, 0x54, 0x00, 0x00])
GUID_RzAxis = _guid(0xA36D02E3, 0xC9F3, 0x11CF, [0xBF, 0xC7, 0x44, 0x45, 0x53, 0x54, 0x00, 0x00])
GUID_Slider = _guid(0xA36D02E4, 0xC9F3, 0x11CF, [0xBF, 0xC7, 0x44, 0x45, 0x53, 0x54, 0x00, 0x00])
GUID_POV = _guid(0xA36D02F2, 0xC9F3, 0x11CF, [0xBF, 0xC7, 0x44, 0x45, 0x53, 0x54, 0x00, 0x00])


class _DIJOYSTATE2(ctypes.Structure):
    """Official DIJOYSTATE2 — 272 bytes, 128 buttons."""
    _fields_ = [
        ("lX", _LONG),
        ("lY", _LONG),
        ("lZ", _LONG),
        ("lRx", _LONG),
        ("lRy", _LONG),
        ("lRz", _LONG),
        ("rglSlider", _LONG * 2),
        ("rgdwPOV", _DWORD * 4),
        ("rgbButtons", _BYTE * _DI_BUTTON_COUNT),
        ("lVX", _LONG),
        ("lVY", _LONG),
        ("lVZ", _LONG),
        ("lVRx", _LONG),
        ("lVRy", _LONG),
        ("lVRz", _LONG),
        ("rglVSlider", _LONG * 2),
        ("lAX", _LONG),
        ("lAY", _LONG),
        ("lAZ", _LONG),
        ("lARx", _LONG),
        ("lARy", _LONG),
        ("lARz", _LONG),
        ("rglASlider", _LONG * 2),
        ("lFX", _LONG),
        ("lFY", _LONG),
        ("lFZ", _LONG),
        ("lFRx", _LONG),
        ("lFRy", _LONG),
        ("lFRz", _LONG),
        ("rglFSlider", _LONG * 2),
    ]


class _DIOBJECTDATAFORMAT(ctypes.Structure):
    _fields_ = [
        ("pguid", ctypes.c_void_p),
        ("dwOfs", _DWORD),
        ("dwType", _DWORD),
        ("dwFlags", _DWORD),
    ]


class _DIDATAFORMAT(ctypes.Structure):
    _fields_ = [
        ("dwSize", _DWORD),
        ("dwObjSize", _DWORD),
        ("dwFlags", _DWORD),
        ("dwDataSize", _DWORD),
        ("dwNumObjs", _DWORD),
        ("rgodf", ctypes.c_void_p),
    ]


def _field_offset(struct_cls: type[ctypes.Structure], name: str) -> int:
    return getattr(struct_cls, name).offset


def _obj(guid: _GUID | None, ofs: int, dw_type: int, flags: int = 0
         ) -> _DIOBJECTDATAFORMAT:
    o = _DIOBJECTDATAFORMAT()
    o.pguid = ctypes.cast(ctypes.byref(guid), ctypes.c_void_p) if guid else None
    o.dwOfs = ofs
    o.dwType = dw_type
    o.dwFlags = flags
    return o


class BuiltDataFormat(NamedTuple):
    fmt: _DIDATAFORMAT
    objects: ctypes.Array
    num_objs: int
    data_size: int
    obj_size: int
    fmt_size: int


def build_dijoystate2_data_format() -> BuiltDataFormat:
    """Build c_dfDIJoystick2 (DIDATAFORMAT + 164 DIOBJECTDATAFORMAT entries).

    Object list matches Wine/Microsoft ``dfDIJoystick2``: 8 position axes,
    4 POVs, 128 buttons, then velocity/accel/force axis groups. Extra
    slider objects reuse ``DIJOFS_SLIDER(n)`` (Windows quirk).
    """
    slider0 = _field_offset(_DIJOYSTATE2, "rglSlider")
    slider1 = slider0 + ctypes.sizeof(_LONG)
    pov0 = _field_offset(_DIJOYSTATE2, "rgdwPOV")
    btn0 = _field_offset(_DIJOYSTATE2, "rgbButtons")

    objs: list[_DIOBJECTDATAFORMAT] = [
        _obj(GUID_XAxis, _field_offset(_DIJOYSTATE2, "lX"), _TYPE_AXIS, DIDOI_ASPECTPOSITION),
        _obj(GUID_YAxis, _field_offset(_DIJOYSTATE2, "lY"), _TYPE_AXIS, DIDOI_ASPECTPOSITION),
        _obj(GUID_ZAxis, _field_offset(_DIJOYSTATE2, "lZ"), _TYPE_AXIS, DIDOI_ASPECTPOSITION),
        _obj(GUID_RxAxis, _field_offset(_DIJOYSTATE2, "lRx"), _TYPE_AXIS, DIDOI_ASPECTPOSITION),
        _obj(GUID_RyAxis, _field_offset(_DIJOYSTATE2, "lRy"), _TYPE_AXIS, DIDOI_ASPECTPOSITION),
        _obj(GUID_RzAxis, _field_offset(_DIJOYSTATE2, "lRz"), _TYPE_AXIS, DIDOI_ASPECTPOSITION),
        _obj(GUID_Slider, slider0, _TYPE_AXIS, DIDOI_ASPECTPOSITION),
        _obj(GUID_Slider, slider1, _TYPE_AXIS, DIDOI_ASPECTPOSITION),
        _obj(GUID_POV, pov0 + 0, _TYPE_POV),
        _obj(GUID_POV, pov0 + 4, _TYPE_POV),
        _obj(GUID_POV, pov0 + 8, _TYPE_POV),
        _obj(GUID_POV, pov0 + 12, _TYPE_POV),
    ]
    for i in range(_DI_BUTTON_COUNT):
        objs.append(_obj(None, btn0 + i, _TYPE_BUTTON))

    # Velocity / accel / force. Slider extras reuse DIJOFS_SLIDER (Windows).
    extra = (
        (DIDOI_ASPECTVELOCITY, "lVX", "lVY", "lVZ", "lVRx", "lVRy", "lVRz"),
        (DIDOI_ASPECTACCEL, "lAX", "lAY", "lAZ", "lARx", "lARy", "lARz"),
        (DIDOI_ASPECTFORCE, "lFX", "lFY", "lFZ", "lFRx", "lFRy", "lFRz"),
    )
    axis_guids = (GUID_XAxis, GUID_YAxis, GUID_ZAxis,
                  GUID_RxAxis, GUID_RyAxis, GUID_RzAxis)
    for aspect, *names in extra:
        for guid, name in zip(axis_guids, names):
            objs.append(_obj(guid, _field_offset(_DIJOYSTATE2, name),
                             _TYPE_AXIS, aspect))
        objs.append(_obj(GUID_Slider, slider0, _TYPE_AXIS, aspect))
        objs.append(_obj(GUID_Slider, slider1, _TYPE_AXIS, aspect))

    arr_type = _DIOBJECTDATAFORMAT * len(objs)
    arr = arr_type(*objs)

    fmt = _DIDATAFORMAT()
    fmt.dwSize = ctypes.sizeof(_DIDATAFORMAT)
    fmt.dwObjSize = ctypes.sizeof(_DIOBJECTDATAFORMAT)
    fmt.dwFlags = DIDF_ABSAXIS
    fmt.dwDataSize = ctypes.sizeof(_DIJOYSTATE2)
    fmt.dwNumObjs = len(objs)
    fmt.rgodf = ctypes.cast(arr, ctypes.c_void_p)

    return BuiltDataFormat(
        fmt=fmt,
        objects=arr,
        num_objs=len(objs),
        data_size=int(fmt.dwDataSize),
        obj_size=int(fmt.dwObjSize),
        fmt_size=int(fmt.dwSize),
    )


# Built once; pointers in ``fmt.rgodf`` stay valid for the process lifetime.
_BUILT_DF: BuiltDataFormat | None = None


def c_dfDIJoystick2() -> _DIDATAFORMAT:
    """Return the process-wide DIDATAFORMAT for SetDataFormat."""
    global _BUILT_DF
    if _BUILT_DF is None:
        _BUILT_DF = build_dijoystate2_data_format()
    return _BUILT_DF.fmt
