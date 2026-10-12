"""Tests for the overlay visibility toggle state logic and persistence.

Validates:
- overlay_visible flag round-trips through settings (positions.json)
- default value is True when no entry exists
- overlay_hotkey round-trips (optional binding or None)
- corrupt/missing entries fall back safely
"""
import json
from pathlib import Path

import pytest


class TestOverlayVisiblePersistence:
    def _patch_path(self, settings_mod, tmp_path):
        orig = settings_mod._positions_path
        settings_mod._positions_path = lambda: tmp_path / "positions.json"
        return orig

    def test_default_is_true(self, tmp_path):
        from live_telemetry_evo import settings
        orig = self._patch_path(settings, tmp_path)
        try:
            assert settings.load_overlay_visible() is True
        finally:
            settings._positions_path = orig

    def test_save_and_load_false(self, tmp_path):
        from live_telemetry_evo import settings
        orig = self._patch_path(settings, tmp_path)
        try:
            settings.save_overlay_visible(False)
            assert settings.load_overlay_visible() is False
        finally:
            settings._positions_path = orig

    def test_save_and_load_true(self, tmp_path):
        from live_telemetry_evo import settings
        orig = self._patch_path(settings, tmp_path)
        try:
            settings.save_overlay_visible(False)
            settings.save_overlay_visible(True)
            assert settings.load_overlay_visible() is True
        finally:
            settings._positions_path = orig

    def test_round_trip_preserves_other_keys(self, tmp_path):
        from live_telemetry_evo import settings
        orig = self._patch_path(settings, tmp_path)
        try:
            settings.save_size_index(3)
            settings.save_overlay_visible(False)
            assert settings.load_size_index(2, 5) == 3
            assert settings.load_overlay_visible() is False
        finally:
            settings._positions_path = orig

    def test_corrupt_value_returns_default(self, tmp_path):
        from live_telemetry_evo import settings
        orig = self._patch_path(settings, tmp_path)
        try:
            path = tmp_path / "positions.json"
            path.write_text(json.dumps({"overlay_visible": "yes"}),
                            encoding="utf-8")
            assert settings.load_overlay_visible() is True
        finally:
            settings._positions_path = orig

    def test_missing_file_returns_default(self, tmp_path):
        from live_telemetry_evo import settings
        orig = self._patch_path(settings, tmp_path)
        try:
            assert settings.load_overlay_visible(default=True) is True
        finally:
            settings._positions_path = orig


class TestOverlayHotkeyPersistence:
    def _patch_path(self, settings_mod, tmp_path):
        orig = settings_mod._positions_path
        settings_mod._positions_path = lambda: tmp_path / "positions.json"
        return orig

    def test_default_is_none(self, tmp_path):
        from live_telemetry_evo import settings
        orig = self._patch_path(settings, tmp_path)
        try:
            assert settings.load_overlay_hotkey() is None
        finally:
            settings._positions_path = orig

    def test_save_and_load_keyboard_binding(self, tmp_path):
        from live_telemetry_evo import settings
        orig = self._patch_path(settings, tmp_path)
        try:
            binding = {"type": "keyboard", "key_name": "f12",
                       "joy_id": -1, "button_id": -1}
            settings.save_overlay_hotkey(binding)
            loaded = settings.load_overlay_hotkey()
            assert loaded == binding
        finally:
            settings._positions_path = orig

    def test_save_and_load_joystick_binding(self, tmp_path):
        from live_telemetry_evo import settings
        orig = self._patch_path(settings, tmp_path)
        try:
            binding = {"type": "joystick", "key_name": "",
                       "joy_id": 0, "button_id": 5}
            settings.save_overlay_hotkey(binding)
            loaded = settings.load_overlay_hotkey()
            assert loaded == binding
        finally:
            settings._positions_path = orig

    def test_save_none_clears(self, tmp_path):
        from live_telemetry_evo import settings
        orig = self._patch_path(settings, tmp_path)
        try:
            settings.save_overlay_hotkey({"type": "keyboard", "key_name": "f9"})
            settings.save_overlay_hotkey(None)
            assert settings.load_overlay_hotkey() is None
        finally:
            settings._positions_path = orig

    def test_corrupt_value_returns_none(self, tmp_path):
        from live_telemetry_evo import settings
        orig = self._patch_path(settings, tmp_path)
        try:
            path = tmp_path / "positions.json"
            path.write_text(json.dumps({"overlay_hotkey": 42}),
                            encoding="utf-8")
            assert settings.load_overlay_hotkey() is None
        finally:
            settings._positions_path = orig

    def test_preserves_overlay_visible(self, tmp_path):
        from live_telemetry_evo import settings
        orig = self._patch_path(settings, tmp_path)
        try:
            settings.save_overlay_visible(False)
            settings.save_overlay_hotkey({"type": "keyboard", "key_name": "f10"})
            assert settings.load_overlay_visible() is False
            assert settings.load_overlay_hotkey()["key_name"] == "f10"
        finally:
            settings._positions_path = orig


class TestToggleStateLogic:
    """Test the logical toggle pattern used by _set_overlay_visible."""

    def test_toggle_flips_state(self):
        state = [True]
        def toggle():
            state[0] = not state[0]
        toggle()
        assert state[0] is False
        toggle()
        assert state[0] is True

    def test_toggle_persistence_round_trip(self, tmp_path):
        from live_telemetry_evo import settings
        orig = settings._positions_path
        settings._positions_path = lambda: tmp_path / "positions.json"
        try:
            state = [settings.load_overlay_visible()]
            assert state[0] is True

            state[0] = not state[0]
            settings.save_overlay_visible(state[0])
            assert settings.load_overlay_visible() is False

            state[0] = not state[0]
            settings.save_overlay_visible(state[0])
            assert settings.load_overlay_visible() is True
        finally:
            settings._positions_path = orig
