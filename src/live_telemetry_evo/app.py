from __future__ import annotations

import argparse
import signal
import sys
import time
from typing import Callable, Sequence

from PySide6.QtCore import QThread, QTimer, QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from .frame_bus import FrameBus
from .layout import ScreenLayout, compute_layout, pick_resolution
from .logbook import log, log_action
from .logger import CsvLogger
from .race_engineer.balance import BalanceDetector
from .race_engineer.corner_map import TrackProfile
from .race_engineer.incidents import IncidentTracker, IncidentSeverity
from .race_engineer.lap_summary import LapSummaryBuilder
from .race_engineer.proximity import FasterCarBehindDetector
from .race_worker_client import (
    RaceWorkerClient,
    SessionSync,
    session_payload_from_iracing,
)
from .settings import (delete_entries, load_overlay_visible, load_polling_hz,
                        load_positions,
                        load_race_worker_settings, load_release_channel,
                        load_size_index, load_visibility, load_vr_distance,
                        load_vr_placement, load_vr_spread,
                        save_overlay_visible, save_polling_hz,
                        save_position, save_size_index, save_visibility,
                        save_vr_distance, save_vr_placement, save_vr_spread)
from .sources import make_source
from .telemetry import TelemetryFrame
from .tray import make_tray
from .tts import SpeechScheduler, get_engine, Priority as TTSPriority
from .tts.settings_dialog import load_tts_settings
from .ptt.ptt_controller import PTTController, PTTMode
from .ptt.recorder import AudioRecorder
from .ptt.stt import get_stt_engine
from .ptt.intents import IntentHandler, DriverContext
from .updater import UpdateController
from .vr import detect as vr_detect
from .vr.overlay_output import VROverlayOutput
from .widgets.countdown import CountdownView
from .widgets.detection import DetectionView
from .widgets.engine_view import EngineView
from .widgets.inputs_view import InputsView
from .widgets.wheel_view import WheelView
from .window import (HOTKEY_CLICK_LABEL, HOTKEY_LOG_LABEL, HOTKEY_QUIT_LABEL,
                     HOTKEY_RESET_LABEL, HOTKEY_SIZE_LABEL,
                     HOTKEY_VR_DISTANCE_LABEL, HOTKEY_VR_PLACEMENT_LABEL,
                     HOTKEY_VR_SPREAD_LABEL, OverlayWindow)


# Polling rates exposed in the tray submenu. The source's QTimer runs at
# the chosen Hz on its dedicated worker thread; UI repaint is independent
# (display refresh rate). 60 is the default — matches AC's physics step
# and keeps the EMA-smoothed derivatives (e.g. kers_deploy_kw) tight.
POLLING_HZ_OPTIONS: tuple[int, ...] = (30, 60, 100, 120, 144, 250)
DEFAULT_POLLING_HZ = 60


_RESETTABLE_IDS = ("engine", "inputs", "FL", "FR", "RL", "RR")

# (widget key, human label) for the per-widget show/hide toggles offered in
# the tray's Windows and VR menus. Order sets the menu order — alphabetical by
# label. Keys match the visibility-persistence ids and the ``all_views`` map.
WIDGET_TOGGLES: tuple[tuple[str, str], ...] = (
    ("engine", "Engine"),
    ("FL", "Front Left"),
    ("FR", "Front Right"),
    ("inputs", "Inputs"),
    ("RL", "Rear Left"),
    ("RR", "Rear Right"),
)

# Scale factors applied on top of the auto-detected resolution multiplier.
# Index 2 ("M") is 1.0 — i.e. matches the original auto-picked size.
SIZE_FACTORS: tuple[float, ...] = (0.5, 0.75, 1.0, 1.25, 1.5)
SIZE_LABELS: tuple[str, ...] = ("XS", "S", "M", "L", "XL")
DEFAULT_SIZE_INDEX = 2


# VR panel geometry exposed in the tray. Spread is the horizontal fan-out
# factor (1.0 == exact desktop layout); distance is how far the panel
# floats from the viewer, in metres (the cylinder radius). Defaults mirror
# HORIZONTAL_SPREAD / CYLINDER_RADIUS_M in vr.overlay_output.
VR_SPREAD_OPTIONS: tuple[float, ...] = (
    0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2)
DEFAULT_VR_SPREAD = 0.8
VR_DISTANCE_OPTIONS: tuple[float, ...] = (1.0, 1.2, 1.4, 1.6, 1.8, 2.0)
DEFAULT_VR_DISTANCE = 1.4

# Placement modes the VR placement hotkey cycles through, in order. Keys
# match the persisted values driving the tray placement submenu ("dash" is
# the fixed-in-place mode, kept for settings backward-compatibility).
VR_PLACEMENT_MODES: tuple[str, ...] = ("head", "dash")


def _next_option(options: Sequence[float], current: float) -> float:
    """Return the option after ``current`` (wrapping to the first), matched
    with a float tolerance. Falls back to the first option when ``current``
    is not in the list — e.g. a persisted value that is no longer offered."""
    for i, opt in enumerate(options):
        if abs(opt - current) < 1e-6:
            return options[(i + 1) % len(options)]
    return options[0]


# Anchor corner per widget — kept stable across size cycles so a widget
# the user dragged into a particular corner stays pinned to that corner
# when growing/shrinking. Going back to the original size recovers the
# exact original position (no drift from edge-clamping during cycles).
_ANCHORS: dict[str, tuple[str, str]] = {
    "engine": ("center", "bottom"),
    "inputs": ("center", "top"),
    "FL": ("left", "top"),
    "FR": ("right", "top"),
    "RL": ("left", "bottom"),
    "RR": ("right", "bottom"),
}


def _resolve_xy(saved: dict[str, tuple[int, int]],
                wid: str, default_x: int, default_y: int,
                w: int, h: int, screen_w: int, screen_h: int) -> tuple[int, int]:
    """Use the saved position only if the widget would land fully on-screen
    at the current geometry; otherwise fall back to the layout default."""
    if wid in saved:
        x, y = saved[wid]
        if 0 <= x and x + w <= screen_w and 0 <= y and y + h <= screen_h:
            return x, y
    return default_x, default_y


def _default_pos(wid: str, layout: ScreenLayout) -> tuple[int, int, int, int]:
    """Return (x, y, w, h) for the layout default of a given widget id."""
    if wid == "engine":
        p = layout.engine
    elif wid == "inputs":
        p = layout.inputs
    else:
        p = layout.wheels[wid]
    return p.x, p.y, p.w, p.h


def _apply_layout(
    window: OverlayWindow,
    engine: EngineView,
    inputs: InputsView,
    wheels: dict[str, WheelView],
    layout: ScreenLayout,
) -> None:
    """Stretch the overlay across the screen and place widgets at corners.

    Saved positions are honoured when they fit on the current screen;
    anything off-screen (e.g. resolution change since last run) reverts
    to the computed default for that screen. Visibility flags persist
    too — closing a widget hides it across sessions until reset.
    """
    saved = load_positions()
    visibility = load_visibility()
    window.setGeometry(0, 0, layout.screen_w, layout.screen_h)

    def _place(wid: str, view, default_x: int, default_y: int,
               w: int, h: int) -> None:
        view.setParent(window)
        x, y = _resolve_xy(saved, wid, default_x, default_y, w, h,
                           layout.screen_w, layout.screen_h)
        view.setGeometry(x, y, w, h)

    _place("engine", engine, *_default_pos("engine", layout))
    engine.moved_to.connect(lambda x, y: (
        save_position("engine", x, y),
        log_action(f"widget engine moved to ({x}, {y})")))
    engine.closed.connect(lambda: (
        engine.hide(), save_visibility("engine", False),
        log_action("widget engine closed")))
    if visibility.get("engine", True):
        engine.show()
    else:
        engine.hide()

    _place("inputs", inputs, *_default_pos("inputs", layout))
    inputs.moved_to.connect(lambda x, y: (
        save_position("inputs", x, y),
        log_action(f"widget inputs moved to ({x}, {y})")))
    inputs.closed.connect(lambda: (
        inputs.hide(), save_visibility("inputs", False),
        log_action("widget inputs closed")))
    # Phase-3 widget hidden by default for now — Ctrl+Shift+R / tray Reset
    # brings it back when the user wants to see it.
    inputs.hide()

    for wid, view in wheels.items():
        _place(wid, view, *_default_pos(wid, layout))
        # Default-arg trick binds the loop variable into each lambda;
        # otherwise all four would close over the last value of `wid`.
        view.moved_to.connect(lambda x, y, k=wid: (
            save_position(k, x, y),
            log_action(f"widget {k} moved to ({x}, {y})")))
        view.closed.connect(lambda v=view, k=wid: (
            v.hide(), save_visibility(k, False),
            log_action(f"widget {k} closed")))
        if visibility.get(wid, True):
            view.show()
        else:
            view.hide()


