# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/).

## [0.9.0-test.12] - 2026-10-12

### Fixed
- **DirectInput `c_dfDIJoystick2` flood**: that symbol lives in `dinput8.lib`, not `dinput8.dll`. The client now builds the official `DIDATAFORMAT` / `DIOBJECTDATAFORMAT` array for `DIJOYSTATE2` (272 bytes, 164 objects, 128 buttons) in ctypes. Device setup (`SetDataFormat` / cooperative level / acquire) runs once per device; repeated errors are rate-limited.
- **PTT backend order**: if DirectInput enumerates devices but cannot poll them, PTT listing, assign, and hold fall back to pygame (128 buttons). winmm is last. pygame inits with `SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS` and a hidden display; assign no longer deadlocks by joining its own poll thread.
- **Diagnostics Health 405**: the Health line uses authenticated `GET /race/sessions` (same as Check Connection). HTTP 404/405 is reported as `server needs update` (older race-worker is POST-only on that path).

### Added
- Unit test that builds the `DIJOYSTATE2` data format and checks sizes/offsets/object count. `--self-test` treats a format-size mismatch as fatal.

## [0.9.0-test.11] - 2026-10-12

### Fixed
- **Race-worker 422 at startup**: `create_session` is no longer called with only `source=` before iRacing connects. The client waits for session YAML (`sim`, `track_id`, `track_name`, `track_config`, `car`, `series`, `session_type`, `driver`), recreates the session on a session change, retries with backoff, and logs the 422 response body.
- **Check Connection false OK**: the dialog hits authenticated `GET /race/sessions` so a wrong token shows 401. Pasted tokens are stripped of whitespace and trailing punctuation.
- **Updater HTTP 406**: GitHub requests send `Accept: application/vnd.github+json`, `User-Agent: LiveTelemetryEvo/<ver>`, and `X-GitHub-Api-Version: 2022-11-28`.
- **DirectInput hidden HWND**: window class is registered with correct x64 ctypes prototypes; `GetLastError` is logged; falls back to `GetDesktopWindow()`.
- **winmm JOYCAPSW size**: struct is 728 bytes (`szOEMVxD` = 260 WCHARs); `JOYINFOEX` is 52. Self-test fails the build if sizes are wrong.
- **pynput / pygame missing in the exe**: added to requirements and PyInstaller hiddenimports / collected DLLs. Self-test imports both.
- **piper_engine import error**: optional stub; missing module no longer logs an error.
- **Misleading `--overlay-hidden` log**: hide-on-reveal no longer pretends the CLI flag was passed.
- **TTS preview / engineer playback**: PowerShell `Media.SoundPlayer` (WAV-only, flashes a console, swallowed errors) is gone. Playback is in-process (`winsound` / `pygame.mixer` / `sounddevice`). ElevenLabs requests `pcm_24000` and wraps it as WAV. Every synth/playback step is logged; the TTS dialog shows the error instead of hanging on «Синтез...».
- **GP preset Similarity 0.75**: saved GP profiles with the dataclass default 0.75 (or zeros) migrate back to the official preset — stability 0.8, similarity 0.9, style 0, speed 0.9, language en.

### Added
- `GET /race/sessions` (auth required) lists recent sessions.
- CI e2e: `RaceWorkerClient` against a local race-worker (create session, post lap, event, `driver_feedback`).
- `--self-test` hardware-independent checks are fatal on the Windows smoke runner (device count > 0 remains non-fatal).

## [0.9.0-test.8] - 2026-10-12

### Fixed
- **Joystick/wheel still not detected (winmm limitation)**: winmm's `joyGetDevCapsW` only sees legacy joystick-mapper devices. Modern HID-based wheels (Moza R16 via Pit House) don't register in the legacy mapper. Added a DirectInput8 backend via raw ctypes COM as the primary detection path — it sees all HID game controllers visible in Windows "Game Controllers", operates in background mode (`DISCL_BACKGROUND | DISCL_NONEXCLUSIVE`), and supports 128 buttons per device.

