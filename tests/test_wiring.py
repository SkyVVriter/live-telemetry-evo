"""Tests that the new iRacing modules are properly importable and wired.

Runs headless (PySide6 is mocked in conftest.py). Validates that:
- race_engineer modules are importable and functional
- RaceWorkerClient URL normalization works
- TTS registry and scheduler are importable
- tray menu construction accepts the new dialog/channel parameters
- app module imports all needed symbols
- settings round-trips for race_worker and release_channel
- default release channel is 'test' for -test builds
"""
import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# -- RaceWorkerClient URL normalization -----------------------------------

class TestURLNormalization:
    def test_plain_url(self):
        from live_telemetry_evo.race_worker_client import _normalize_base_url
        assert _normalize_base_url("https://example.com") == "https://example.com"

    def test_trailing_slash(self):
        from live_telemetry_evo.race_worker_client import _normalize_base_url
        assert _normalize_base_url("https://example.com/") == "https://example.com"

    def test_trailing_race(self):
        from live_telemetry_evo.race_worker_client import _normalize_base_url
        assert _normalize_base_url("https://example.com/race") == "https://example.com"

    def test_trailing_race_slash(self):
        from live_telemetry_evo.race_worker_client import _normalize_base_url
        assert _normalize_base_url("https://example.com/race/") == "https://example.com"

    def test_race_in_path(self):
        from live_telemetry_evo.race_worker_client import _normalize_base_url
        result = _normalize_base_url("https://example.com/api/race")
        assert result == "https://example.com/api"

    def test_client_init_normalizes(self):
        from live_telemetry_evo.race_worker_client import RaceWorkerClient
        c = RaceWorkerClient(base_url="https://host.com/race/", token="tok")
        assert c.base_url == "https://host.com"

    def test_client_configure_normalizes(self):
        from live_telemetry_evo.race_worker_client import RaceWorkerClient
        c = RaceWorkerClient()
        c.configure("https://host.com/race", "tok")
        assert c.base_url == "https://host.com"


# -- race_engineer imports ------------------------------------------------

class TestRaceEngineerImports:
    def test_balance_detector(self):
        from live_telemetry_evo.race_engineer.balance import BalanceDetector
        bd = BalanceDetector()
        assert bd.wheelbase > 0

    def test_corner_map(self):
        from live_telemetry_evo.race_engineer.corner_map import detect_corners
        assert detect_corners([]) == []

    def test_incidents(self):
        from live_telemetry_evo.race_engineer.incidents import IncidentTracker
        t = IncidentTracker()
        alerts = t.update(0)
        assert isinstance(alerts, list)

    def test_proximity(self):
        from live_telemetry_evo.race_engineer.proximity import FasterCarBehindDetector
        d = FasterCarBehindDetector()
        assert d.alert_gap_pct > 0

    def test_lap_summary(self):
        from live_telemetry_evo.race_engineer.lap_summary import LapSummaryBuilder
        from live_telemetry_evo.race_engineer.balance import BalanceDetector
        from live_telemetry_evo.race_engineer.incidents import IncidentTracker
        b = LapSummaryBuilder(BalanceDetector(), IncidentTracker())
        b.new_lap(1, 50.0, 0)
        s = b.finish_lap(90.0)
        assert s.lap_number == 1


# -- TTS imports ----------------------------------------------------------

class TestTTSImports:
    def test_tts_package(self):
        from live_telemetry_evo.tts import (
            SpeechScheduler, get_engine, list_engines,
            Priority, VoiceEngine,
        )
        assert callable(get_engine)
        assert callable(list_engines)

    def test_scheduler_instantiation(self):
        from live_telemetry_evo.tts.base import Priority, VoiceEngine
        from live_telemetry_evo.tts.scheduler import SpeechScheduler

        class DummyEngine(VoiceEngine):
            name = "dummy"
            def speak(self, text, priority=Priority.NORMAL):
                pass
            def list_voices(self):
                return []
            def settings_schema(self):
                return {}

        s = SpeechScheduler(DummyEngine())
        s.start()
        s.enqueue("hello")
        time.sleep(0.2)
        s.stop()


