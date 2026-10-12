"""FFB-safe wheel polling invariants.

Button polling must never steal, reset, or invert a sim's force-feedback
device. test.12 inverted Moza R16 FFB in iRacing while pygame/SDL HIDAPI
had the wheel open; FFB returned to normal after the app quit.

Rules:
- DirectInput cooperative level is ``DISCL_NONEXCLUSIVE | DISCL_BACKGROUND``
  only. Never exclusive, never foreground.
- Never send/stop/reset effects, never set autocenter / ``DIPROP_FFGAIN``.
- pygame/SDL inits the joystick subsystem only (never ``pygame.init()``
  or haptic). HIDAPI, raw input, and WGI are forced off so SDL does not
  open the wheel and send reports.
- Prefer winmm (read-only ``joyGetPosEx``) — it cannot touch FFB.
"""
from __future__ import annotations

import os

from ..logbook import log

# DirectInput cooperative level (dinput.h)
DISCL_EXCLUSIVE = 0x00000001
DISCL_NONEXCLUSIVE = 0x00000002
DISCL_FOREGROUND = 0x00000004
DISCL_BACKGROUND = 0x00000008
DISCL_NOWINKEY = 0x00000010

# The only flags we ever pass to SetCooperativeLevel.
DISCL_SAFE = DISCL_NONEXCLUSIVE | DISCL_BACKGROUND  # 0x0A

# IDirectInputDevice8W vtable slots that mutate FFB / device properties.
# Listed so tests can assert the poller never calls them.
VT_DEV_SETPROPERTY = 6          # DIPROP_AUTOCENTER, DIPROP_FFGAIN
VT_DEV_CREATEEFFECT = 18
VT_DEV_GETFORCEFEEDBACKSTATE = 21
VT_DEV_SENDFORCEFEEDBACKCOMMAND = 22

WHEEL_BACKENDS = ("winmm", "dinput", "pygame", "off")
DEFAULT_WHEEL_BACKEND = "winmm"
WHEEL_BACKEND_ALIASES = {
    "directinput": "dinput",
    "dinput8": "dinput",
    "di": "dinput",
    "sdl": "pygame",
    "sdl2": "pygame",
}

# Both SDL_HINT_* names and the env-var names SDL actually reads.
SDL_FFB_SAFE_HINTS: dict[str, str] = {
    "SDL_HINT_JOYSTICK_HIDAPI": "0",
    "SDL_JOYSTICK_HIDAPI": "0",
    "SDL_HINT_JOYSTICK_RAWINPUT": "0",
    "SDL_JOYSTICK_RAWINPUT": "0",
    "SDL_HINT_JOYSTICK_WGI": "0",
    "SDL_JOYSTICK_WGI": "0",
    "SDL_HINT_JOYSTICK_THREAD": "0",
    "SDL_JOYSTICK_THREAD": "0",
    "SDL_HINT_JOYSTICK_RAWINPUT_CORRELATE_XINPUT": "0",
    "SDL_JOYSTICK_RAWINPUT_CORRELATE_XINPUT": "0",
}

# Per-device HIDAPI (SDL 2.0.12+). All off so SDL does not open Moza via HIDAPI.
_HIDAPI_DEVICES = (
    "PS3", "PS4", "PS5", "PS4_RUMBLE", "PS5_RUMBLE",
    "STADIA", "STEAM", "STEAMDECK", "STEAM_HORI",
    "SWITCH", "JOY_CONS", "COMBINE_JOY_CONS", "VERTICAL_JOY_CONS",
    "WII", "XBOX", "XBOX_360", "GAMECUBE", "LUNA", "SHIELD",
    "JOYCON_HOME_LED",
)

for _dev in _HIDAPI_DEVICES:
    SDL_FFB_SAFE_HINTS[f"SDL_HINT_JOYSTICK_HIDAPI_{_dev}"] = "0"
    SDL_FFB_SAFE_HINTS[f"SDL_JOYSTICK_HIDAPI_{_dev}"] = "0"


def apply_sdl_ffb_safe_hints() -> dict[str, str]:
    """Force SDL to leave HIDAPI / raw-input / WGI closed.

    Must run before ``import pygame`` / ``SDL_Init``.
    """
    for key, value in SDL_FFB_SAFE_HINTS.items():
        os.environ[key] = value
    return dict(SDL_FFB_SAFE_HINTS)


def cooperative_level_flags() -> int:
    """``DISCL_NONEXCLUSIVE | DISCL_BACKGROUND``. Never exclusive."""
    flags = DISCL_SAFE
    if flags & DISCL_EXCLUSIVE:
        raise RuntimeError(
            "FFB-safe cooperative level must not include DISCL_EXCLUSIVE")
    if flags & DISCL_FOREGROUND:
        raise RuntimeError(
            "FFB-safe cooperative level must not include DISCL_FOREGROUND")
    return flags


def normalize_wheel_backend(name: str | None) -> str:
    if not name:
        return DEFAULT_WHEEL_BACKEND
    key = str(name).strip().lower()
    key = WHEEL_BACKEND_ALIASES.get(key, key)
    if key not in WHEEL_BACKENDS:
        log(f"[ptt/input] unknown wheel backend {name!r}, "
            f"using {DEFAULT_WHEEL_BACKEND}")
        return DEFAULT_WHEEL_BACKEND
    return key


def resolve_wheel_backend(override: str | None = None) -> str:
    """Return the active wheel backend (setting, then default ``winmm``)."""
    if override:
        return normalize_wheel_backend(override)
    try:
        from ..settings import load_ptt_settings
        return normalize_wheel_backend(
            load_ptt_settings().get("wheel_backend"))
    except Exception:
        return DEFAULT_WHEEL_BACKEND
