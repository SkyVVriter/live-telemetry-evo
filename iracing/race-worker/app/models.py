"""Pydantic models for request/response validation."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# --- Enums ---

class SessionType(str, Enum):
    practice = "practice"
    qualify = "qualify"
    race = "race"
    warmup = "warmup"


class EventType(str, Enum):
    incident = "incident"
    slide = "slide"
    spin = "spin"
    plow = "plow"
    overtake = "overtake"
    overtaken = "overtaken"
    pit_in = "pit_in"
    pit_out = "pit_out"
    flag_black = "flag_black"
    flag_blue = "flag_blue"
    flag_dq = "flag_dq"
    driver_feedback = "driver_feedback"


class Priority(str, Enum):
    low = "low"
    normal = "normal"
    urgent = "urgent"


class SpeakWhen(str, Enum):
    straight = "straight"
    now = "now"


class BalancePhase(str, Enum):
    entry = "entry"
    mid = "mid"
    exit = "exit"


class BalanceType(str, Enum):
    understeer = "understeer"
    oversteer = "oversteer"
    slide = "slide"


# --- Request models ---

class SessionCreate(BaseModel):
    sim: str
    track_id: str
    track_name: str
    track_config: str = ""
    car: str
    series: str = ""
    session_type: SessionType = SessionType.race
    race_length: str = ""
    driver: str = ""


class BalanceNote(BaseModel):
    corner: str
    phase: BalancePhase
    type: BalanceType
    magnitude: float = Field(ge=0, le=10, default=5.0)


class LapCreate(BaseModel):
    lap: int = Field(ge=0)
    lap_time: float | None = None
    position: int | None = None
    class_position: int | None = None
    gap_ahead: float | None = None
    car_ahead_num: str | None = None
    car_ahead_name: str | None = None
    car_ahead_last_lap: float | None = None
    gap_behind: float | None = None
    car_behind_num: str | None = None
    car_behind_name: str | None = None
    car_behind_last_lap: float | None = None
    incidents: int = 0
    incident_limit_dt: int | None = None
    incident_limit_dq: int | None = None
    fuel_level: float | None = None
    fuel_per_lap: float | None = None
    tyre_compound: str | None = None
    tyre_wear_fl: float | None = None
    tyre_wear_fr: float | None = None
    tyre_wear_rl: float | None = None
    tyre_wear_rr: float | None = None
    pit_this_lap: bool = False
    off_track: bool = False
    balance_notes: list[BalanceNote] = Field(default_factory=list)


class EventCreate(BaseModel):
    event_type: EventType
    timestamp: str | None = None
    lap: int | None = None
    lap_dist_pct: float | None = None
    corner: str | None = None
    payload: dict[str, Any] | None = None


class MessageCreate(BaseModel):
    text: str = Field(max_length=300)
    priority: Priority = Priority.normal
    expires_in_s: int = Field(default=120, ge=1, le=3600)
    speak_when: SpeakWhen = SpeakWhen.straight


# --- Response models ---

class SessionOut(BaseModel):
    id: str
    sim: str
    track_id: str
    track_name: str
    track_config: str
    car: str
    series: str
    session_type: str
    race_length: str
    driver: str
    active: bool
    created_at: str
    ended_at: str | None = None


class LapOut(BaseModel):
    id: int
    session_id: str
    lap: int
    lap_time: float | None = None
    position: int | None = None
    class_position: int | None = None
    gap_ahead: float | None = None
    car_ahead_num: str | None = None
    car_ahead_name: str | None = None
    car_ahead_last_lap: float | None = None
    gap_behind: float | None = None
    car_behind_num: str | None = None
    car_behind_name: str | None = None
    car_behind_last_lap: float | None = None
    incidents: int = 0
    incident_limit_dt: int | None = None
    incident_limit_dq: int | None = None
    fuel_level: float | None = None
    fuel_per_lap: float | None = None
    tyre_compound: str | None = None
    tyre_wear_fl: float | None = None
    tyre_wear_fr: float | None = None
    tyre_wear_rl: float | None = None
    tyre_wear_rr: float | None = None
    pit_this_lap: bool = False
    off_track: bool = False
    balance_notes: list[BalanceNote] = Field(default_factory=list)
    created_at: str = ""


class EventOut(BaseModel):
    id: int
    session_id: str
    event_type: str
    timestamp: str | None = None
    lap: int | None = None
    lap_dist_pct: float | None = None
    corner: str | None = None
    payload: dict[str, Any] | None = None
    created_at: str = ""


class MessageOut(BaseModel):
    id: int
    session_id: str
    text: str
    priority: str
    speak_when: str
    expires_at: str | None = None
    acked: bool = False
    created_at: str = ""


class HealthOut(BaseModel):
    status: str = "ok"
    version: str = "0.1.0"


class StateOut(BaseModel):
    session: SessionOut
    latest_lap: LapOut | None = None
    last_laps: list[LapOut] = Field(default_factory=list)
    new_events: list[EventOut] = Field(default_factory=list)
    pending_messages: int = 0
