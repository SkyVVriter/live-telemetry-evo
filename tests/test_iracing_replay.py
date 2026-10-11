"""Tests for iRacing CSV replay/mock mode."""
from pathlib import Path

import pytest

from live_telemetry_evo.sources.iracing import load_garage61_csv


class TestGarage61CSV:
    def test_load(self, sample_csv_path):
        rows = load_garage61_csv(sample_csv_path)
        assert len(rows) > 0
        assert "Speed" in rows[0]
        assert "LapDistPct" in rows[0]
        assert "Throttle" in rows[0]

    def test_values_are_floats(self, sample_csv_path):
        rows = load_garage61_csv(sample_csv_path)
        for row in rows:
            for key, val in row.items():
                assert isinstance(val, float), f"{key}={val} is not float"

    def test_speed_range(self, sample_csv_path):
        rows = load_garage61_csv(sample_csv_path)
        speeds = [r["Speed"] for r in rows]
        assert min(speeds) > 0
        assert max(speeds) < 400

    def test_lap_dist_pct_range(self, sample_csv_path):
        rows = load_garage61_csv(sample_csv_path)
        pcts = [r["LapDistPct"] for r in rows]
        assert min(pcts) >= 0.0
        assert max(pcts) <= 1.0

    def test_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_garage61_csv(tmp_path / "nonexistent.csv")

    def test_empty_csv(self, tmp_path):
        path = tmp_path / "empty.csv"
        path.write_text(
            "Speed,LapDistPct,Lat,Lon,Brake,Throttle,RPM,"
            "SteeringWheelAngle,Gear,Clutch,ABSActive,LatAccel,"
            "LongAccel,VertAccel,Yaw,YawRate\n",
            encoding="utf-8"
        )
        rows = load_garage61_csv(path)
        assert rows == []
