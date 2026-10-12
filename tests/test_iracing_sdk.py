"""Read-only iRacing SDK invariants (Garage 61 / telemetry-safe)."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from live_telemetry_evo.sources.iracing import _sdk_int, _sdk_position
from live_telemetry_evo.sources.irsdk_readonly import (
    FILE_MAP_READ, FILE_MAP_WRITE, IRACING_MAX_HZ, SYNCHRONIZE,
    cap_iracing_hz, wait_timeout_ms,
)

_SRC = Path(__file__).parent.parent / "src" / "live_telemetry_evo" / "sources"


class TestIRSDKCaps:
    def test_hz_capped_at_60(self):
        assert cap_iracing_hz(250) == IRACING_MAX_HZ == 60
        assert cap_iracing_hz(30) == 30
        assert cap_iracing_hz(0) == 1

    def test_wait_timeout_not_a_spin(self):
        assert wait_timeout_ms(60) >= 16
        assert wait_timeout_ms(250) >= 16
        assert wait_timeout_ms(30) <= 50
        assert wait_timeout_ms(1) == 50


class TestIRSDKSourceScans:
    def test_live_source_never_starts_pyirsdk_or_freezes(self):
        text = (_SRC / "iracing.py").read_text(encoding="utf-8")
        assert "self._ir.startup(" not in text
        assert ".freeze_var_buffer_latest(" not in text
        assert "telem_command" not in text
        assert "BroadcastTelemCommand" not in text
        assert "SendNotifyMessage" not in text
        assert "app.ini" not in text
        assert "self._ro.wait(" in text
        assert "_run" in text

    def test_readonly_module_is_ffb_and_g61_safe(self):
        text = (_SRC / "irsdk_readonly.py").read_text(encoding="utf-8")
        assert "OpenFileMappingW" in text
        assert "FILE_MAP_READ" in text
        assert "WaitForSingleObject" in text
        assert "SYNCHRONIZE" in text
        assert "CreateFileMappingW" not in text
        assert "ResetEvent(" not in text
        assert "SetEvent(" not in text
        assert "FILE_MAP_WRITE" in text  # named so we can refuse it
        assert "OpenFileMappingW(FILE_MAP_WRITE" not in text
        assert "OpenEventW(SYNCHRONIZE" in text
        assert "telem_command" in text  # disabled wrapper
        assert "_forbid_broadcast" in text
        assert "app.ini" in text  # mentioned as never-touch
        assert FILE_MAP_READ == 0x0004
        assert FILE_MAP_WRITE == 0x0002
        assert SYNCHRONIZE == 0x00100000


class _FakeRO:
    def __init__(self) -> None:
        self.waits: list[int] = []
        self.opened = False
        self.closed = False
        self._tick = 0

    def open(self) -> bool:
        self.opened = True
        return True

    def close(self) -> None:
        self.closed = True
        self.opened = False

    @property
    def is_open(self) -> bool:
        return self.opened

    def is_connected(self) -> bool:
        return self.opened

    def wait(self, timeout_ms: int) -> bool:
        self.waits.append(int(timeout_ms))
        time.sleep(min(timeout_ms, 20) / 1000)
        return True

    def tick_count(self) -> int:
        self._tick += 1
        return self._tick

    def buffer(self):
        return object()


class TestWaitLoop:
    def test_wait_timeout_paces_worker_without_busy_spin(self):
        ro = _FakeRO()
        stop = threading.Event()

        def _run():
            while not stop.is_set():
                ro.wait(wait_timeout_ms(60))

        worker = threading.Thread(target=_run, name="irsdk-test")
        t0 = time.monotonic()
        worker.start()
        ticks = 0
        while time.monotonic() - t0 < 0.08:
            ticks += 1
            time.sleep(0.01)
        stop.set()
        worker.join(0.5)
        assert ticks >= 6, f"caller only ticked {ticks} times (blocked?)"
        assert ro.waits, "wait path never ran"
        assert all(w >= 16 for w in ro.waits)
        assert len(ro.waits) < 20, f"busy-spun {len(ro.waits)} waits in 80ms"


class TestSdkValueHelpers:
    def test_position_rejects_minus_one(self):
        assert _sdk_position(-1) == 0
        assert _sdk_position(0) == 0
        assert _sdk_position(None) == 0
        assert _sdk_position(4) == 4

    def test_session_state_and_lap_completed(self):
        assert _sdk_int(5, 0) == 5
        assert _sdk_int(None, 0) == 0
        assert _sdk_int("3", 0) == 3


class TestForbidBroadcast:
    def test_attach_disables_broadcast(self):
        from live_telemetry_evo.sources.irsdk_readonly import _forbid_broadcast
        try:
            _forbid_broadcast()
            assert False, "broadcast should be disabled"
        except RuntimeError as e:
            assert "broadcast is disabled" in str(e)