### Added
- **DirectInput8 backend** (`dinput.py`): `IDirectInput8W::EnumDevices` + `CreateDevice` + `SetDataFormat(c_dfDIJoystick2)` + `GetDeviceState(DIJOYSTATE2)` via raw COM vtable calls. Hidden message-only HWND for cooperative level. VID:PID extracted from `guidProduct`.
- Backend priority: DirectInput8 → winmm → pygame.
- **"Диагностика устройств"** button in PTT dialog: runs all three backends and shows results (name, VID:PID, button count, errors) in a copyable text box. Also logged to session.log.
- `--open-dialog ptt_diag` CLI flag auto-opens PTT dialog and runs diagnostics (used in smoke test).
- Smoke test phase 5: PTT diagnostics screenshot (`05-ptt-diagnostics.png`).
- Support for button IDs > 32 in binding format (DIJOYSTATE2 has 128 buttons).
- Devices freshly enumerated on each dialog open / assign press (hotplug support).
- 7 new tests (244 total): DInput dataclass, DIJOYSTATE2 128-button layout, high button IDs, JoystickInfo, diagnostics report, backend field, run_diagnostics string content.

## [0.9.0-test.7] - 2026-10-12

### Added
- **ElevenLabs Scribe STT engine** (`elevenlabs-scribe`): POST to `/v1/speech-to-text` with multipart `model_id=scribe_v1` and WAV file. Supports optional `language_code` (default auto; `ru`/`en`). Now the default STT engine for new installs.
- Scribe engine reuses the ElevenLabs API key from TTS settings (single shared key). PTT dialog shows "uses ElevenLabs key from TTS" hint with option to override via a separate key field.
- 9 new tests: registry, mocked HTTP (success, language_code, HTTP error, timeout), TTS key fallback, no-key-leak source scan, default engine check.

## [0.9.0-test.6] - 2026-10-12

### Fixed
- **Joystick/wheel not detected**: pygame (SDL2) DLLs were not bundled by PyInstaller, and SDL requires a display surface + foreground focus for joystick events. Replaced with a native Windows `winmm.dll` backend (`joyGetNumDevs` / `joyGetDevCapsW` / `joyGetPosEx` via ctypes) that works without SDL, without focus, and detects DirectInput devices (Moza, Fanatec, Thrustmaster, Logitech) while iRacing is in the foreground. Falls back to pygame on non-Windows platforms.

### Added
- `winmm.dll` joystick backend: `list_joysticks_winmm()` enumerates devices, `_read_buttons_winmm()` polls up to 32 buttons per device at ~100 Hz from a background thread.
- `WinmmJoystick` dataclass with index, name, and num_buttons.
- Joystick enumeration results logged to session.log for debugging.
- PTT dialog shows winmm-detected device names and button counts.
- 8 new tests for the winmm backend (struct fields, mocked enumeration, button reading, fallback preference).

## [0.9.0-test.5] - 2026-10-12

### Fixed
- **App quits when closing any settings dialog**: `QApplication.quitOnLastWindowClosed` defaulted to `True`; closing a dialog while the overlay window was hidden caused the app to exit. Fixed with `app.setQuitOnLastWindowClosed(False)` — quit only via tray Exit or Ctrl+Shift+Q.
- **PTT button assignment silently fails**: `_on_binding_captured` was called from pynput/pygame background threads but updated Qt widgets directly, violating Qt's thread-affinity rules. Fixed with a `QObject` signal bridge that marshals the binding to the GUI thread via a queued connection.
- **PTT dialog shows only "Default" device**: `sounddevice` (PortAudio) DLLs were not bundled by PyInstaller. Added `sounddevice` and `_sounddevice_data` to hidden imports and collected their native libraries in the spec. Devices are now deduped by name (preferring WASAPI over MME/DirectSound) and persisted by name+hostapi rather than volatile index.
- **Edge-TTS voice list empty on CI/frozen exe**: `asyncio.get_event_loop()` fails in frozen builds; replaced with `asyncio.new_event_loop()`.

### Added
- PTT dialog improvements: "Нажмите клавишу или кнопку руля..." prompt during assign, Cancel button + Esc to abort, detected joysticks list for debugging, device refresh button (⟳), hostapi displayed per device.
- 8 new tests: device dedup, name-based persistence, binding round-trip, `quitOnLastWindowClosed` check.

## [0.9.0-test.4] - 2026-10-12

### Fixed
- **Overlay toggle now uses `window.hide()`** instead of `setWindowOpacity(0.0)` which silently failed on Windows Server (no DWM compositor). The overlay window is now definitively hidden.
- `_reveal_widgets` actively hides the window when `overlay_visible` is false, instead of just skipping widget show calls.
- `_start_source` starts the headless engineer timer (instead of the repaint timer) when overlay is hidden at startup, keeping the race-engineer pipeline alive without rendering.

