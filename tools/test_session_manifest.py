"""Tests for CSV manifest session/weather snapshot (no Qt required)."""

from __future__ import annotations

import unittest

from live_telemetry_evo.logger import MANIFEST_VERSION, build_telemetry_manifest
from live_telemetry_evo.telemetry import ENGINE_SESSION_FIELDS, EngineData, TelemetryFrame


class ManifestTests(unittest.TestCase):
    def test_session_weather_snapshot(self) -> None:
        engine = EngineData(
            session_type=2,
            session_name="Qualifying",
            is_timed_race=False,
            is_online=True,
            starting_grip=5,
            static_weather=True,
            starting_air_temp_c=18.5,
            starting_road_temp_c=22.0,
            air_temp_c=19.25,
            road_temp_c=23.5,
            tod_hours=16,
            tod_minutes=5,
            tod_seconds=9,
        )
        blob = build_telemetry_manifest(
            "2026-08-21T08:00:00+00:00",
            "laguna_seca",
            "gp",
            "Mazda MX-5 ND Cup",
            "SkyVVriter",
            engine,
        )
        self.assertEqual(blob["version"], MANIFEST_VERSION)
        session = blob["session"]
        self.assertEqual(session["session_type"], 2)
        self.assertEqual(session["session_name"], "Qualifying")
        self.assertTrue(session["is_online"])
        self.assertEqual(session["grip"], 5)
        self.assertEqual(session["air_temp_c"], 19.25)
        self.assertEqual(session["time_of_day"], "16:05:09")
        self.assertEqual(blob["vehicle"]["car_id"], "Mazda MX-5 ND Cup")

    def test_reset_for_car_keeps_session_fields(self) -> None:
        frame = TelemetryFrame()
        frame.engine.session_type = 3
        frame.engine.session_name = "Race"
        frame.engine.starting_grip = 1
        frame.engine.air_temp_c = 12.0
        frame.engine.track_id = "spa"
        frame.engine.car_model = "old_car"
        frame.reset_for_car("new_car")
        self.assertEqual(frame.engine.session_type, 3)
        self.assertEqual(frame.engine.session_name, "Race")
        self.assertEqual(frame.engine.starting_grip, 1)
        self.assertEqual(frame.engine.air_temp_c, 12.0)
        self.assertEqual(frame.engine.track_id, "spa")
        self.assertEqual(frame.engine.car_model, "")
        self.assertTrue(set(ENGINE_SESSION_FIELDS))


if __name__ == "__main__":
    unittest.main()