def _reset_layout(engine: EngineView, inputs: InputsView,
                  wheels: dict[str, WheelView],
                  layout: ScreenLayout) -> None:
    """Restore every overlay widget to its default position and shown
    state, and wipe persisted entries for them. Inputs is the exception —
    it's hidden by default (Phase-3 widget), so reset restores its default
    position but leaves it hidden; the tray "Widgets" toggle brings it up."""
    delete_entries(list(_RESETTABLE_IDS))
    engine.setGeometry(*_default_pos("engine", layout))
    engine.show()
    inputs.setGeometry(*_default_pos("inputs", layout))
    inputs.hide()
    for wid, view in wheels.items():
        view.setGeometry(*_default_pos(wid, layout))
        view.show()


def _anchor_resize(view, wid: str, new_w: int, new_h: int,
                   screen_w: int, screen_h: int) -> None:
    """Resize a widget to (``new_w``, ``new_h``) while pinning the corner
    declared in ``_ANCHORS`` for ``wid``. A size cycle is round-trippable:
    going M → L → M lands the widget back where it started."""
    ax_kind, ay_kind = _ANCHORS[wid]
    old_x, old_y = view.x(), view.y()
    old_w, old_h = view.width(), view.height()

    if ax_kind == "left":
        new_x = old_x
    elif ax_kind == "right":
        new_x = old_x + old_w - new_w
    else:  # center
        new_x = int(old_x + old_w / 2 - new_w / 2)

    if ay_kind == "top":
        new_y = old_y
    elif ay_kind == "bottom":
        new_y = old_y + old_h - new_h
    else:
        new_y = int(old_y + old_h / 2 - new_h / 2)

    new_x = max(0, min(screen_w - new_w, int(new_x)))
    new_y = max(0, min(screen_h - new_h, int(new_y)))
    view.setGeometry(new_x, new_y, new_w, new_h)


def _resize_widgets(engine: EngineView, inputs: InputsView,
                    wheels: dict[str, WheelView],
                    layout: ScreenLayout) -> None:
    """Re-apply layout-computed dimensions for engine + inputs + wheels."""
    _anchor_resize(engine, "engine", layout.engine.w, layout.engine.h,
                   layout.screen_w, layout.screen_h)
    _anchor_resize(inputs, "inputs", layout.inputs.w, layout.inputs.h,
                   layout.screen_w, layout.screen_h)
    for wid, view in wheels.items():
        place = layout.wheels[wid]
        _anchor_resize(view, wid, place.w, place.h,
                       layout.screen_w, layout.screen_h)


def _dispatch_frame(frame: TelemetryFrame, engine: EngineView,
                    inputs: InputsView, wheels: dict[str, WheelView]) -> None:
    """Push the latest frame to every widget. Called from the UI-side
    repaint timer (display refresh rate), not from the polling thread —
    so widget paint events stay decoupled from SHM read latency."""
    engine.set_data(frame.engine)
    inputs.set_data(frame.inputs)
    for wid, view in wheels.items():
        view.set_data(frame.wheels[wid])


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="live-telemetry-evo",
        description="Assetto Corsa telemetry overlay (AC1 / ACC / AC Evo / AC Rally)",
    )
    parser.add_argument(
        "--source",
        choices=("auto", "synthetic", "ac-evo", "ac1", "acc", "acrally",
                 "iracing", "iracing-replay"),
        default="auto",
        help=("telemetry source: 'auto' (default, detect the running game), "
              "'ac-evo' (Assetto Corsa Evo), 'ac1' (original Assetto Corsa), "
              "'acc' (Assetto Corsa Competizione), 'acrally' (Assetto Corsa "
              "Rally), 'iracing' (iRacing via pyirsdk), "
              "'iracing-replay' (replay from Garage 61 CSV), "
              "or 'synthetic' (mock data)"),
    )
    parser.add_argument("--replay-csv", type=str, default="",
                        help="path to Garage 61 CSV for iracing-replay source")
    parser.add_argument("--hz", type=int, default=0,
                        help=("polling rate in Hz; 0 = use the value persisted in "
                              "settings (default 60). Allowed live values: "
                              "30/60/100/120/144/250."))
    parser.add_argument(
        "--open-dialog",
        type=str, default="",
        help=("open a settings dialog on startup for smoke-test "
              "screenshots, e.g. --open-dialog race_worker or "
              "--open-dialog tts. Accepts a pipe-separated list."))
    parser.add_argument(
        "--overlay-hidden", action="store_true",
        help=("start with the overlay hidden (widgets not rendered). "
              "Useful for smoke tests. The tray 'Оверлей' toggle or "
              "configured hotkey brings it back."))
    parser.add_argument(
        "--self-test", action="store_true",
        help="run backend diagnostics and exit with code 0 if all OK")
    vr_group = parser.add_mutually_exclusive_group()
    vr_group.add_argument(
        "--vr", action="store_true",
        help=("force the VR overlay on: submit the HUD to SteamVR as an "
              "overlay quad. Falls back to the desktop overlay if SteamVR / "
              "the OpenVR runtime isn't available."))
    vr_group.add_argument(
        "--novr", action="store_true",
        help=("force the VR overlay off, disabling SteamVR auto-detection "
              "(pure desktop overlay). Default is to auto-detect SteamVR and "
              "enable VR when a headset is present."))
    return parser.parse_args(argv)