### Added
- 9 new tests: widget `isVisible()` / `window.hide()` / `window.show()` assertions after toggle, `_reveal_widgets` behavior for both states, tray toggle callback contract, parameter introspection.
- Automated pixel-diff check in smoke workflow: compares widget regions between overlay-visible and overlay-hidden screenshots and fails if they're nearly identical.

## [0.9.0-test.3] - 2026-10-12

### Added
- **Overlay toggle**: checkable tray menu item "Оверлей" to show/hide the telemetry overlay independently of the race-engineer, TTS, and PTT pipelines (which keep running when hidden).
- Optional global hotkey for the overlay toggle, configurable in settings (`overlay_hotkey` in positions.json). Supports keyboard key or wheel button via the existing PTT input binding code.
- Overlay visibility state persisted in `positions.json` (`overlay_visible` key) and restored on startup.
- CLI flag `--overlay-hidden` to start with the overlay hidden (useful for smoke tests and automation).
- When hidden, widgets stop rendering/repainting to save CPU; the telemetry source, frame bus, race-engineer analysis, and speech scheduler continue uninterrupted.
- Headless engineer timer keeps the iRacing race-engineer pipeline ticking when the overlay is hidden.
- Smoke workflow screenshot with overlay hidden (`01b-overlay-hidden.png`).
- 14 new tests for overlay toggle state logic and settings persistence.

## [0.8.1] - 2026-08-20

### Fixed
- Fixed missing `import json` in `logger.py` that caused background CSV writer thread to crash and drop telemetry rows.

## [0.8.0] - 2026-08-20

### Added
- AI Cloud Race Engineer Platform integration for automated telemetry ingestion and coaching.
- Metadata extraction from AC Evo `StaticView` and `GraphicsView` (track, track configuration, car model, driver name).
- Accurate in-game lap counter, lap validity flag, and millisecond lap timing capture.
- Telemetry manifest JSON header in CSV exports.
- Qt6 Settings Dialog (`🏎️ AI Cloud Engineer...`) in system tray for configuring backend server, API token, pilot ID, and coaching profile with live connectivity test.
- Asynchronous cloud dispatcher for background zip compression and upload without game stutter.

## [0.7.4] - 2026-08-02

### Fixed
- ACC brake bias now matches the in-car dash: the per-car offset from the official documentation is applied to the raw shared-memory value.
- Changing car without restarting the game kept the previous car's power scale, ideal pressures and calibrated maxima — all of it re-seeds on a car change now.
- Brake bias read one point low (a 69 % setting showed 68 %) — the readout truncated the game's value instead of rounding it.
- Fitting a compound the car files don't describe kept the previous compound's temperature window instead of the default curve.
- AC Rally's spec sheet is picked up when the game fills it in, instead of only at connect time.

### Changed
- Per-car power and tyre-temperature colour curves rebuild only when the curve changes, not on every repaint.
- Docs: the suspension bar is scaled to the car's mechanical travel or the session's deepest compression — never to the setup's packer range.
- Docs: the suspension reference self-calibrates on ACC and AC EVO, and one hard kerb strike can set it for the rest of the session.

## [0.7.3] - 2026-08-01

### Fixed
- AC Rally was not detected at all (regression in 0.7.0) — its `acr.exe` process was missing from the detector's list of known executables.

### Changed
- Game auto-detection now identifies the game solely by its executable, matched exactly against the running-process list.
- AC1's 32-bit `acs_x86.exe` is now recognised.
- Detection no longer reads shared memory, and no longer tells ACC from AC Rally by tyre-temperature units.
- A stale `acpmf_*` mapping is never mistaken for a running game.
- A game on its main menu is detected immediately, with no 10-second wait before defaulting to ACC.
- The overlay appears when the game process starts, not at its first telemetry frame; readers connect once it publishes.

## [0.7.2] - 2026-07-24

### Fixed
- Bundled `.exe` crashed at launch with `No module named 'PySide6.QtOpenGL'` — the build stripped `QtOpenGL`/`Qt6OpenGL.dll`, which the VR overlay imports at startup. Both are now kept in the bundle.

## [0.7.1] - 2026-07-23

