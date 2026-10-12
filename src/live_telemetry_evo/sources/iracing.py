"""iRacing telemetry source via a read-only IRSDK client.

Live path (Windows): we open ``Local\\IRSDKMemMapFileName`` with
``FILE_MAP_READ`` and wait on ``Local\\IRSDKDataValidEvent`` via
``WaitForSingleObject`` (SYNCHRONIZE only). pyirsdk is used only as a
parser on that view — we never call pyirsdk startup or freeze-latest,
or any broadcast / telem / replay command.

Replay path: Garage 61 CSV, testable without a sim.
"""
from __future__ import annotations

import csv
import sys
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QTimer

from ..logbook import log
from ..telemetry import CarPosition, IRacingData, TelemetryFrame, WHEEL_IDS
from .base import TelemetrySource
from .irsdk_readonly import (
    ReadOnlyIRSDK, attach_pyirsdk, cap_iracing_hz, wait_timeout_ms,
)


def _try_import_irsdk():
    """Lazy import of pyirsdk; returns the module or None."""
    if sys.platform != "win32":
        return None
    try:
        import irsdk  # type: ignore[import-untyped]
        return irsdk
    except ImportError:
        return None


def _parse_session_yaml(ir) -> dict:
    """Extract useful fields from iRacing's session info YAML (parsed once
    per connect, refreshed on session changes)."""
    info: dict = {}
    try:
        weekend = ir["WeekendInfo"]
        info["track_id"] = int(weekend.get("TrackID", 0))
        info["track_name"] = weekend.get("TrackDisplayName", "")
        info["track_config"] = weekend.get("TrackConfigName", "")
        info["series"] = (
            weekend.get("SeriesName")
            or weekend.get("SeasonName")
            or ""
        )
        info["incident_limit"] = 0
        sessions = ir["SessionInfo"]["Sessions"]
        for s in sessions:
            lim = s.get("ResultsOfficialIncidentLimit")
            if lim and int(lim) > 0:
                info["incident_limit"] = int(lim)
                break
        try:
            current_num = int(ir["SessionNum"] or 0)
        except (KeyError, TypeError, ValueError):
            current_num = 0
        info["session_num"] = current_num
        matched_type = ""
        for s in sessions:
            try:
                if int(s.get("SessionNum", -1)) == current_num:
                    matched_type = s.get("SessionType", "") or ""
                    break
            except (TypeError, ValueError):
                continue
        if not matched_type and sessions:
            matched_type = sessions[0].get("SessionType", "") or ""
        info["session_type"] = matched_type
    except (KeyError, TypeError, ValueError):
        pass
    try:
        drivers_raw = ir["DriverInfo"]["Drivers"]
        info["drivers"] = [
            {
                "car_idx": int(d.get("CarIdx", -1)),
                "name": d.get("UserName", ""),
                "car_number": str(d.get("CarNumber", "")),
                "car_name": d.get("CarScreenName", ""),
            }
            for d in drivers_raw
        ]
        player_idx = int(ir["DriverInfo"]["DriverCarIdx"])
        for d in info["drivers"]:
            if d["car_idx"] == player_idx:
                info["car_name"] = d["car_name"]
                info["driver"] = d["name"]
                break
    except (KeyError, TypeError, ValueError):
        info["drivers"] = []
    return info


