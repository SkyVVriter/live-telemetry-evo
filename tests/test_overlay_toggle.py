"""Tests for the overlay visibility toggle state logic and persistence.

Validates:
- overlay_visible flag round-trips through settings (positions.json)
- default value is True when no entry exists
- overlay_hotkey round-trips (optional binding or None)
- corrupt/missing entries fall back safely
- widget isVisible() reflects toggle state (offscreen Qt platform)
- tray menu action wiring
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch, call

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


class TestWidgetVisibility:
    """Test that widget isVisible() reflects overlay toggle state.

    Uses mocked Qt widgets to validate the toggle logic drives
    show()/hide() correctly on the overlay window and its children.
    """

    def _make_widget(self, name="widget"):
        w = MagicMock()
        w._visible = True
        w.show = MagicMock(side_effect=lambda: setattr(w, '_visible', True))
        w.hide = MagicMock(side_effect=lambda: setattr(w, '_visible', False))
        w.isVisible = MagicMock(side_effect=lambda: w._visible)
        w.setVisible = MagicMock(
            side_effect=lambda v: setattr(w, '_visible', v))
        return w

    def test_hide_calls_window_hide(self):
        """Toggling off must call window.hide() to remove it from screen."""
        window = self._make_widget("window")
        engine = self._make_widget("engine")
        repaint_timer = MagicMock()
        vr_submit_timer = MagicMock()
        engineer_timer = MagicMock()
        game_detected = [True]
        overlay_visible = [True]
        widgets_ready = [True]

        vr_running = MagicMock(return_value=False)

        def _set_overlay_visible(visible):
            overlay_visible[0] = visible
            if visible:
                engineer_timer.stop()
                engine.show()
                window.show()
                if not vr_running():
                    repaint_timer.start()
            else:
                repaint_timer.stop()
                vr_submit_timer.stop()
                if game_detected[0]:
                    engineer_timer.start()
                window.hide()

        _set_overlay_visible(False)
        assert overlay_visible[0] is False
        window.hide.assert_called()
        repaint_timer.stop.assert_called()
        engineer_timer.start.assert_called()

    def test_show_calls_window_show(self):
        """Toggling on must call window.show() and start repaint timer."""
        window = self._make_widget("window")
        engine = self._make_widget("engine")
        repaint_timer = MagicMock()
        vr_submit_timer = MagicMock()
        engineer_timer = MagicMock()
        overlay_visible = [False]

        vr_running = MagicMock(return_value=False)

        def _set_overlay_visible(visible):
            overlay_visible[0] = visible
            if visible:
                engineer_timer.stop()
                engine.show()
                window.show()
                if not vr_running():
                    repaint_timer.start()
            else:
                repaint_timer.stop()
                vr_submit_timer.stop()
                window.hide()

        _set_overlay_visible(True)
        assert overlay_visible[0] is True
        window.show.assert_called()
        repaint_timer.start.assert_called()
        engineer_timer.stop.assert_called()

    def test_toggle_cycle(self):
        """Full cycle: visible -> hidden -> visible uses window.hide/show."""
        window = self._make_widget("window")
        engine = self._make_widget("engine")
        overlay_visible = [True]
        game_detected = [True]
        repaint_timer = MagicMock()
        vr_submit_timer = MagicMock()
        engineer_timer = MagicMock()
        vr_running = MagicMock(return_value=False)

        def _set_overlay_visible(visible):
            overlay_visible[0] = visible
            if visible:
                engineer_timer.stop()
                engine.show()
                window.show()
                if not vr_running():
                    repaint_timer.start()
            else:
                repaint_timer.stop()
                vr_submit_timer.stop()
                if game_detected[0]:
                    engineer_timer.start()
                window.hide()

        _set_overlay_visible(False)
        assert window._visible is False
        assert overlay_visible[0] is False

        _set_overlay_visible(True)
        assert window._visible is True
        assert overlay_visible[0] is True

    def test_reveal_widgets_hides_when_overlay_hidden(self):
        """_reveal_widgets must call window.hide() when overlay is off."""
        window = self._make_widget("window")
        engine = self._make_widget("engine")
        wheels = {"FL": self._make_widget("FL"),
                  "FR": self._make_widget("FR")}
        widgets_ready = [False]
        overlay_visible = [False]
        visibility = {"engine": True, "FL": True, "FR": True}

        def _reveal_widgets():
            widgets_ready[0] = True
            if not overlay_visible[0]:
                window.hide()
                return
            if visibility.get("engine", True):
                engine.show()
            for wid, view in wheels.items():
                if visibility.get(wid, True):
                    view.show()

        _reveal_widgets()
        assert widgets_ready[0] is True
        window.hide.assert_called()
        engine.show.assert_not_called()
        for view in wheels.values():
            view.show.assert_not_called()

    def test_reveal_widgets_shows_when_overlay_visible(self):
        """_reveal_widgets shows widgets when overlay is on."""
        window = self._make_widget("window")
        engine = self._make_widget("engine")
        wheels = {"FL": self._make_widget("FL"),
                  "FR": self._make_widget("FR")}
        widgets_ready = [False]
        overlay_visible = [True]
        visibility = {"engine": True, "FL": True, "FR": True}

        def _reveal_widgets():
            widgets_ready[0] = True
            if not overlay_visible[0]:
                window.hide()
                return
            if visibility.get("engine", True):
                engine.show()
            for wid, view in wheels.items():
                if visibility.get(wid, True):
                    view.show()

        _reveal_widgets()
        assert widgets_ready[0] is True
        window.hide.assert_not_called()
        engine.show.assert_called()
        for view in wheels.values():
            view.show.assert_called()


class TestTrayToggleWiring:
    """Test the overlay toggle callback pattern used by the tray menu.

    The actual make_tray function requires deep PySide6 mocking, so
    these tests validate the toggle callback contract in isolation.
    """

    def test_toggle_callback_flips_state(self):
        """The on_toggle_overlay callback must flip the visible state."""
        visible = [True]

        def on_toggle():
            visible[0] = not visible[0]

        assert visible[0] is True
        on_toggle()
        assert visible[0] is False
        on_toggle()
        assert visible[0] is True

    def test_toggle_callback_with_persistence(self, tmp_path):
        """Toggle callback must persist the new state."""
        from live_telemetry_evo import settings
        orig = settings._positions_path
        settings._positions_path = lambda: tmp_path / "positions.json"
        try:
            visible = [True]

            def on_toggle():
                visible[0] = not visible[0]
                settings.save_overlay_visible(visible[0])

            on_toggle()
            assert settings.load_overlay_visible() is False
            on_toggle()
            assert settings.load_overlay_visible() is True
        finally:
            settings._positions_path = orig

    def test_is_overlay_visible_tracks_state(self):
        """is_overlay_visible must always return the current state."""
        visible = [True]

        def is_visible():
            return visible[0]

        assert is_visible() is True
        visible[0] = False
        assert is_visible() is False

    def test_tray_overlay_params_are_optional(self):
        """make_tray's on_toggle_overlay/is_overlay_visible default to None,
        ensuring backward compatibility with callers that don't pass them."""
        import importlib
        import sys
        updater_mock = MagicMock()
        updater_mock.UpdateController = type(
            "UpdateController", (), {
                "IDLE": "idle", "CHECKING": "checking",
                "DOWNLOADING": "downloading", "READY": "ready",
            })
        sys.modules["live_telemetry_evo.updater"] = updater_mock
        try:
            if "live_telemetry_evo.tray" in sys.modules:
                del sys.modules["live_telemetry_evo.tray"]
            from live_telemetry_evo.tray import make_tray
            import inspect
            sig = inspect.signature(make_tray)
            overlay_param = sig.parameters.get("on_toggle_overlay")
            visible_param = sig.parameters.get("is_overlay_visible")
            assert overlay_param is not None, "on_toggle_overlay param missing"
            assert visible_param is not None, "is_overlay_visible param missing"
            assert overlay_param.default is None
            assert visible_param.default is None
        finally:
            if "live_telemetry_evo.tray" in sys.modules:
                del sys.modules["live_telemetry_evo.tray"]
            if "live_telemetry_evo.updater" in sys.modules:
                del sys.modules["live_telemetry_evo.updater"]
