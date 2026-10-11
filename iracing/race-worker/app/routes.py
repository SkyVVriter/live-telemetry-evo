"""All /race API routes."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from .auth import Role, require_any, require_app, require_assistant
from .database import get_db
from .models import (
    BalanceNote,
    EventCreate,
    EventOut,
    HealthOut,
    LapCreate,
    LapOut,
    MessageCreate,
    MessageOut,
    SessionCreate,
    SessionOut,
    StateOut,
)
from .rate_limit import ingest_limiter

router = APIRouter(prefix="/race")


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _row_to_session(row) -> SessionOut:
    return SessionOut(
        id=row["id"],
        sim=row["sim"],
        track_id=row["track_id"],
        track_name=row["track_name"],
        track_config=row["track_config"],
        car=row["car"],
        series=row["series"],
        session_type=row["session_type"],
        race_length=row["race_length"],
        driver=row["driver"],
        active=bool(row["active"]),
        created_at=row["created_at"],
        ended_at=row["ended_at"],
    )


def _row_to_lap(row) -> LapOut:
    notes: list[BalanceNote] = []
    raw = row["balance_notes"]
    if raw:
        try:
            notes = [BalanceNote(**n) for n in json.loads(raw)]
        except (json.JSONDecodeError, TypeError):
            pass
    return LapOut(
        id=row["id"],
        session_id=row["session_id"],
        lap=row["lap"],
        lap_time=row["lap_time"],
        position=row["position"],
        class_position=row["class_position"],
        gap_ahead=row["gap_ahead"],
        car_ahead_num=row["car_ahead_num"],
        car_ahead_name=row["car_ahead_name"],
        car_ahead_last_lap=row["car_ahead_last_lap"],
        gap_behind=row["gap_behind"],
        car_behind_num=row["car_behind_num"],
        car_behind_name=row["car_behind_name"],
        car_behind_last_lap=row["car_behind_last_lap"],
        incidents=row["incidents"],
        incident_limit_dt=row["incident_limit_dt"],
        incident_limit_dq=row["incident_limit_dq"],
        fuel_level=row["fuel_level"],
        fuel_per_lap=row["fuel_per_lap"],
        tyre_compound=row["tyre_compound"],
        tyre_wear_fl=row["tyre_wear_fl"],
        tyre_wear_fr=row["tyre_wear_fr"],
        tyre_wear_rl=row["tyre_wear_rl"],
        tyre_wear_rr=row["tyre_wear_rr"],
        pit_this_lap=bool(row["pit_this_lap"]),
        off_track=bool(row["off_track"]),
        balance_notes=notes,
        created_at=row["created_at"],
    )


def _row_to_event(row) -> EventOut:
    payload = None
    raw = row["payload"]
    if raw:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            pass
    return EventOut(
        id=row["id"],
        session_id=row["session_id"],
        event_type=row["event_type"],
        timestamp=row["timestamp"],
        lap=row["lap"],
        lap_dist_pct=row["lap_dist_pct"],
        corner=row["corner"],
        payload=payload,
        created_at=row["created_at"],
    )


def _row_to_message(row) -> MessageOut:
    return MessageOut(
        id=row["id"],
        session_id=row["session_id"],
        text=row["text"],
        priority=row["priority"],
        speak_when=row["speak_when"],
        expires_at=row["expires_at"],
        acked=bool(row["acked"]),
        created_at=row["created_at"],
    )


def _get_session_or_404(db, session_id: str):
    row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    return row


# ---- Health ----

@router.get("/health", response_model=HealthOut)
def health():
    return HealthOut()


# ---- Sessions ----

@router.post("/sessions", response_model=SessionOut, status_code=201)
def create_session(body: SessionCreate, _role: Annotated[Role, Depends(require_app)], request: Request):
    ingest_limiter.check("session")
    sid = uuid.uuid4().hex[:12]
    with get_db() as db:
        db.execute(
            """INSERT INTO sessions (id, sim, track_id, track_name, track_config, car, series,
               session_type, race_length, driver) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (sid, body.sim, body.track_id, body.track_name, body.track_config,
             body.car, body.series, body.session_type.value, body.race_length, body.driver),
        )
        row = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    return _row_to_session(row)