class IRacingTelemetrySource(TelemetrySource):
    """Live iRacing telemetry: read-only shared memory, wait-for-event."""

    def __init__(self, hz: int = 60, parent: QObject | None = None,
                 ro_client: ReadOnlyIRSDK | None = None) -> None:
        super().__init__(parent)
        self._hz = cap_iracing_hz(hz)
        self._frame = TelemetryFrame()
        self._ir = None
        self._ro = ro_client if ro_client is not None else ReadOnlyIRSDK()
        self._connected = False
        self._session_info_version = -1
        self._last_tick = -1
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        irsdk = _try_import_irsdk()
        if irsdk is None:
            log("[iracing] pyirsdk not available (non-Windows or not installed)")
            return
        self._ir = irsdk.IRSDK()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="irsdk-readonly", daemon=True)
        self._thread.start()
        log(f"[iracing] read-only poller started (max {self._hz} Hz, "
            f"wait={wait_timeout_ms(self._hz)}ms; no broadcast, no .ibt)")

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=0.5)
        self._thread = None
        self._ro.close()
        self._connected = False
        self._ir = None

    def set_hz(self, hz: int) -> None:
        self._hz = cap_iracing_hz(hz)

    def _try_connect(self) -> bool:
        if self._ir is None:
            return False
        if not self._ro.open():
            return False
        if not attach_pyirsdk(self._ir, self._ro.buffer()):
            self._ro.close()
            return False
        self._connected = True
        self._last_tick = -1
        self._session_info_version = -1
        log("[iracing] connected (FILE_MAP_READ, no CreateFileMapping, "
            "no freeze_var_buffer_latest)")
        return True

    def _disconnect(self) -> None:
        self._connected = False
        self._ro.close()
        if self._ir is not None:
            self._ir._shared_mem = None
            self._ir._header = None
            self._ir.is_initialized = False

    def _run(self) -> None:
        while not self._stop.is_set():
            timeout = wait_timeout_ms(self._hz)
            if not self._connected:
                if not self._try_connect():
                    self._stop.wait(0.2)
                    continue
            if not self._ro.is_connected():
                self._disconnect()
                self._stop.wait(0.2)
                continue
            self._ro.wait(timeout)
            if self._stop.is_set():
                break
            if not self._ro.is_connected():
                self._disconnect()
                continue
            tick = self._ro.tick_count()
            if tick == self._last_tick:
                continue
            self._last_tick = tick
            try:
                header = getattr(self._ir, "_header", None)
                if header is not None:
                    ver = header.session_info_update
                    if ver != self._session_info_version:
                        self._session_info_version = ver
                        self._parse_session()
                self._read_telemetry()
                if self._bus is not None:
                    self._bus.publish(self._frame)
            except Exception as e:
                log(f"[iracing] read error: {e}")

    def _parse_session(self) -> None:
        info = _parse_session_yaml(self._ir)
        ir_data = self._frame.iracing
        ir_data.track_id_iracing = info.get("track_id", 0)
        ir_data.track_name = info.get("track_name", "")
        ir_data.track_config = info.get("track_config", "")
        ir_data.car_name = info.get("car_name", "")
        ir_data.series = info.get("series", "")
        ir_data.session_type = info.get("session_type", "")
        ir_data.session_num = info.get("session_num", 0)
        ir_data.driver = info.get("driver", "")
        ir_data.incident_limit = info.get("incident_limit", 0)
        ir_data.drivers = info.get("drivers", [])

        self._frame.engine.track_id = str(ir_data.track_id_iracing)
        self._frame.engine.track_config = ir_data.track_config
        self._frame.engine.car_model = ir_data.car_name

    def _read_telemetry(self) -> None:
        ir = self._ir
        frame = self._frame
        eng = frame.engine
        inp = frame.inputs
        ir_data = frame.iracing

        eng.rpm = float(ir["RPM"] or 0)
        eng.speed_kmh = float(ir["Speed"] or 0) * 3.6
        eng.gear = int(ir["Gear"] or 0) + 1  # iRacing: -1=R,0=N,1+=fwd -> our 0=R,1=N,2+=fwd

        inp.throttle = float(ir["Throttle"] or 0)
        inp.brake = float(ir["Brake"] or 0)
        inp.clutch = float(ir["Clutch"] or 0)
        inp.steering = float(ir["SteeringWheelAngle"] or 0)

        g_lat = ir["LatAccel"]
        g_long = ir["LongAccel"]
        g_vert = ir["VertAccel"]
        if g_lat is not None:
            inp.g_lat = float(g_lat) / 9.81
        if g_long is not None:
            inp.g_long = float(g_long) / 9.81
        if g_vert is not None:
            inp.g_vert = float(g_vert) / 9.81

        ir_data.lap_dist_pct = float(ir["LapDistPct"] or 0)
        ir_data.yaw_rate = float(ir["YawRate"] or 0)
        ir_data.steering_angle_rad = float(ir["SteeringWheelAngle"] or 0)
        ir_data.incident_count = int(ir["PlayerCarMyIncidentCount"] or 0)
        ir_data.on_pit_road = bool(ir["OnPitRoad"])
        ir_data.session_flags = int(ir["SessionFlags"] or 0)
        ir_data.player_position = int(ir["PlayerCarPosition"] or 0)
        ir_data.player_class_position = int(ir["PlayerCarClassPosition"] or 0)

        eng.fuel_liters = float(ir["FuelLevel"] or 0)
        eng.water_temp_c = float(ir["WaterTemp"] or 0)
        eng.oil_temp_c = float(ir["OilTemp"] or 0)
        eng.oil_pressure_bar = float(ir["OilPress"] or 0) / 100.0

        eng.current_lap = int(ir["Lap"] or 1)
        last_time_raw = ir["LapLastLapTime"]
        if last_time_raw and last_time_raw > 0:
            eng.last_lap_time_ms = int(last_time_raw * 1000)
        best_time_raw = ir["LapBestLapTime"]
        if best_time_raw and best_time_raw > 0:
            eng.best_lap_time_ms = int(best_time_raw * 1000)

        try:
            positions = ir["CarIdxPosition"]
            lap_pcts = ir["CarIdxLapDistPct"]
            est_times = ir["CarIdxEstTime"]
            last_laps = ir["CarIdxLastLapTime"]
            if positions and lap_pcts:
                cars = []
                driver_map = {d["car_idx"]: d for d in ir_data.drivers}
                for idx in range(len(positions)):
                    if positions[idx] > 0:
                        d = driver_map.get(idx, {})
                        cars.append(CarPosition(
                            car_idx=idx,
                            position=positions[idx],
                            lap_dist_pct=lap_pcts[idx] if lap_pcts else 0.0,
                            est_time=est_times[idx] if est_times else 0.0,
                            last_lap_time=last_laps[idx] if last_laps and last_laps[idx] > 0 else 0.0,
                            car_number=d.get("car_number", ""),
                            driver_name=d.get("name", ""),
                        ))
                ir_data.cars = cars
        except (TypeError, IndexError):
            pass


