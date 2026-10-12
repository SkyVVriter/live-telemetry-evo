"""Read-only iRacing shared-memory client (Garage 61 / FFB-safe).

Must never interfere with other IRSDK clients (Garage 61, CrewChief) or
with iRacing disk telemetry:

- Open ``Local\\IRSDKMemMapFileName`` with ``FILE_MAP_READ`` only.
  Never ``CreateFileMapping`` (Python ``mmap`` would create a dummy map),
  never ``FILE_MAP_WRITE`` / ``FILE_MAP_ALL_ACCESS``, never write.
- Open ``Local\\IRSDKDataValidEvent`` with ``SYNCHRONIZE`` only.
  ``WaitForSingleObject`` with a timeout; never ``ResetEvent`` / ``SetEvent``.
- Never broadcast (``IRSDK_BROADCASTMSG``, telem start/stop/restart,
  replay, camera). Never write ``.ibt`` or touch ``app.ini``.
"""
from __future__ import annotations

import ctypes
import struct
import sys
from ..logbook import log

IRSDK_MEMMAP_NAME = "Local\\IRSDKMemMapFileName"
IRSDK_DATAVALID_NAME = "Local\\IRSDKDataValidEvent"
IRSDK_MEMMAP_SIZE = 1164 * 1024
IRSDK_BROADCAST_MSG = "IRSDK_BROADCASTMSG"

FILE_MAP_READ = 0x0004
FILE_MAP_WRITE = 0x0002
FILE_MAP_ALL_ACCESS = 0x000F001F
SYNCHRONIZE = 0x00100000
EVENT_MODIFY_STATE = 0x0002
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x00000102

# iRacing publishes at most ~60 Hz. Faster overlay polling is wasted CPU
# and just hammers the data-valid event.
IRACING_MAX_HZ = 60

_STATUS_CONNECTED = 1
_OFF_STATUS = 4
_OFF_TICK_RATE = 8
_OFF_SESSION_INFO_UPDATE = 12
_OFF_CUR_BUF_TICK = 40


def wait_timeout_ms(hz: int) -> int:
    """Timeout for WaitForSingleObject: paced by Hz, never a tight spin."""
    hz = max(1, min(int(hz), IRACING_MAX_HZ))
    return max(16, min(50, int(1000 / hz)))


def cap_iracing_hz(hz: int) -> int:
    return max(1, min(int(hz), IRACING_MAX_HZ))


def _forbid_broadcast(*_a, **_k):
    raise RuntimeError(
        "iRacing broadcast is disabled (Garage 61 / telemetry-safe)")