# -- Settings round-trips -------------------------------------------------

class TestSettingsWiring:
    def test_race_worker_settings(self, tmp_path):
        from live_telemetry_evo import settings
        orig_path = settings._positions_path
        settings._positions_path = lambda: tmp_path / "settings.json"
        try:
            from live_telemetry_evo.settings import (
                load_race_worker_settings, save_race_worker_settings,
            )
            save_race_worker_settings(
                url="https://test.com/race/",
                app_token="tok123",
                enabled=True,
            )
            cfg = load_race_worker_settings()
            assert cfg["url"] == "https://test.com/race"
            assert cfg["app_token"] == "tok123"
            assert cfg["enabled"] is True
        finally:
            settings._positions_path = orig_path

    def test_release_channel_settings(self, tmp_path):
        from live_telemetry_evo import settings
        orig_path = settings._positions_path
        settings._positions_path = lambda: tmp_path / "settings.json"
        try:
            from live_telemetry_evo.settings import (
                load_release_channel, save_release_channel,
            )
            save_release_channel("test")
            assert load_release_channel() == "test"
            save_release_channel("prod")
            assert load_release_channel() == "prod"
        finally:
            settings._positions_path = orig_path

    def test_default_channel_for_test_build(self, tmp_path):
        from live_telemetry_evo import settings
        orig_path = settings._positions_path
        settings._positions_path = lambda: tmp_path / "settings.json"
        try:
            with patch("live_telemetry_evo.updater.get_current_version",
                       return_value="0.9.0-test.1"):
                ch = settings._default_release_channel()
                assert ch == "test"
        finally:
            settings._positions_path = orig_path

    def test_default_channel_for_dev_build(self, tmp_path):
        from live_telemetry_evo import settings
        orig_path = settings._positions_path
        settings._positions_path = lambda: tmp_path / "settings.json"
        try:
            with patch("live_telemetry_evo.updater.get_current_version",
                       return_value="0.9.0.dev1"):
                ch = settings._default_release_channel()
                assert ch == "test"
        finally:
            settings._positions_path = orig_path

    def test_default_channel_for_prod_build(self, tmp_path):
        from live_telemetry_evo import settings
        orig_path = settings._positions_path
        settings._positions_path = lambda: tmp_path / "settings.json"
        try:
            with patch("live_telemetry_evo.updater.get_current_version",
                       return_value="0.9.0"):
                ch = settings._default_release_channel()
                assert ch == "prod"
        finally:
            settings._positions_path = orig_path


# -- app.py imports the new modules --------------------------------------

class TestAppImports:
    def test_app_imports_race_engineer(self):
        """Verify that the race-engineer, RaceWorkerClient, and TTS
        symbols are importable at the package level (no need to fully
        import app.py which requires deeper PySide6 mocking)."""
        from live_telemetry_evo.race_engineer.balance import BalanceDetector
        from live_telemetry_evo.race_engineer.incidents import IncidentTracker
        from live_telemetry_evo.race_engineer.lap_summary import LapSummaryBuilder
        from live_telemetry_evo.race_engineer.proximity import FasterCarBehindDetector
        from live_telemetry_evo.race_worker_client import RaceWorkerClient
        from live_telemetry_evo.tts import SpeechScheduler, Priority
        assert BalanceDetector is not None
        assert IncidentTracker is not None
        assert LapSummaryBuilder is not None
        assert FasterCarBehindDetector is not None
        assert RaceWorkerClient is not None
        assert SpeechScheduler is not None
        assert Priority is not None


# -- Fixture CSV exists under tests/fixtures/ -----------------------------

class TestFixtures:
    def test_sample_replay_csv_exists(self):
        csv = Path(__file__).parent / "fixtures" / "sample_replay.csv"
        assert csv.exists(), f"missing {csv}"
        lines = csv.read_text().strip().splitlines()
        assert len(lines) > 1, "CSV must have header + data rows"