@router.get("/sessions/active", response_model=list[SessionOut])
def get_active_sessions(_role: Annotated[Role, Depends(require_any)]):
    with get_db() as db:
        rows = db.execute("SELECT * FROM sessions WHERE active=1 ORDER BY created_at DESC").fetchall()
    return [_row_to_session(r) for r in rows]


@router.get("/sessions/{session_id}", response_model=SessionOut)
def get_session(session_id: str, _role: Annotated[Role, Depends(require_any)]):
    with get_db() as db:
        row = _get_session_or_404(db, session_id)
    return _row_to_session(row)


# ---- Laps ----

@router.post("/sessions/{session_id}/laps", response_model=LapOut, status_code=201)
def create_lap(session_id: str, body: LapCreate, _role: Annotated[Role, Depends(require_app)], request: Request):
    ingest_limiter.check("lap")
    balance_json = json.dumps([n.model_dump() for n in body.balance_notes]) if body.balance_notes else None
    with get_db() as db:
        _get_session_or_404(db, session_id)
        cur = db.execute(
            """INSERT INTO laps (session_id, lap, lap_time, position, class_position,
               gap_ahead, car_ahead_num, car_ahead_name, car_ahead_last_lap,
               gap_behind, car_behind_num, car_behind_name, car_behind_last_lap,
               incidents, incident_limit_dt, incident_limit_dq,
               fuel_level, fuel_per_lap, tyre_compound,
               tyre_wear_fl, tyre_wear_fr, tyre_wear_rl, tyre_wear_rr,
               pit_this_lap, off_track, balance_notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (session_id, body.lap, body.lap_time, body.position, body.class_position,
             body.gap_ahead, body.car_ahead_num, body.car_ahead_name, body.car_ahead_last_lap,
             body.gap_behind, body.car_behind_num, body.car_behind_name, body.car_behind_last_lap,
             body.incidents, body.incident_limit_dt, body.incident_limit_dq,
             body.fuel_level, body.fuel_per_lap, body.tyre_compound,
             body.tyre_wear_fl, body.tyre_wear_fr, body.tyre_wear_rl, body.tyre_wear_rr,
             int(body.pit_this_lap), int(body.off_track), balance_json),
        )
        row = db.execute("SELECT * FROM laps WHERE id=?", (cur.lastrowid,)).fetchone()
    return _row_to_lap(row)


# ---- Events ----

@router.post("/sessions/{session_id}/events", response_model=EventOut, status_code=201)
def create_event(session_id: str, body: EventCreate, _role: Annotated[Role, Depends(require_app)], request: Request):
    ingest_limiter.check("event")
    payload_json = json.dumps(body.payload) if body.payload else None
    with get_db() as db:
        _get_session_or_404(db, session_id)
        cur = db.execute(
            """INSERT INTO events (session_id, event_type, timestamp, lap, lap_dist_pct, corner, payload)
               VALUES (?,?,?,?,?,?,?)""",
            (session_id, body.event_type.value, body.timestamp, body.lap,
             body.lap_dist_pct, body.corner, payload_json),
        )
        row = db.execute("SELECT * FROM events WHERE id=?", (cur.lastrowid,)).fetchone()
    return _row_to_event(row)


# ---- State (assistant polling) ----

@router.get("/sessions/{session_id}/state", response_model=StateOut)
def get_state(
    session_id: str,
    _role: Annotated[Role, Depends(require_any)],
    since: int | None = Query(None, description="Return events with id > since"),
    n: int = Query(5, ge=1, le=50, description="Number of recent laps"),
):
    with get_db() as db:
        sess_row = _get_session_or_404(db, session_id)
        session_out = _row_to_session(sess_row)

        lap_rows = db.execute(
            "SELECT * FROM laps WHERE session_id=? ORDER BY lap DESC LIMIT ?",
            (session_id, n),
        ).fetchall()
        laps = [_row_to_lap(r) for r in lap_rows]

        if since is not None:
            event_rows = db.execute(
                "SELECT * FROM events WHERE session_id=? AND id>? ORDER BY id",
                (session_id, since),
            ).fetchall()
        else:
            event_rows = db.execute(
                "SELECT * FROM events WHERE session_id=? ORDER BY id",
                (session_id,),
            ).fetchall()
        events = [_row_to_event(r) for r in event_rows]

        pending = db.execute(
            "SELECT COUNT(*) as cnt FROM messages WHERE session_id=? AND acked=0 AND (expires_at IS NULL OR expires_at>?)",
            (session_id, _utcnow()),
        ).fetchone()["cnt"]

    return StateOut(
        session=session_out,
        latest_lap=laps[0] if laps else None,
        last_laps=laps,
        new_events=events,
        pending_messages=pending,
    )


# ---- Messages ----

@router.post("/sessions/{session_id}/messages", response_model=MessageOut, status_code=201)
def create_message(session_id: str, body: MessageCreate, _role: Annotated[Role, Depends(require_assistant)]):
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=body.expires_in_s)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with get_db() as db:
        _get_session_or_404(db, session_id)
        cur = db.execute(
            "INSERT INTO messages (session_id, text, priority, speak_when, expires_at) VALUES (?,?,?,?,?)",
            (session_id, body.text, body.priority.value, body.speak_when.value, expires_at),
        )
        row = db.execute("SELECT * FROM messages WHERE id=?", (cur.lastrowid,)).fetchone()
    return _row_to_message(row)


@router.get("/sessions/{session_id}/messages/next", response_model=list[MessageOut])
def get_next_messages(session_id: str, _role: Annotated[Role, Depends(require_app)]):
    now = _utcnow()
    with get_db() as db:
        _get_session_or_404(db, session_id)
        rows = db.execute(
            """SELECT * FROM messages
               WHERE session_id=? AND acked=0 AND (expires_at IS NULL OR expires_at>?)
               ORDER BY
                 CASE priority WHEN 'urgent' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
                 id""",
            (session_id, now),
        ).fetchall()
    return [_row_to_message(r) for r in rows]


@router.post("/sessions/{session_id}/messages/{message_id}/ack", status_code=204)
def ack_message(session_id: str, message_id: int, _role: Annotated[Role, Depends(require_app)]):
    with get_db() as db:
        _get_session_or_404(db, session_id)
        result = db.execute(
            "UPDATE messages SET acked=1 WHERE id=? AND session_id=?",
            (message_id, session_id),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Message not found")


# ---- End / Export ----

@router.post("/sessions/{session_id}/end", response_model=SessionOut)
def end_session(session_id: str, _role: Annotated[Role, Depends(require_app)]):
    now = _utcnow()
    with get_db() as db:
        _get_session_or_404(db, session_id)
        db.execute("UPDATE sessions SET active=0, ended_at=? WHERE id=?", (now, session_id))
        row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    return _row_to_session(row)


@router.get("/sessions/{session_id}/export")
def export_session(session_id: str, _role: Annotated[Role, Depends(require_any)]):
    with get_db() as db:
        sess = _get_session_or_404(db, session_id)
        laps = db.execute("SELECT * FROM laps WHERE session_id=? ORDER BY lap", (session_id,)).fetchall()
        events = db.execute("SELECT * FROM events WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
        msgs = db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY id", (session_id,)).fetchall()

    return {
        "session": _row_to_session(sess).model_dump(),
        "laps": [_row_to_lap(r).model_dump() for r in laps],
        "events": [_row_to_event(r).model_dump() for r in events],
        "messages": [_row_to_message(r).model_dump() for r in msgs],
    }
