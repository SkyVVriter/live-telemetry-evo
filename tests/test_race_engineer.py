"""Tests for the race engineer core modules."""
import json
import math
from pathlib import Path

import pytest

from live_telemetry_evo.race_engineer.balance import (
    BalanceDetector,
    BalanceType,
    Phase,
)
from live_telemetry_evo.race_engineer.corner_map import (
    Corner,
    TrackProfile,
    detect_corners,
    load_track_profile,
)
from live_telemetry_evo.race_engineer.incidents import (
    IncidentSeverity,
    IncidentThresholds,
    IncidentTracker,
)
from live_telemetry_evo.race_engineer.lap_summary import LapSummaryBuilder
from live_telemetry_evo.race_engineer.proximity import FasterCarBehindDetector
from live_telemetry_evo.telemetry import CarPosition


class TestCornerMap:
    def test_detect_corners_basic(self):
        samples = []
        for i in range(100):
            pct = i / 100.0
            if 0.08 < pct < 0.15:
                speed = 80.0 + abs(pct - 0.10) * 500
                steer = 0.3
            elif 0.40 < pct < 0.50:
                speed = 90.0 + abs(pct - 0.45) * 400
                steer = -0.25
            else:
                speed = 250.0
                steer = 0.01
            samples.append((pct, speed, steer))
        corners = detect_corners(samples, min_speed_drop=15.0, min_steer_rad=0.04)
        assert len(corners) >= 1
        for c in corners:
            assert c.name.startswith("T")
            assert c.start_pct < c.apex_pct < c.end_pct or c.start_pct <= c.apex_pct <= c.end_pct

    def test_detect_corners_empty(self):
        assert detect_corners([]) == []

    def test_load_track_profile(self, track_profile_path):
        profile = load_track_profile(track_profile_path)
        assert profile is not None
        assert profile.track_name == "Test Track"
        assert len(profile.corners) == 2
        assert profile.corners[0].name == "T1"
        assert profile.corners[0].focus is True

    def test_load_missing_profile(self, tmp_path):
        assert load_track_profile(tmp_path / "missing.json") is None

    def test_corner_at(self):
        profile = TrackProfile(corners=[
            Corner("T1", 0.05, 0.10, 0.15),
            Corner("T2", 0.40, 0.45, 0.50),
        ])
        assert profile.corner_at(0.10).name == "T1"
        assert profile.corner_at(0.45).name == "T2"
        assert profile.corner_at(0.25) is None


class TestBalanceDetector:
    def test_neutral(self):
        bd = BalanceDetector(wheelbase=2.7, steering_ratio=14.0)
        speed_kmh = 100.0
        speed_ms = speed_kmh / 3.6
        steer_rad = 0.5
        expected = bd.expected_yaw_rate(speed_ms, steer_rad)
        event = bd.classify(speed_kmh, steer_rad, expected, 0.0, 0.5)
        assert event is None  # within neutral band

    def test_understeer(self):
        bd = BalanceDetector(wheelbase=2.7, steering_ratio=14.0)
        speed_kmh = 120.0
        steer_rad = 0.6
        speed_ms = speed_kmh / 3.6
        expected = bd.expected_yaw_rate(speed_ms, steer_rad)
        actual_yaw = expected * 0.5  # 50% of expected -> strong understeer
        event = bd.classify(speed_kmh, steer_rad, actual_yaw, 0.0, 0.5)
        assert event is not None
        assert event.balance_type in (BalanceType.UNDERSTEER, BalanceType.SLIDE)

    def test_oversteer(self):
        bd = BalanceDetector(wheelbase=2.7, steering_ratio=14.0)
        speed_kmh = 100.0
        steer_rad = 0.4
        speed_ms = speed_kmh / 3.6
        expected = bd.expected_yaw_rate(speed_ms, steer_rad)
        actual_yaw = expected * 1.5  # 50% more than expected -> oversteer
        event = bd.classify(speed_kmh, steer_rad, actual_yaw, 0.0, 0.5)
        assert event is not None
        assert event.balance_type in (BalanceType.OVERSTEER, BalanceType.SLIDE)

    def test_phase_detection(self):
        bd = BalanceDetector()
        speed_kmh = 100.0
        steer_rad = 0.5
        speed_ms = speed_kmh / 3.6
        expected = bd.expected_yaw_rate(speed_ms, steer_rad)
        actual = expected * 0.5

        event_entry = bd.classify(speed_kmh, steer_rad, actual, brake=0.5, throttle=0.0)
        assert event_entry is not None
        assert event_entry.phase == Phase.ENTRY

        event_exit = bd.classify(speed_kmh, steer_rad, actual, brake=0.0, throttle=0.8)
        assert event_exit is not None
        assert event_exit.phase == Phase.EXIT

    def test_low_speed_ignored(self):
        bd = BalanceDetector()
        assert bd.classify(20.0, 0.5, 0.5, 0.0, 0.5) is None

    def test_baseline_learning(self):
        bd = BalanceDetector()
        bd.update_baseline("T1", Phase.ENTRY, 0.5)
        bd.update_baseline("T1", Phase.ENTRY, 0.6)
        bl = bd.get_baseline("T1")
        assert bl is not None
        assert len(bl.entry_yaw_rates) == 2
        assert abs(bl.mean_yaw(Phase.ENTRY) - 0.55) < 0.01