### Added
- Per-widget show/hide: a **Widgets** submenu in the tray **VR** and **Windows** menus toggles each panel (engine, inputs, four wheels); persisted.
- VR widgets wrap around the viewer on a **cylinder** — the corner panels turn to face inward — instead of sitting on a flat plane.
- Tray **VR** submenus **Spread** (sideways fan-out) and **Distance** (panel distance), plus **Size** in VR — each persisted, applied live.
- Global hotkeys `Ctrl+Shift+C` (toggle click-through) and `Ctrl+Shift+S` (cycle widget size `XS → S → M → L → XL`, wrapping).
- Global VR hotkeys `Ctrl+Shift+P` (placement toggle), `Ctrl+Shift+W` (spread) and `Ctrl+Shift+D` (distance); each cycles its tray submenu.

### Changed
- VR submits each widget as its own native-resolution quad instead of one screen-size canvas — smaller uploads, and a hidden widget costs nothing.
- Tray **Windows** and **VR** categories are mutually exclusive: **Windows** greys out while VR renders, **VR** greys out on the desktop overlay.
- **Reset** restores the **Inputs** widget's default position but leaves it hidden (it's off by default); use the Widgets toggle to show it.
- Hotkeys moved from `Ctrl+Alt` to `Ctrl+Shift` (less contested by gaming peripherals and vendor overlays); logging is now `Ctrl+Shift+L`.
- Full global hotkey set: `Ctrl+Shift` + `R` reset, `C` click-through, `S` size, `L` logging, `P`/`W`/`D` VR placement/spread/distance, `Q` quit.
- Each hotkey is inert while its tray entry is greyed: reset/size/click-through need widgets placed, logging a detected game, VR keys a live headset.
- Tray **Data** category (Polling Hz, logging, Open logs folder) and `Ctrl+Shift+L` stay disabled until a game is detected and telemetry flows.

### Fixed
- VR HUD widgets were clipped and shrank at non-default **Size** — fixed by a max-size texture with the widget centred and full (not sub-rect) bounds.
- ACC is no longer detected from a stale `acpmf` shared-memory tag; a live ACC process must be running before its physics block is trusted.
- Global hotkeys are released on every quit path, not just `closeEvent` (skipped by `QApplication.quit()`), avoiding err 1409 on the next launch.
- Hotkeys register per-combo with failures attributed individually, so one `GetLastError()` no longer misreports a single conflict as total failure.

## [0.7.0] - 2026-06-11

### Added
- VR output: the HUD is submitted to the **SteamVR / OpenVR** compositor as a flat overlay quad — one backend for every PCVR headset.
- `--vr` forces VR on, `--novr` forces it off; default auto-detects a running SteamVR runtime + connected HMD and enables VR on its own.
- Tray submenu **VR placement** — **Head-locked** (default) or **Fixed in place**; persisted and applied to the live overlay without a restart.
- **Fixed in place** freezes the quad where it floats at activation; re-selecting it re-anchors at the current gaze.
- New `overlay.vr` package — `detect` (SteamVR/HMD probes) + `VROverlayOutput` (lifecycle, placement, submit, event pump); import-guarded.
- Frames stream into a persistent OpenGL texture ring (V bounds flipped to render right-side-up); raw-RGBA fallback when GL init fails.
- One VR tick dispatches telemetry, grabs, composites, and submits at the HMD refresh rate; steps down when the tick cost can't sustain it.
- Desktop overlay is blanked while VR is live (no duplicate copy on the monitor); widgets keep rendering so the headset still receives them.
- Widget `×` close buttons are hidden while VR is live — the grab would bake them into the headset quad with no pointer to click them.

### Fixed
- Update check queries `github.com` instead of `api.github.com` (unreachable on some networks); asset URL rebuilt from the release name.

## [0.6.6] - 2026-05-19

### Added
- Auto-update on launch: hits GitHub releases API, downloads the new `LiveTelemetryEvo-<version>.exe` next to the app; skips already-downloaded files.
- Streams via a `.partial` sibling + atomic rename so a crash mid-download never leaves a half-file under the final name.
- Tray entry **Check for Updates** — 4 states: idle → `Checking...` → `Downloading...` → **Restart to Update** (launches new .exe, quits).
- Tray balloon notification when a fresh download completes; silent on already-on-disk / up-to-date / offline paths.
- New `overlay.updater` module — `UpdateChecker` worker + `UpdateController` (state machine + Qt signals the tray menu subscribes to).

## [0.6.5] - 2026-05-18