# ---------------------------------------------------------------------------
# Garage 61 CSV Replay
# ---------------------------------------------------------------------------

_G61_COLUMN_MAP = {
    "Speed": "speed_kmh",
    "LapDistPct": "lap_dist_pct",
    "Lat": "g_lat",
    "Lon": "g_long",
    "Brake": "brake",
    "Throttle": "throttle",
    "RPM": "rpm",
    "SteeringWheelAngle": "steering_angle_rad",
    "Gear": "gear",
    "Clutch": "clutch",
    "ABSActive": "abs_active",
    "LatAccel": "lat_accel",
    "LongAccel": "long_accel",
    "VertAccel": "vert_accel",
    "Yaw": "yaw",
    "YawRate": "yaw_rate",
}


def load_garage61_csv(path: Path) -> list[dict[str, float]]:
    """Load a Garage 61 CSV into a list of row dicts with float values."""
    rows: list[dict[str, float]] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            row: dict[str, float] = {}
            for col, val in raw.items():
                col = col.strip()
                try:
                    row[col] = float(val)
                except (ValueError, TypeError):
                    row[col] = 0.0
            rows.append(row)
    return rows


class IRacingReplaySource(TelemetrySource):
    """Replay iRacing telemetry from a Garage 61 CSV file.

    Feeds frames at the configured Hz, looping the CSV. Testable on any
    platform (no pyirsdk dependency).
    """

    def __init__(self, csv_path: str | Path, hz: int = 60,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._csv_path = Path(csv_path)
        self._hz = hz
        self._rows: list[dict[str, float]] = []
        self._idx = 0
        self._frame = TelemetryFrame()
        self._timer: Optional[QTimer] = None

    def start(self) -> None:
        self._rows = load_garage61_csv(self._csv_path)
        if not self._rows:
            log(f"[iracing-replay] empty CSV: {self._csv_path}")
            return
        log(f"[iracing-replay] loaded {len(self._rows)} rows from {self._csv_path.name}")
        self._timer = QTimer(self)
        self._timer.setInterval(int(1000 / self._hz))
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def stop(self) -> None:
        if self._timer:
            self._timer.stop()

    def set_hz(self, hz: int) -> None:
        hz = max(1, int(hz))
        self._hz = hz
        if self._timer:
            self._timer.setInterval(int(1000 / hz))

    def _tick(self) -> None:
        if not self._rows:
            return
        row = self._rows[self._idx % len(self._rows)]
        self._idx += 1
        self._apply_row(row)
        self.frame.emit(self._frame)
        if self._bus is not None:
            self._bus.publish(self._frame)

    def _apply_row(self, row: dict[str, float]) -> None:
        f = self._frame
        eng = f.engine
        inp = f.inputs
        ir_data = f.iracing

        eng.speed_kmh = row.get("Speed", 0.0)
        eng.rpm = row.get("RPM", 0.0)
        raw_gear = row.get("Gear", 0.0)
        eng.gear = int(raw_gear) + 1  # iRacing convention

        inp.throttle = row.get("Throttle", 0.0)
        inp.brake = row.get("Brake", 0.0)
        inp.clutch = row.get("Clutch", 0.0)
        inp.steering = row.get("SteeringWheelAngle", 0.0)

        lat_a = row.get("LatAccel", 0.0)
        long_a = row.get("LongAccel", 0.0)
        vert_a = row.get("VertAccel", 0.0)
        inp.g_lat = lat_a / 9.81 if abs(lat_a) > 0.01 else lat_a
        inp.g_long = long_a / 9.81 if abs(long_a) > 0.01 else long_a
        inp.g_vert = vert_a / 9.81 if abs(vert_a) > 0.01 else vert_a

        ir_data.lap_dist_pct = row.get("LapDistPct", 0.0)
        ir_data.yaw_rate = row.get("YawRate", 0.0)
        ir_data.steering_angle_rad = row.get("SteeringWheelAngle", 0.0)