class TestIncidentTracker:
    def test_new_incident(self):
        tracker = IncidentTracker(IncidentThresholds(warn_at=5, dt_limit=10, dq_limit=15))
        alerts = tracker.update(2)
        assert len(alerts) == 1
        assert alerts[0].label == "incident"

    def test_warning_threshold(self):
        tracker = IncidentTracker(IncidentThresholds(warn_at=5, dt_limit=10, dq_limit=15))
        tracker.update(4)
        alerts = tracker.update(5)
        labels = [a.label for a in alerts]
        assert "warn" in labels

    def test_dt_threshold(self):
        tracker = IncidentTracker(IncidentThresholds(warn_at=5, dt_limit=10, dq_limit=15))
        tracker.update(4)
        tracker.update(5)
        alerts = tracker.update(10)
        labels = [a.label for a in alerts]
        assert "dt" in labels

    def test_dq_threshold(self):
        tracker = IncidentTracker(IncidentThresholds(warn_at=5, dt_limit=10, dq_limit=15))
        tracker.update(4)
        tracker.update(9)
        tracker.update(14)
        alerts = tracker.update(15)
        labels = [a.label for a in alerts]
        assert "dq" in labels

    def test_no_duplicate_warnings(self):
        tracker = IncidentTracker(IncidentThresholds(warn_at=3, dt_limit=5, dq_limit=10))
        tracker.update(3)
        alerts1 = [a for a in tracker.update(4) if a.label == "warn"]
        assert len(alerts1) == 0  # already warned

    def test_reset(self):
        tracker = IncidentTracker()
        tracker.update(5)
        tracker.reset()
        assert tracker._last_count == 0

    def test_session_limits(self):
        tracker = IncidentTracker()
        tracker.set_limits_from_session(17, 25)
        assert tracker.thresholds.dt_limit == 17
        assert tracker.thresholds.dq_limit == 25
        assert tracker.thresholds.warn_at < 17


class TestProximity:
    def test_no_alert_when_far(self):
        det = FasterCarBehindDetector(alert_gap_pct=0.03)
        cars = [CarPosition(car_idx=1, position=1, lap_dist_pct=0.50)]
        alerts = det.update(0.80, 2, cars)
        assert alerts == []

    def test_alert_when_closing(self):
        det = FasterCarBehindDetector(alert_gap_pct=0.05)
        cars = [CarPosition(car_idx=1, position=1, lap_dist_pct=0.77,
                            car_number="44", driver_name="Hamilton")]
        det.update(0.80, 2, cars)
        cars[0].lap_dist_pct = 0.78
        alerts = det.update(0.80, 2, cars)
        assert len(alerts) == 1
        assert "Hamilton" in alerts[0].message

    def test_no_alert_for_cars_behind_in_position(self):
        det = FasterCarBehindDetector()
        cars = [CarPosition(car_idx=1, position=3, lap_dist_pct=0.79)]
        alerts = det.update(0.80, 2, cars)
        assert alerts == []

    def test_reset(self):
        det = FasterCarBehindDetector()
        det._alerted.add(1)
        det.reset()
        assert len(det._alerted) == 0


class TestLapSummary:
    def test_basic_summary(self):
        bd = BalanceDetector()
        it = IncidentTracker()
        builder = LapSummaryBuilder(bd, it)
        builder.new_lap(1, fuel=50.0, incidents=0)
        builder.add_sample(200.0, 0.1, 0.1, 0.0, 0.8, 0.5, 48.0, 0)
        builder.add_sample(250.0, 0.01, 0.01, 0.0, 1.0, 0.7, 47.5, 0)
        summary = builder.finish_lap(lap_time=90.5)
        assert summary.lap_number == 1
        assert summary.lap_time == 90.5
        assert summary.top_speed == 250.0
        assert summary.fuel_used == pytest.approx(2.5, abs=0.1)

    def test_incident_tracking_in_summary(self):
        bd = BalanceDetector()
        it = IncidentTracker(IncidentThresholds(warn_at=2, dt_limit=5, dq_limit=10))
        builder = LapSummaryBuilder(bd, it)
        builder.new_lap(1, fuel=50.0, incidents=0)
        builder.add_sample(200.0, 0.1, 0.1, 0.0, 0.8, 0.5, 49.0, 3)
        summary = builder.finish_lap()
        assert summary.incidents_this_lap == 3
        assert len(summary.incident_alerts) > 0