### Added
- Global hotkey `Ctrl+Alt+C` toggles CSV logging on/off, mirroring the tray **Start logging** / **Stop logging** action.
- Polling and rendering decoupled across threads — source runs on its own `QThread`; UI repaints at `QScreen.refreshRate()` Hz.
- `FrameBus` transports the latest snapshot (deep-copied, under lock) to the UI and a bounded queue to the CSV writer thread.
- Tray submenu **Polling Hz** — 30 / 60 / 100 / 120 / 144 / 250; choice persisted via `settings.json`.
- Tray actions **Start logging** / **Stop logging** + **Open logs folder**; CSV writes on a dedicated thread off the polling worker.
- CSV schema auto-built from `dataclasses.fields()` on `TelemetryFrame`; new fields appear in the next session's file automatically.
- New `overlay.paths` module — `positions.json` and `logs/` sit next to the executable (CWD in dev); `LIVE_TELEMETRY_DATA_DIR` overrides.
- Countdown screen shows the **detected game name** above the digit, both centred at the detection-screen font size.
- AC EVO: KERS fields exposed (`has_kers`, `battery_temp_c`, lap-cap flags, overtake/heat/deploy/recharge maps); chips KMAX/CMAX/OT/HEAT.
- AC1: per-car ACD data plumbed to widgets — torque curve drives rev-bar tint, per-compound curves drive tyre temps, ideal psi surfaced.
- `WheelData.susp_v` flag — true when `susp_m_t` is rolling-max calibrated; suspension mid-band paints **blue** in that mode.

### Changed
- AC1 boost bar: dropped `static.maxTurboBoost` seed (unreliable on mods); right-edge now tracks rolling max of observed `turbo_boost`.
- Engine rev-bar colour: uses per-car torque curve from ACD on AC1; otherwise self-calibrates from observed `current_bhp` peak RPM.
- Boost and KERS battery bar slots fully transparent on NA / ICE-only cars — no more black stripes at the top of the engine widget.
- AC EVO ABS blink merges all three signals (`absInAction` int, `abs` intensity float, `graphics.abs_active` bool); same logic for TC.
- Per-wheel ABS heuristic on AC EVO uses documented `slipRatio` (not `wheelSlip`); threshold 0.10 → 0.03 — fires before the wheel is lost.
- Brake disk now always rendered on AC1 (neutral white base + blue ABS blink); other games keep temp-curve tint + white blink.
- AC1 + AC EVO: per-wheel body-roll height correction — per-axle `rideHeight` split by relative suspension travel across the axle.
- Engine `max_turbo_boost` default 1.2 → 0.0 so NA cars on every source hide the boost slot via the existing `> 0.05` gate.
- Default polling rate moved from CLI-only `--hz` to a persisted setting changeable at runtime; `--hz 0` (new default) uses persisted value.

### Fixed
- AC1 suspension calibration: removed 5 % headroom and 2× initial seed; static `suspensionMaxTravel` now trusted as-is. ACC/Rally too.
- AC1 tyre-wear bar: raw `tyreWear` is a grip/health value (~0.06 useful range), not a linear 0-100 % counter — remapped onto 0..1.
- KERS deploy-power on AC EVO now gates on `physics.ersIsCharging` instead of inferring direction from SoC drop; robust at SoC 0 / 1.
- `has_brake_temp=False` on AC1 no longer hides the whole brake column — icon stays for ABS/lock blink, only the `°C` label is suppressed.
- Dropped wasted writes to `brake_t` / `brake_t_norm` on AC1 (consumed only when `has_brake_temp=True`).
- Sibling project fix: `lt_wheel_info.py` body-roll correction mixed Python-API and SHM reads across wheels — now consistent on both sides.

## [0.6.0] - 2026-05-11

### Added
- `--source auto` (now default) detects the running game via SHM tag namespace + process snapshot; shows "Detecting AC Environment..." until found.
- Assetto Corsa Rally support via `--source acrally` flag.
- Assetto Corsa Competizione support via `--source acc` flag.
- Original Assetto Corsa support via `--source ac1` flag.
- Tire silhouette rotates by per-wheel camber on each wheel widget.
- Contact-patch bars below each tire encoding inner/middle/outer load distribution.
- Brake-pad and disc wear bars on each wheel widget, self-calibrated against per-wheel max observed since session start.
- New `pad_w` / `disc_w` fields on `WheelData`, populated from `ph.padLife` / `ph.discLife`.
- Per-zone temperature readouts (inner / middle / outer / core) on the tyre silhouette, with luminance-based text colour.