def _run_self_test() -> int:
    """Run backend diagnostics and return 0 if all critical backends work."""
    import ctypes
    from .logbook import log as _log

    results = {}

    def _p(msg):
        print(msg)
        _log(f"[self-test] {msg}")

    _p("=== LiveTelemetryEvo Self-Test ===")

    # --- FATAL checks (hardware-independent; must pass on any runner) ---
    # Only "device count > 0" is allowed to be non-fatal on CI.

    # DIJOYSTATE2 data format (ctypes, no dinput8.dll export)
    try:
        from .ptt.dinput_format import (
            DIJOYSTATE2_SIZE, _DF_JOYSTICK2_NUM_OBJS, _DIJOYSTATE2,
            build_dijoystate2_data_format,
        )
        built = build_dijoystate2_data_format()
        js_sz = ctypes.sizeof(_DIJOYSTATE2)
        ok_fmt = bool(
            js_sz == DIJOYSTATE2_SIZE
            and built.data_size == DIJOYSTATE2_SIZE
            and built.num_objs == _DF_JOYSTICK2_NUM_OBJS
            and built.fmt.rgodf
        )
        _p(f"dinput format: sizeof(DIJOYSTATE2)={js_sz} "
           f"dwNumObjs={built.num_objs} dwDataSize={built.data_size}")
        results["dinput_format"] = ok_fmt
        if not ok_fmt:
            _p("dinput format: FAIL (size/object-count mismatch)")
    except Exception as e:
        _p(f"dinput format: FAIL ({e})")
        results["dinput_format"] = False

    # TTS engines registered
    try:
        from .tts import list_engines
        engines = list_engines()
        _p(f"TTS engines: {engines}")
        results["tts_registered"] = "edge-tts" in engines and "elevenlabs" in engines
    except Exception as e:
        _p(f"TTS engines: FAIL ({e})")
        results["tts_registered"] = False

    # pynput keyboard importable
    try:
        from pynput import keyboard  # type: ignore
        _p(f"pynput: OK ({keyboard.__name__})")
        results["pynput"] = True
    except ImportError as e:
        _p(f"pynput: FAIL ({e})")
        results["pynput"] = False

    # pygame importable (bundled in the frozen exe)
    try:
        import pygame  # type: ignore
        _p(f"pygame: OK ({getattr(pygame, '__version__', '?')})")
        results["pygame"] = True
    except ImportError as e:
        if sys.platform == "win32":
            _p(f"pygame: FAIL ({e})")
            results["pygame"] = False
        else:
            _p(f"pygame: SKIP ({e})")
            results["pygame"] = True

    # sounddevice + PortAudio loadable
    try:
        import sounddevice as sd  # type: ignore
        _p(f"sounddevice: OK (PortAudio {sd.get_portaudio_version()[1]})")
        results["sounddevice"] = True
    except (ImportError, OSError) as e:
        _p(f"sounddevice: FAIL ({e})")
        results["sounddevice"] = False

    if sys.platform == "win32":
        # winmm struct sizes
        from .ptt.input_binding import _JOYCAPSW, _JOYINFOEX
        caps_sz = ctypes.sizeof(_JOYCAPSW)
        info_sz = ctypes.sizeof(_JOYINFOEX)
        _p(f"winmm: sizeof(JOYCAPSW)={caps_sz} (expect 728), "
           f"sizeof(JOYINFOEX)={info_sz} (expect 52)")
        results["winmm_structs"] = (caps_sz == 728 and info_sz == 52)

        # winmm loads and joyGetNumDevs callable
        try:
            from .ptt.input_binding import _get_winmm
            funcs = _get_winmm()
            if funcs is None:
                _p("winmm: FAIL (dll not loaded)")
                results["winmm_load"] = False
            else:
                n = funcs[0]()
                _p(f"winmm: loaded, joyGetNumDevs={n}")
                results["winmm_load"] = True
        except Exception as e:
            _p(f"winmm: FAIL ({e})")
            results["winmm_load"] = False

        # DirectInput8Create succeeds + hidden HWND
        try:
            from .ptt.dinput import DirectInput8
            di = DirectInput8()
            ok = di.init(hwnd=0)
            if ok:
                devs = di.enumerate()
                hwnd_ok = bool(di._hwnd)
                _p(f"DirectInput8: OK hwnd={int(di._hwnd.value or 0)} "
                   f"{len(devs)} device(s)")
                results["dinput_init"] = hwnd_ok
            else:
                _p("DirectInput8: FAIL (init returned False)")
                results["dinput_init"] = False
        except Exception as e:
            _p(f"DirectInput8: FAIL ({e})")
            results["dinput_init"] = False

    else:
        results["winmm_structs"] = True
        results["winmm_load"] = True
        results["dinput_init"] = True

    # GitHub API reachable
    try:
        from .updater import _GITHUB_HEADERS, GITHUB_API_RELEASES_URL
        import urllib.request, ssl, json as _json
        url = f"{GITHUB_API_RELEASES_URL}?per_page=5"
        req = urllib.request.Request(url, headers=dict(_GITHUB_HEADERS))
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        n = len(data) if isinstance(data, list) else 1
        ua = _GITHUB_HEADERS.get("User-Agent", "")
        _p(f"GitHub API: OK ({n} releases, UA={ua})")
        results["updater"] = n > 0 and "application/vnd.github+json" in (
            _GITHUB_HEADERS.get("Accept") or "")
    except Exception as e:
        _p(f"GitHub API: FAIL ({e})")
        results["updater"] = False

    # Token sanitization (always) + 401 from a local auth stub
    try:
        from .race_worker_client import RaceWorkerClient, _sanitize_token
        assert _sanitize_token("tok.") == "tok"
        assert _sanitize_token("  tok  ") == "tok"
        _p("race-worker: token sanitization OK")
        results["rw_sanitize"] = True
    except Exception as e:
        _p(f"race-worker sanitization: FAIL ({e})")
        results["rw_sanitize"] = False

    try:
        results["rw_auth_401"] = _self_test_race_worker_401(_p)
    except Exception as e:
        _p(f"race-worker auth 401: FAIL ({e})")
        results["rw_auth_401"] = False

    # --- Report ---
    all_ok = all(results.values())
    _p(f"Results: {results}")
    _p(f"Overall: {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 1


def _self_test_race_worker_401(_p) -> bool:
    """Spin a tiny local server that 401s a bad token; client must report it."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from .race_worker_client import RaceWorkerClient

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            auth = self.headers.get("Authorization", "")
            if auth != "Bearer good-token":
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"detail":"Invalid token"}')
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"[]")

        def log_message(self, fmt, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    started = threading.Event()

    def _serve():
        started.set()
        server.serve_forever()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    started.wait(2)
    try:
        bad = RaceWorkerClient(base_url=f"http://127.0.0.1:{port}",
                               token="bad-token.")
        ok, msg = bad.test_connection()
        if ok or "401" not in msg:
            _p(f"race-worker auth 401: FAIL (ok={ok} msg={msg!r})")
            return False
        _p(f"race-worker auth 401: OK ({msg})")
        return True
    finally:
        server.shutdown()
        server.server_close()


def run(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        import os, threading
        def _self_test_watchdog():
            import time
            time.sleep(20)
            print("[self-test] watchdog: forcing exit after 20s")
            os._exit(2)
        threading.Thread(target=_self_test_watchdog, daemon=True).start()
        rc = _run_self_test()
        os._exit(rc)

    app = QApplication(sys.argv)
    # Config + logs live next to the executable (or in CWD during dev)
    # — see ``overlay.paths``. These identifiers no longer affect file
    # locations; they're kept so any QStandardPaths-aware Qt component
    # (taskbar grouping, native dialogs) has a sensible app identity.
    app.setOrganizationName("LiveTelemetryEvo")
    app.setApplicationName("Overlay")
    app.setQuitOnLastWindowClosed(False)

    # Marker so the appended action log separates one run from the next.
    log_action("session started")
    # pylint: disable-next=no-member
    app.aboutToQuit.connect(lambda: log_action("session ended"))

    screen = app.primaryScreen()
    geom = screen.availableGeometry()

    window = OverlayWindow()

    engine = EngineView()
    inputs = InputsView()
    wheels = {wid: WheelView(wid) for wid in ("FL", "FR", "RL", "RR")}

    # ``size_idx`` and ``layout`` are mutated by the size-cycle handler
    # below; closures here read the current value via ``nonlocal``.
    size_idx = load_size_index(DEFAULT_SIZE_INDEX, len(SIZE_FACTORS))
    base_mult = pick_resolution(geom.height())[1]
    actual_mult = base_mult * SIZE_FACTORS[size_idx]
    layout = compute_layout(geom.width(), geom.height(), multiplier=actual_mult)
    _apply_layout(window, engine, inputs, wheels, layout)
    window.move(geom.x(), geom.y())

    def _set_size(idx: int) -> None:
        nonlocal size_idx, layout
        idx = max(0, min(len(SIZE_FACTORS) - 1, int(idx)))
        size_idx = idx
        save_size_index(idx)
        new_mult = base_mult * SIZE_FACTORS[idx]
        layout = compute_layout(geom.width(), geom.height(), multiplier=new_mult)
        _resize_widgets(engine, inputs, wheels, layout)
        log_action(f"size -> {SIZE_LABELS[idx]}")

    def _cycle_size() -> None:
        _set_size((size_idx + 1) % len(SIZE_FACTORS))

    def _do_reset() -> None:
        _reset_layout(engine, inputs, wheels, layout)
        log_action("reset positions")

    # Per-widget show/hide, driven by the tray Windows/VR "Widgets" submenus.
    # One visibility flag backs both menus and the desktop and headset views:
    # a hidden widget isn't painted (so it's absent on the desktop overlay)
    # and ``_vr_submit`` skips widgets whose ``isVisible()`` is False, so it
    # drops out of the headset too. The choice persists like the × button's.
    _all_views = {"engine": engine, "inputs": inputs, **wheels}

    def _set_widget_visible(key: str, visible: bool) -> None:
        view = _all_views.get(key)
        if view is None:
            return
        view.setVisible(visible)
        save_visibility(key, visible)
        log_action(f"widget {key} {'shown' if visible else 'hidden'}")

    def _is_widget_visible(key: str) -> bool:
        view = _all_views.get(key)
        return bool(view is not None and view.isVisible())

    # --- Overlay visibility toggle ------------------------------------
    # Independent of the race-engineer / TTS / PTT pipeline: hiding the
    # overlay stops rendering but the source, bus, engineer, and speech
    # scheduler keep running so no telemetry or voice data is lost.
    overlay_visible = [False if args.overlay_hidden else load_overlay_visible()]
    if args.overlay_hidden:
        log(f"[overlay] --overlay-hidden flag active, overlay_visible={overlay_visible[0]}")

    def _set_overlay_visible(visible: bool) -> None:
        overlay_visible[0] = visible
        save_overlay_visible(visible)
        if visible:
            engineer_timer.stop()
            vis = load_visibility()
            if widgets_ready[0]:
                if vis.get("engine", True):
                    engine.show()
                if vis.get("inputs", False):
                    inputs.show()
                for wid, view in wheels.items():
                    if vis.get(wid, True):
                        view.show()
            window.show()
            if not vr.is_running():
                repaint_timer.start()
            else:
                vr_submit_timer.start()
        else:
            repaint_timer.stop()
            vr_submit_timer.stop()
            if game_detected[0]:
                engineer_timer.start()
            window.hide()
        log_action(f"overlay {'shown' if visible else 'hidden'}")

    def _toggle_overlay() -> None:
        _set_overlay_visible(not overlay_visible[0])

    def _is_overlay_visible() -> bool:
        return overlay_visible[0]

    # --- Telemetry transport: bus + repaint timer + worker thread ----
    bus = FrameBus()
    logger = CsvLogger(bus)
    # Tracks the source name passed in via auto-detect / CLI so the
    # logger can stamp the CSV filename with it. Filled in by
    # ``_start_source``.
    current_source_name = ["unknown"]

    # UI-side repaint at display refresh rate. QScreen.refreshRate()
    # returns Hz as a float (60.0, 144.0, etc.); fall back to 60 when
    # the platform doesn't report a real rate.
    refresh_hz = max(30.0, float(screen.refreshRate() or 60.0))
    repaint_timer = QTimer(window)
    repaint_timer.setInterval(int(1000 / refresh_hz))

    def _on_repaint() -> None:
        f = bus.latest()
        if f is None:
            return
        _dispatch_frame(f, engine, inputs, wheels)
        _process_iracing_frame(f)

    # pylint: disable-next=no-member  # QTimer.timeout is a PySide6 Signal
    repaint_timer.timeout.connect(_on_repaint)

    # Headless engineer timer: keeps the race-engineer / TTS pipeline
    # ticking when the overlay is hidden and the repaint timer is stopped.
    engineer_timer = QTimer(window)
    engineer_timer.setInterval(int(1000 / refresh_hz))

    def _on_engineer_tick() -> None:
        f = bus.latest()
        if f is not None:
            _process_iracing_frame(f)

    # pylint: disable-next=no-member
    engineer_timer.timeout.connect(_on_engineer_tick)
    # Started after the source goes live so we don't repaint before the
    # bus has anything; ``_start_source`` flips it on.

    # Polling Hz — persisted choice trumps the CLI when --hz is 0 (the
    # default). An explicit ``--hz N`` from the CLI overrides for this
    # session but is also persisted so the tray submenu reflects it.
    polling_hz = load_polling_hz(DEFAULT_POLLING_HZ, POLLING_HZ_OPTIONS)
    if args.hz and args.hz in POLLING_HZ_OPTIONS:
        polling_hz = args.hz
        save_polling_hz(polling_hz)

    def _set_polling_hz(hz: int) -> None:
        nonlocal polling_hz
        if hz not in POLLING_HZ_OPTIONS:
            return
        polling_hz = hz
        save_polling_hz(hz)
        log_action(f"polling {hz} Hz")
        src = getattr(window, "_source", None)
        if src is not None:
            # Queued signal → worker thread mutates its own QTimer.
            src.hz_change_requested.emit(hz)

    # Flips True once a game is detected and the source starts feeding the
    # bus (``_start_source``). Guards logging and greys out the tray "Data"
    # category until then — there is nothing to poll or log before a source
    # is live, and a CSV started early would just capture empty frames.
    game_detected = [False]

    def _stop_logging() -> None:
        """Stop CSV logging and log the saved file's absolute path so the
        terminal makes it clickable. No-op when not logging — so it's safe
        to call from both the toggle and the quit-shutdown path."""
        if not logger.is_active():
            return
        # Grab the path before stop(); it survives stop() but read it up
        # front to be safe. Absolute (logs_dir is resolved), so terminals
        # render it as a clickable link.
        path = logger.current_path()
        logger.stop()
        saved = f"; saved {path}" if path is not None else ""
        msg = f"logging stopped (dropped rows: {bus.csv_dropped}){saved}"
        log(f"[overlay] {msg}")
        log_action(msg)

    def _toggle_logging() -> None:
        # No source yet → nothing to log. The tray Data menu is greyed out
        # in this state, but the global hotkey can still fire, so guard here.
        if not game_detected[0]:
            return
        if logger.is_active():
            _stop_logging()
        else:
            path = logger.start(current_source_name[0])
            log(f"[overlay] logging started: {path}")
            log_action(f"logging started: {path}")

    def _open_logs_folder() -> None:
        log_action("open logs folder")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(CsvLogger.logs_dir())))

    # Flips True once the countdown reveals the overlay widgets — the tray
    # "Windows" category (reset / size / click-through) stays greyed out
    # until then, since there are no placed widgets to act on during
    # detection / countdown.
    widgets_ready = [False]

    # Global hotkeys (registered in window.py via Win32 RegisterHotKey).
    # Each signal shares its handler with the matching tray-menu entry, so
    # firing the hotkey and clicking the menu take exactly one path — and
    # each key is gated by the same readiness state that enables its menu
    # entry, so a hotkey never acts while its menu item is greyed out.
    # Reset / size / click-through act on the overlay widgets (Windows menu,
    # ready once the countdown reveals them); logging guards on
    # ``game_detected`` inside ``_toggle_logging`` (Data menu). Quit has no
    # gate, matching its always-enabled menu entry.
    def _when_widgets_ready(fn: Callable[[], None]) -> Callable[[], None]:
        return lambda: fn() if widgets_ready[0] else None

    # Reset / click-through are desktop-only: reset re-lays the on-screen
    # windows and click-through toggles a desktop-window flag, neither of
    # which the user can act on from inside the headset — so they're also
    # gated on VR being off, mirroring the greyed-out tray Windows category.
    # Size is the exception: it scales the HUD in VR too (bigger widgets ->
    # bigger grabbed texture -> bigger quad), so it stays available in both
    # modes and only needs the widgets-ready gate. (``vr`` is bound further
    # down but only read when a key fires, so the forward reference is fine.)
    def _when_desktop_widgets_ready(fn: Callable[[], None]) -> Callable[[], None]:
        return lambda: fn() if widgets_ready[0] and not vr.is_running() else None

    window.reset_hotkey.connect(_when_desktop_widgets_ready(_do_reset))
    window.log_hotkey.connect(_toggle_logging)
    window.size_hotkey.connect(_when_widgets_ready(_cycle_size))
    window.click_through_hotkey.connect(
        _when_desktop_widgets_ready(window.toggle_click_through)
    )
    # Release the Win32 hotkeys on every quit path. ``QApplication.quit()``
    # (tray Quit, Ctrl+Shift+Q) exits the event loop without delivering a
    # ``closeEvent``, so the window's own cleanup never runs — leaving the
    # hotkeys registered to a thread that may linger as an orphaned process
    # and blocking the next launch with ERROR_HOTKEY_ALREADY_REGISTERED (1409).
    # pylint: disable-next=no-member
    app.aboutToQuit.connect(window._unregister_hotkeys)

    # --- VR output: SteamVR overlay quad mirroring the HUD ------------
    # The overlay window's widgets are submitted to the OpenVR compositor
    # so they appear inside the headset (a desktop window is invisible in
    # VR). Started later per --vr / --novr / auto-detect; placement is
    # persisted and switchable from the tray submenu below.
    vr = VROverlayOutput(
        placement=load_vr_placement(),
        spread=load_vr_spread(DEFAULT_VR_SPREAD, VR_SPREAD_OPTIONS),
        distance=load_vr_distance(DEFAULT_VR_DISTANCE, VR_DISTANCE_OPTIONS),
    )
    # Tell the VR output each widget's LARGEST pixel size, so it can allocate
    # one fixed-size overlay texture per widget and upload smaller sizes into
    # a sub-region of it. Resizing an overlay's texture mid-session left the
    # HUD clipped at the panel edge (the compositor pins the backing surface
    # to the first texture size); a fixed texture + per-frame UV bounds keeps
    # the surface constant. The max is the layout at the biggest Size step.
    _max_layout = compute_layout(geom.width(), geom.height(),
                                 multiplier=base_mult * max(SIZE_FACTORS))
    vr.set_max_sizes({
        "engine": (_max_layout.engine.w, _max_layout.engine.h),
        "inputs": (_max_layout.inputs.w, _max_layout.inputs.h),
        **{wid: (p.w, p.h) for wid, p in _max_layout.wheels.items()},
    })

    def _set_vr_placement(mode: str) -> None:
        save_vr_placement(mode)
        vr.set_placement(mode)
        log_action(f"VR placement -> {mode}")

    def _set_vr_spread(factor: float) -> None:
        save_vr_spread(factor)
        vr.set_spread(factor)
        log_action(f"VR spread -> {round(factor * 100)}%")

    def _set_vr_distance(meters: float) -> None:
        save_vr_distance(meters)
        vr.set_distance(meters)
        log_action(f"VR distance -> {meters:.1f} m")

    # --- VR hotkey cycle handlers -------------------------------------
    # The tray VR submenus expose discrete choices; the global hotkeys step
    # forward through them (wrapping) so the whole control is reachable from
    # inside the headset, where the tray can't be clicked. Each routes back
    # through the matching _set_vr_* above, so persistence and live-apply
    # stay identical to picking the entry from the menu.
    def _cycle_vr_placement() -> None:
        cur = vr.placement
        idx = VR_PLACEMENT_MODES.index(cur) if cur in VR_PLACEMENT_MODES else 0
        _set_vr_placement(VR_PLACEMENT_MODES[(idx + 1) % len(VR_PLACEMENT_MODES)])

    def _cycle_vr_spread() -> None:
        _set_vr_spread(_next_option(VR_SPREAD_OPTIONS, vr.spread))

    def _cycle_vr_distance() -> None:
        _set_vr_distance(_next_option(VR_DISTANCE_OPTIONS, vr.distance))

    # Gate the VR hotkeys on the same state that enables the tray VR
    # category (is_vr_active == vr.is_running), so a key never acts while
    # its menu entry is greyed out on the desktop overlay.
    def _when_vr_active(fn: Callable[[], None]) -> Callable[[], None]:
        return lambda: fn() if vr.is_running() else None

    window.vr_placement_hotkey.connect(_when_vr_active(_cycle_vr_placement))
    window.vr_spread_hotkey.connect(_when_vr_active(_cycle_vr_spread))
    window.vr_distance_hotkey.connect(_when_vr_active(_cycle_vr_distance))

    # System-tray icon: reset / click-through / size submenu /
    # polling Hz submenu / quit. Held by ``window`` so it lives as long
    # as the overlay does.
    # Updater is built before the tray so the tray's "Check for Updates"
    # action can subscribe to controller state transitions and dispatch
    # IDLE-click → start_check / READY-click → restart_into_update.
    window._updater = UpdateController(window)

    window._tray = make_tray(
        window,
        on_reset=_do_reset,
        on_toggle_click_through=window.toggle_click_through,
        is_click_through=lambda: window.click_through,
        on_set_size=_set_size,
        current_size_index=lambda: size_idx,
        size_labels=SIZE_LABELS,
        widget_toggles=WIDGET_TOGGLES,
        on_toggle_widget=_set_widget_visible,
        is_widget_visible=_is_widget_visible,
        on_set_polling_hz=_set_polling_hz,
        current_polling_hz=lambda: polling_hz,
        polling_hz_options=POLLING_HZ_OPTIONS,
        on_set_vr_placement=_set_vr_placement,
        current_vr_placement=lambda: vr.placement,
        on_set_vr_spread=_set_vr_spread,
        current_vr_spread=lambda: vr.spread,
        vr_spread_options=VR_SPREAD_OPTIONS,
        on_set_vr_distance=_set_vr_distance,
        current_vr_distance=lambda: vr.distance,
        vr_distance_options=VR_DISTANCE_OPTIONS,
        is_vr_active=vr.is_running,
        is_windows_ready=lambda: widgets_ready[0],
        is_game_detected=lambda: game_detected[0],
        on_toggle_logging=_toggle_logging,
        is_logging=logger.is_active,
        on_open_logs_folder=_open_logs_folder,
        on_quit=app.quit,
        updater=window._updater,
        on_toggle_overlay=_toggle_overlay,
        is_overlay_visible=_is_overlay_visible,
        reset_shortcut=HOTKEY_RESET_LABEL,
        quit_shortcut=HOTKEY_QUIT_LABEL,
        logging_shortcut=HOTKEY_LOG_LABEL,
        size_shortcut=HOTKEY_SIZE_LABEL,
        click_through_shortcut=HOTKEY_CLICK_LABEL,
        vr_placement_shortcut=HOTKEY_VR_PLACEMENT_LABEL,
        vr_spread_shortcut=HOTKEY_VR_SPREAD_LABEL,
        vr_distance_shortcut=HOTKEY_VR_DISTANCE_LABEL,
    )

    def _on_update_downloaded(tag: str, path: str) -> None:
        # Tray balloon fires only on a fresh download — already_present
        # (file from a prior session) would otherwise spam this on every
        # startup once the .exe is on disk. The user still sees
        # "Restart to Update" in the tray menu either way.
        tray = getattr(window, "_tray", None)
        if tray is None or not tray.supportsMessages():
            return
        from pathlib import Path as _Path  # local import: rarely used
        tray.showMessage(
            f"Live Telemetry Evo {tag} downloaded",
            f"{_Path(path).name} was saved next to the current app. "
            "Open the tray menu and click \"Restart to Update\" "
            "when you're ready.",
            QSystemTrayIcon.MessageIcon.Information,
            10_000,
        )

    # pylint: disable-next=no-member
    window._updater.download_finished.connect(_on_update_downloaded)

    # Kick off the auto-check at startup. The controller is non-blocking
    # (worker thread) so telemetry detection / countdown / overlay
    # rendering proceed while the request is in flight.
    window._updater.start_check()

    # Hide the telemetry widgets during the countdown — they reveal when
    # the countdown finishes (subject to the persisted visibility flag).
    # The source still feeds frames the whole time so widgets show live
    # data the instant they appear.
    visibility = load_visibility()
    engine.hide()
    inputs.hide()
    for view in wheels.values():
        view.hide()

    countdown = CountdownView(window)
    countdown.setGeometry(0, 0, layout.screen_w, layout.screen_h)
    countdown.raise_()  # ensure it sits above any pre-shown chrome

    def _reveal_widgets() -> None:
        widgets_ready[0] = True
        if not overlay_visible[0]:
            window.hide()
            log("[overlay] overlay not visible; widgets remain hidden")
            return
        if visibility.get("engine", True):
            engine.show()
        if visibility.get("inputs", False):
            inputs.show()
        for wid, view in wheels.items():
            if visibility.get(wid, True):
                view.show()

    countdown.finished.connect(_reveal_widgets)

    # --- iRacing Race Engineer pipeline ---------------------------------
    _is_iracing_source = [False]
    _balance_detector = BalanceDetector()
    _incident_tracker = IncidentTracker()
    _proximity_detector = FasterCarBehindDetector()
    _lap_builder = LapSummaryBuilder(_balance_detector, _incident_tracker)
    _rw_client: RaceWorkerClient | None = None
    _session_sync: SessionSync | None = None
    _speech_scheduler: SpeechScheduler | None = None
    _prev_lap_number = [0]

    def _init_race_engineer(source_name: str) -> None:
        """Spin up race-worker + TTS when the source is iRacing.

        Session creation is deferred until iRacing publishes session YAML
        (track/car/driver). Creating an empty session at startup 422s.
        """
        if source_name not in ("iracing", "iracing-replay"):
            return
        _is_iracing_source[0] = True

        rw_cfg = load_race_worker_settings()
        if rw_cfg["enabled"] and rw_cfg["url"]:
            client = RaceWorkerClient(base_url=rw_cfg["url"],
                                      token=rw_cfg["app_token"])
            nonlocal _rw_client, _session_sync
            _rw_client = client
            _session_sync = SessionSync(client)
            log("[overlay] race-worker client started "
                "(session will be created after iRacing connects)")

        tts_cfg = load_tts_settings()
        engine = get_engine(tts_cfg["engine"])
        if engine is not None:
            engine.configure(voice=tts_cfg["voice"],
                             rate=tts_cfg["rate"],
                             volume=tts_cfg["volume"])
            sched = SpeechScheduler(engine)
            sched.start()
            nonlocal _speech_scheduler
            _speech_scheduler = sched
            log(f"[overlay] TTS scheduler started (engine={tts_cfg['engine']})")

        if _rw_client is not None:
            _rw_client.start_polling(_on_rw_message)

        _init_ptt()

    def _on_rw_message(msg: dict) -> None:
        text = msg.get("text", "")
        if not text:
            return
        prio_str = msg.get("priority", "normal").upper()
        prio = getattr(TTSPriority, prio_str, TTSPriority.NORMAL)
        if _speech_scheduler is not None:
            _speech_scheduler.enqueue(text, priority=prio)
        log(f"[race-worker] message: {text[:80]}")

    def _process_iracing_frame(frame: TelemetryFrame) -> None:
        """Run the race-engineer analysis on every frame when iRacing active."""
        if not _is_iracing_source[0]:
            return
        ir = frame.iracing
        eng = frame.engine
        inp = frame.inputs

        if _session_sync is not None:
            _session_sync.sync(session_payload_from_iracing(ir))

        cur_lap = eng.current_lap
        if cur_lap > _prev_lap_number[0] and _prev_lap_number[0] > 0:
            summary = _lap_builder.finish_lap(
                lap_time=(eng.last_lap_time_ms / 1000.0
                          if eng.last_lap_time_ms > 0 else 0.0))
            if _rw_client is not None:
                _rw_client.post_lap({
                    "lap_number": summary.lap_number,
                    "lap_time": round(summary.lap_time, 3),
                    "position": summary.position,
                    "incidents_total": summary.incidents_total,
                    "incidents_this_lap": summary.incidents_this_lap,
                    "fuel_used": round(summary.fuel_used, 3),
                    "fuel_remaining": round(summary.fuel_remaining, 3),
                    "top_speed": round(summary.top_speed, 1),
                    "dominant_balance": summary.dominant_balance(),
                    "balance_notes": summary.balance_notes_for_api(),
                })
            _lap_builder.new_lap(
                cur_lap, fuel=eng.fuel_liters,
                incidents=ir.incident_count,
                position=ir.player_position,
                class_position=ir.player_class_position)
        elif _prev_lap_number[0] == 0:
            _lap_builder.new_lap(
                cur_lap, fuel=eng.fuel_liters,
                incidents=ir.incident_count,
                position=ir.player_position,
                class_position=ir.player_class_position)
        _prev_lap_number[0] = cur_lap

        _lap_builder.add_sample(
            speed_kmh=eng.speed_kmh,
            steer_rad=ir.steering_angle_rad,
            yaw_rate=ir.yaw_rate,
            brake=inp.brake,
            throttle=inp.throttle,
            lap_dist_pct=ir.lap_dist_pct,
            fuel=eng.fuel_liters,
            incidents=ir.incident_count,
            on_pit_road=ir.on_pit_road,
        )

        proximity_alerts = _proximity_detector.update(
            ir.lap_dist_pct, ir.player_position, ir.cars)
        for pa in proximity_alerts:
            if _speech_scheduler is not None:
                _speech_scheduler.enqueue(pa.message, TTSPriority.HIGH, ttl_seconds=10)
            if _rw_client is not None:
                _rw_client.post_event({
                    "type": "proximity",
                    "message": pa.message,
                    "gap_pct": round(pa.gap_pct, 4),
                })

        current_summary = _lap_builder.current_summary()
        for alert in current_summary.incident_alerts:
            if alert.severity in (IncidentSeverity.WARNING, IncidentSeverity.CRITICAL):
                if _speech_scheduler is not None:
                    prio = (TTSPriority.URGENT if alert.severity == IncidentSeverity.CRITICAL
                            else TTSPriority.HIGH)
                    _speech_scheduler.enqueue(alert.message, prio, ttl_seconds=15)
                if _rw_client is not None:
                    _rw_client.post_event({
                        "type": "incident_alert",
                        "severity": alert.severity.value,
                        "message": alert.message,
                        "count": alert.current_count,
                    })

    # --- PTT (Push-to-Talk) pipeline ------------------------------------
    _ptt_controller: PTTController | None = None

    def _get_driver_context() -> DriverContext:
        f = bus.latest()
        if f is None:
            return DriverContext()
        ir = f.iracing
        eng = f.engine
        inp = f.inputs
        summary = _lap_builder.current_summary()
        corner = None
        if _lap_builder.track_profile:
            corner = _lap_builder.track_profile.corner_at(ir.lap_dist_pct)
        return DriverContext(
            position=ir.player_position,
            class_position=ir.player_class_position,
            lap=eng.current_lap,
            lap_dist_pct=ir.lap_dist_pct,
            corner_name=corner.name if corner else "",
            incidents=ir.incident_count,
            incident_limit_dt=_incident_tracker.thresholds.dt_limit,
            incident_limit_dq=_incident_tracker.thresholds.dq_limit,
            fuel_remaining=eng.fuel_liters,
            fuel_per_lap=summary.fuel_used if summary.fuel_used > 0 else 0.0,
            speed_kmh=eng.speed_kmh,
            throttle=inp.throttle,
            brake=inp.brake,
            steering_rad=ir.steering_angle_rad,
            yaw_rate=ir.yaw_rate,
        )

    _feedback_queue: list[dict] = []

    def _on_driver_feedback(text: str, duration: float, ctx: DriverContext) -> None:
        import time as _time
        event = {
            "type": "driver_feedback",
            "message": text,
            "payload": {
                "transcript": text,
                "audio_duration_s": round(duration, 1),
                "timestamp": _time.time(),
                "lap": ctx.lap,
                "lap_dist_pct": round(ctx.lap_dist_pct, 4),
                "corner": ctx.corner_name,
                "position": ctx.position,
                "telemetry": {
                    "speed_kmh": round(ctx.speed_kmh, 1),
                    "throttle": round(ctx.throttle, 3),
                    "brake": round(ctx.brake, 3),
                    "steering_rad": round(ctx.steering_rad, 4),
                    "yaw_rate": round(ctx.yaw_rate, 4),
                },
            },
        }
        if _rw_client is not None:
            _drain_feedback_queue()
            if not _rw_client.post_event(event):
                _feedback_queue.append(event)
                log("[ptt] driver_feedback queued (worker unavailable)")
        else:
            _feedback_queue.append(event)

    def _drain_feedback_queue() -> None:
        while _feedback_queue and _rw_client is not None:
            ev = _feedback_queue[0]
            if _rw_client.post_event(ev):
                _feedback_queue.pop(0)
            else:
                break

    def _on_ptt_audio(wav_bytes: bytes, duration: float) -> None:
        import threading as _th
        def _stt_and_dispatch():
            from .settings import load_ptt_settings as _load_ptt
            cfg = _load_ptt()
            stt = get_stt_engine(cfg["stt_engine"])
            if stt is None:
                log("[ptt] no STT engine available")
                return
            ptt_key = cfg.get("whisper_api_key", "")
            if ptt_key:
                stt.configure(api_key=ptt_key)
            result = stt.transcribe(wav_bytes)
            if not result.text:
                log("[ptt] empty transcription")
                return
            log(f"[ptt] STT result: {result.text[:80]} (lang={result.language})")
            _intent_handler.handle(result.text, audio_duration=duration)
        _th.Thread(target=_stt_and_dispatch, name="ptt-stt", daemon=True).start()

    def _on_mute_tts(muted: bool) -> None:
        if _speech_scheduler is not None:
            _speech_scheduler.muted = muted

    def _speak_local(text: str) -> None:
        if _speech_scheduler is not None:
            _speech_scheduler.enqueue(text, TTSPriority.HIGH, ttl_seconds=15)

    _intent_handler = IntentHandler(
        on_speak=_speak_local,
        on_driver_feedback=_on_driver_feedback,
        get_context=_get_driver_context,
    )

    def _init_ptt() -> None:
        from .settings import load_ptt_settings
        cfg = load_ptt_settings()
        if not cfg["enabled"]:
            return
        binding_data = cfg.get("binding")
        if not binding_data:
            return
        from .ptt.input_binding import PTTBinding
        binding = PTTBinding.from_dict(binding_data)
        recorder = AudioRecorder(device_index=cfg.get("device_index"))
        controller = PTTController(
            recorder=recorder,
            on_audio_captured=_on_ptt_audio,
            on_mute_tts=_on_mute_tts,
            mode=PTTMode(cfg.get("mode", "hold")),
            max_duration_s=cfg.get("max_duration_s", 15.0),
        )
        controller.set_binding(binding)
        controller.start()
        nonlocal _ptt_controller
        _ptt_controller = controller
        log(f"[overlay] PTT started (mode={cfg['mode']}, binding={binding.display_name})")

    def _shutdown_race_engineer() -> None:
        if _ptt_controller is not None:
            _ptt_controller.stop()
        if _speech_scheduler is not None:
            _speech_scheduler.stop()
        if _rw_client is not None:
            _rw_client.stop_polling()
            _rw_client.end_session()

    # pylint: disable-next=no-member
    app.aboutToQuit.connect(_shutdown_race_engineer)

    def _start_source(name: str, **kwargs) -> None:
        current_source_name[0] = name
        game_detected[0] = True
        source = make_source(name, hz=polling_hz, parent=None, **kwargs)
        source.set_bus(bus)

        _init_race_engineer(name)

        thread = QThread()
        source.moveToThread(thread)
        # ``thread.started`` fires on the worker thread, so ``start()``
        # builds the source's QTimer there — required because Qt timers
        # only tick on the thread they were started from.
        # pylint: disable-next=no-member
        thread.started.connect(source.start)
        # Clean shutdown when the app quits. The CSV writer is joined
        # first so its final flush doesn't race the bus teardown. Then
        # ``stop_requested`` (blocking queued) runs ``source.stop()`` on
        # the worker thread before the loop is quit and joined — quitting
        # first could drop the queued stop, leaving the polling QTimer
        # alive to be destroyed cross-thread at interpreter teardown
        # ("QObject::killTimer: Timers cannot be stopped from another
        # thread").
        def _shutdown() -> None:
            # Logs the saved CSV path if logging was active; plain no-op
            # otherwise (still stops the logger).
            _stop_logging()
            if thread.isRunning():
                source.stop_requested.emit()
                thread.quit()
                thread.wait(2000)

        # pylint: disable-next=no-member
        app.aboutToQuit.connect(_shutdown)
        thread.start()
        # Keep references on the window so the QObjects aren't GC'd when
        # this closure returns.
        window._source = source
        window._source_thread = thread
        # Now that frames will start flowing, kick the UI-side paint
        # loop — unless VR is already live, where the VR tick owns frame
        # dispatch and the desktop overlay is invisible, or the overlay
        # is hidden (no need to repaint invisible widgets — the headless
        # engineer timer keeps the race-engineer pipeline alive instead).
        if not overlay_visible[0]:
            engineer_timer.start()
        elif not vr.is_running():
            repaint_timer.start()

        log(
            f"[overlay] source={name} polling_hz={polling_hz} "
            f"repaint_hz={refresh_hz:.0f} "
            f"screen={geom.width()}x{geom.height()} "
            f"resolution={layout.resolution_name} multiplier={layout.multiplier:.2f} "
            f"engine={layout.engine.w}x{layout.engine.h} "
            f"wheel={layout.wheels['FL'].w}x{layout.wheels['FL'].h}"
        )
        countdown.start(name)

    window.show()

    # Provide the overlay window's native handle to DirectInput8 so it
    # can set a cooperative level without creating its own HWND.
    try:
        from .ptt.dinput import set_hwnd
        set_hwnd(int(window.winId()))
    except (ImportError, Exception):
        pass

    # Default to click-through ON: a full-screen overlay must not steal mouse
    # input from the game underneath. Toggle from the tray (Windows menu).
    # log=False — this is startup initialisation, not a user action.
    window.toggle_click_through(log=False)

    if args.source == "auto":
        detection = DetectionView(window)
        detection.setGeometry(0, 0, layout.screen_w, layout.screen_h)
        detection.raise_()
        detection.detected.connect(_start_source)
        window._detection = detection
        detection.start()
    elif args.source == "iracing-replay" and args.replay_csv:
        _start_source(args.source, csv_path=args.replay_csv)
    else:
        _start_source(args.source)

    # --- VR enable/teardown wiring ------------------------------------
    # One timer owns the whole VR frame pipeline: dispatch the latest
    # telemetry into the widgets, grab them, composite, upload. Folding
    # the dispatch in (instead of leaving the repaint timer running)
    # matters at high rates — two timers on the UI thread starve each
    # other once a tick costs more than the interval, and the headset
    # then streams stale widget pixels. Re-armed to the HMD's display
    # refresh rate when VR enables (45 Hz placeholder until then), and
    # stepped down automatically when the measured tick cost can't
    # sustain the interval. Precise timer type — a coarse timer's 5 %
    # batching on Windows would cap a ~11 ms interval near 60 Hz.
    vr_submit_timer = QTimer(window)
    vr_submit_timer.setTimerType(Qt.TimerType.PreciseTimer)
    vr_submit_timer.setInterval(int(1000 / 45))
    vr_tick_ema_ms = [0.0]  # smoothed tick cost, for the step-down

    def _vr_submit() -> None:
        if not vr.is_running() or not overlay_visible[0]:
            return
        t0 = time.perf_counter()
        f = bus.latest()
        if f is not None:
            _dispatch_frame(f, engine, inputs, wheels)
            _process_iracing_frame(f)
        t1 = time.perf_counter()
        items = []
        for key, view in (("engine", engine), ("inputs", inputs), *wheels.items()):
            if not view.isVisible():
                continue
            g = view.geometry()
            items.append((key, view.grab().toImage(),
                          g.x(), g.y(), g.width(), g.height()))
        t2 = time.perf_counter()
        vr.submit_widgets(items, layout.screen_w, layout.screen_h)
        # Adaptive pacing: when a whole tick costs more than ~80 % of the
        # interval the UI thread has no headroom left for anything else
        # (tray, hotkeys, detection), so halve the rate instead of
        # letting the event loop saturate. Steps 90 → 45 → 22 and stays
        # there; never throttles below ~20 Hz.
        cost_ms = (time.perf_counter() - t0) * 1000.0
        ema = vr_tick_ema_ms[0]
        ema = cost_ms if ema == 0.0 else ema * 0.9 + cost_ms * 0.1
        vr_tick_ema_ms[0] = ema
        interval = vr_submit_timer.interval()
        if ema > interval * 0.8 and interval < 50:
            vr_submit_timer.setInterval(interval * 2)
            vr_tick_ema_ms[0] = 0.0
            pump_ms, composite_ms, upload_ms = vr.phase_ms
            log(f"[overlay] VR tick costs {ema:.1f} ms "
                  f"(dispatch {(t1 - t0) * 1000:.1f} + "
                  f"grab {(t2 - t1) * 1000:.1f} + pump {pump_ms:.1f} + "
                  f"composite {composite_ms:.1f} + upload {upload_ms:.1f}); "
                  f"pacing down to {1000 / (interval * 2):.0f} Hz")

    # pylint: disable-next=no-member  # QTimer.timeout is a PySide6 Signal
    vr_submit_timer.timeout.connect(_vr_submit)
    # pylint: disable-next=no-member
    app.aboutToQuit.connect(vr.stop)

    def _enable_vr() -> bool:
        if not vr.start():
            return False
        # Pace the VR tick at the headset's display rate (fall back to
        # the desktop refresh rate when the runtime doesn't report one).
        # The VR tick dispatches telemetry itself, so the desktop repaint
        # timer is stopped — the desktop overlay is invisible anyway and
        # a second timer would just compete for the UI thread.
        hmd_hz = vr.display_hz() or refresh_hz
        vr_submit_timer.setInterval(max(1, round(1000 / hmd_hz)))
        vr_tick_ema_ms[0] = 0.0
        repaint_timer.stop()
        log(f"[overlay] VR refresh: {hmd_hz:.0f} Hz")
        # Hide the × buttons — the grab would bake them into the headset
        # quad, and there is no pointer in VR to click them.
        for view in (engine, inputs, *wheels.values()):
            view.set_close_visible(False)
        vr_submit_timer.start()
        # VR is live: the HUD is rendered inside the headset, so blank the
        # desktop overlay entirely — a duplicate copy floating on the
        # monitor is just clutter. Full window transparency (rather than
        # hiding the widgets) keeps each widget rendering, so ``_vr_submit``
        # can still ``grab()`` real pixels and ``isVisible()`` stays True.
        window.setWindowOpacity(0.0)
        return True

    # Resolve mode: --novr off, --vr force, neither = auto-detect.
    if args.novr:
        vr_mode = "off"
    elif args.vr:
        vr_mode = "force"
    else:
        vr_mode = "auto"

    if vr_mode == "force":
        if not _enable_vr():
            log("[overlay] VR requested but unavailable; "
                  "staying on desktop overlay")
    elif vr_mode == "auto" and VROverlayOutput.available():
        # Poll for a live SteamVR session; flip into VR on first detection,
        # then stop polling. Mirrors DetectionView's poll-and-start idiom.
        vr_detect_timer = QTimer(window)
        vr_detect_timer.setInterval(2000)

        def _vr_detect_tick() -> None:
            if vr_detect.vr_active():
                vr_detect_timer.stop()
                if _enable_vr():
                    log("[overlay] SteamVR detected; VR overlay enabled")

        # pylint: disable-next=no-member
        vr_detect_timer.timeout.connect(_vr_detect_tick)
        vr_detect_timer.start()
        window._vr_detect_timer = vr_detect_timer

    # Keep references on the window so the QObjects survive past run()'s scope.
    window._vr = vr
    window._vr_submit_timer = vr_submit_timer

    # --open-dialog: open settings dialogs on startup (for smoke tests).
    if args.open_dialog:
        _dialog_names = [d.strip() for d in args.open_dialog.split("|")]
        def _auto_open_dialogs():
            for dname in _dialog_names:
                dlg = None
                if dname == "race_worker":
                    from .race_worker_dialog import RaceWorkerDialog
                    dlg = RaceWorkerDialog()
                elif dname in ("tts", "tts_elevenlabs"):
                    from .tts.settings_dialog import TTSSettingsDialog
                    dlg = TTSSettingsDialog()
                    if dname == "tts_elevenlabs":
                        idx = dlg._engine_combo.findText("elevenlabs")
                        if idx >= 0:
                            dlg._engine_combo.setCurrentIndex(idx)
                elif dname in ("ptt", "ptt_diag"):
                    from .ptt.settings_dialog import PTTSettingsDialog
                    dlg = PTTSettingsDialog()
                    if dname == "ptt_diag":
                        QTimer.singleShot(500, dlg._on_diagnostics)
                elif dname == "debug":
                    from .debug_dialog import DebugDialog
                    dlg = DebugDialog()
                if dlg is not None:
                    dlg.setWindowFlags(
                        dlg.windowFlags() | Qt.WindowStaysOnTopHint)
                    dlg.show()
                    dlg.raise_()
                    dlg.activateWindow()
                    window._smoke_dlg = dlg
        QTimer.singleShot(2000, _auto_open_dialogs)

    # Ctrl+C in a console (dev runs / run.bat) should shut down as gracefully
    # as the tray Quit and Ctrl+Shift+Q paths: route SIGINT through
    # app.quit() so ``aboutToQuit`` fires — flushing/closing the CSV log,
    # releasing the global hotkeys and stopping the VR overlay — instead of
    # the interpreter's default hard KeyboardInterrupt. A 200 ms idle timer
    # keeps the Python interpreter ticking so the pending signal is actually
    # delivered while Qt's C++ event loop is running (otherwise Ctrl+C can
    # sit unhandled until the next Python callback). The windowed .exe has no
    # console, so this is a no-op there.
    signal.signal(signal.SIGINT, lambda *_args: app.quit())
    sigint_timer = QTimer(window)
    sigint_timer.setInterval(200)
    # pylint: disable-next=no-member  # QTimer.timeout is a PySide6 Signal
    sigint_timer.timeout.connect(lambda: None)
    sigint_timer.start()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(run())