class ReadOnlyIRSDK:
    """Owns the IRSDK mapping and data-valid event. Read-only."""

    def __init__(self) -> None:
        self._map_handle = 0
        self._view = 0
        self._event = 0
        self._size = IRSDK_MEMMAP_SIZE
        self._buf = None

    @property
    def is_open(self) -> bool:
        return bool(self._view)

    def open(self) -> bool:
        """Open existing mapping + event. False if iRacing is not running."""
        if sys.platform != "win32":
            return False
        self.close()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        OpenFileMappingW = kernel32.OpenFileMappingW
        OpenFileMappingW.argtypes = [
            ctypes.c_uint32, ctypes.c_int32, ctypes.c_wchar_p]
        OpenFileMappingW.restype = ctypes.c_void_p

        MapViewOfFile = kernel32.MapViewOfFile
        MapViewOfFile.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_size_t]
        MapViewOfFile.restype = ctypes.c_void_p

        OpenEventW = kernel32.OpenEventW
        OpenEventW.argtypes = [
            ctypes.c_uint32, ctypes.c_int32, ctypes.c_wchar_p]
        OpenEventW.restype = ctypes.c_void_p

        handle = OpenFileMappingW(FILE_MAP_READ, False, IRSDK_MEMMAP_NAME)
        if not handle:
            return False
        view = MapViewOfFile(handle, FILE_MAP_READ, 0, 0, self._size)
        if not view:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            return False
        event = OpenEventW(SYNCHRONIZE, False, IRSDK_DATAVALID_NAME)
        # Event is optional for a first snapshot, but we need it to wait.
        if not event:
            kernel32.UnmapViewOfFile(ctypes.c_void_p(view))
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            return False

        self._map_handle = int(handle)
        self._view = int(view)
        self._event = int(event)
        self._buf = (ctypes.c_char * self._size).from_address(self._view)
        log("[iracing] opened IRSDKMemMapFileName FILE_MAP_READ, "
            "IRSDKDataValidEvent SYNCHRONIZE (no ResetEvent, no broadcast)")
        return True

    def wait(self, timeout_ms: int) -> bool:
        """WaitForSingleObject on the data-valid event. Never ResetEvent."""
        if sys.platform != "win32" or not self._event:
            return False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        WaitForSingleObject = kernel32.WaitForSingleObject
        WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        WaitForSingleObject.restype = ctypes.c_uint32
        rc = WaitForSingleObject(ctypes.c_void_p(self._event), int(timeout_ms))
        return rc == WAIT_OBJECT_0

    def header_status(self) -> int:
        return self._unpack_i32(_OFF_STATUS)

    def tick_count(self) -> int:
        return self._unpack_i32(_OFF_CUR_BUF_TICK)

    def tick_rate(self) -> int:
        rate = self._unpack_i32(_OFF_TICK_RATE)
        return rate if rate > 0 else IRACING_MAX_HZ

    def session_info_update(self) -> int:
        return self._unpack_i32(_OFF_SESSION_INFO_UPDATE)

    def is_connected(self) -> bool:
        return self.is_open and bool(self.header_status() & _STATUS_CONNECTED)

    def buffer(self):
        return self._buf

    def _unpack_i32(self, offset: int) -> int:
        if self._buf is None:
            return 0
        return struct.unpack_from("<i", self._buf, offset)[0]

    def close(self) -> None:
        if sys.platform != "win32":
            self._buf = None
            self._view = self._map_handle = self._event = 0
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if self._event:
            kernel32.CloseHandle(ctypes.c_void_p(self._event))
            self._event = 0
        if self._view:
            kernel32.UnmapViewOfFile(ctypes.c_void_p(self._view))
            self._view = 0
        if self._map_handle:
            kernel32.CloseHandle(ctypes.c_void_p(self._map_handle))
            self._map_handle = 0
        self._buf = None


def attach_pyirsdk(ir, buf) -> bool:
    """Point an existing ``irsdk.IRSDK()`` at our read-only view.

    Does not call ``startup()`` (that uses Python ``mmap`` which can
    ``CreateFileMapping``). Leaves ``_data_valid_event`` as None so
    pyirsdk will not ``WaitForSingleObject`` / freeze. Broadcast methods
    are disabled.
    """
    if ir is None or buf is None:
        return False
    try:
        import irsdk  # type: ignore[import-untyped]
    except ImportError:
        return False
    ir._shared_mem = buf
    ir._header = irsdk.Header(buf)
    ir._data_valid_event = None
    ir._IRSDK__var_headers = None
    ir._IRSDK__var_headers_dict = None
    ir._IRSDK__var_headers_names = None
    ir._IRSDK__var_buffer_latest = None
    ir._IRSDK__session_info_dict = {}
    ir._IRSDK__broadcast_msg_id = None
    ir.last_session_info_update = 0
    ir.is_initialized = (
        getattr(ir._header, "version", 0) >= 1
        and getattr(ir._header, "num_buf", 0) > 0
    )
    ir._broadcast_msg = _forbid_broadcast
    ir.telem_command = _forbid_broadcast
    ir.replay_set_state = _forbid_broadcast
    ir.replay_set_play_speed = _forbid_broadcast
    ir.replay_search = _forbid_broadcast
    ir.cam_switch_pos = _forbid_broadcast
    ir.ffb_command = _forbid_broadcast
    return bool(ir.is_initialized)