### Changed
- `WheelData` gains `has_wheel_load`, `has_ride_height`, `has_camber` capability flags; widgets hide indicators when a source lacks the signal.
- Removed the trapezoidal camber strip; tire rotation plus contact bars replace it.
- IMO temp grid always shows the inner face on the screen-centre-facing side of the widget.
- Wheel widget logical height bumped 271 → 316 to fit the new bars and rotated tire.
- Synthetic source: right-side camber sign flipped; corner-load → camber coupling rescaled.
- Brake icon no longer shrinks with disc temperature; temperature drives only tint, wear moves to the dedicated bars.

### Fixed
- Per-face tyre temperatures now read from graphics-block TyreState on AC EVO instead of the dead legacy AC1 slots.
- Brake icon now falls back to interpolating the brake curve from raw temp when `brake_normalized_temperature` is missing.
- Tyre-wear restyled as a horizontal "Tire Wear" bar stacked under the existing Disk/Pads Wear rows; gated on `has_tire_wear` (hidden on AC EVO).
- AC1 hides the Disk/Pads Wear bars — AC1's SDK predates `padLife` / `discLife`, so the bars previously rendered a stuck-fresh value.
- AC1 hides the brake icon entirely (icon + label) since `brakeTemp` is never written by the game; lock/ABS blink disappears with it on AC1.
- Wear bars now space themselves evenly between the brake column and pressure icon — a lone visible bar pins to the top of the available height.
- AC1 contact-patch middle bar no longer collapses: new `has_pressure_norm` flag neutralises the pressure axis when the norm is a rough synthesis.
- AC1 reads per-car spec data from `data.acd`: real per-axle ideal pressure, raw torque LUT for live BHP/torque, per-compound tyre thermal curves.
- AC install auto-detected via Steam registry + `libraryfolders.vdf`; honours `LT_AC_PATH` env override and falls back to the default Steam path.

## [0.5.0] - 2026-05-01

### Added
- System-tray icon with context menu: Reset positions, Click-through toggle, Size submenu (XS/S/M/L/XL), Quit.
- Global hotkeys: `Ctrl+Alt+R` resets widget positions; `Ctrl+Alt+S` cycles widget size.
- Phase 1 — driver-aid chips on the engine widget: ESC, LC, DRS, ERS, WW, INV, LAST.
- Phase 2 — analog readouts row on the engine widget: water/oil temp, oil/fuel pressure, exhaust temp, battery V, fuel L, brake bias.
- Phase 3 — new inputs widget: pedal bars, steering bar, FFB clipping, G-meter, damage chips, tyres-off count, performance-mode label.
- 18 MDI icons rasterised into `resources/img/` via a new `fetch_icons.py` helper.
- GitHub Action that builds the Windows executable and publishes a release on every `v*` tag push.

### Changed
- Removed the floating reset and size buttons from the overlay; tray menu and hotkeys replace them.
- App icon replaced with higher-resolution **LT** branding (256×256); tray and EXE share the source file.
- Engine widget logical height grew 120 → 148 px to fit the Phase 2 readouts row.

### Fixed
- Tire temperature grid no longer shows transparent seams between colours; AA disabled for that grid.
- Tray context menu no longer flickers under the overlay; topmost-reassertion skips while a popup is active.
- `build.py` now bundles `resources/icon.png` so the tray icon resolves in the frozen build.

[0.7.4]: https://github.com/albertowd/live-telemetry-evo/compare/v0.7.3...v0.7.4
[0.7.3]: https://github.com/albertowd/live-telemetry-evo/compare/v0.7.2...v0.7.3
[0.7.2]: https://github.com/albertowd/live-telemetry-evo/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/albertowd/live-telemetry-evo/compare/v0.6.6...v0.7.1
[0.7.0]: https://github.com/albertowd/live-telemetry-evo/compare/v0.6.6...v0.7.0
[0.6.6]: https://github.com/albertowd/live-telemetry-evo/compare/v0.6.5...v0.6.6
[0.6.5]: https://github.com/albertowd/live-telemetry-evo/compare/v0.6.0...v0.6.5
[0.6.0]: https://github.com/albertowd/live-telemetry-evo/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/albertowd/live-telemetry-evo/releases/tag/v0.5.0
