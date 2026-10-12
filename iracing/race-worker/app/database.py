"""SQLite database with WAL mode for race-worker persistence."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

DB_PATH = os.environ.get("RACE_WORKER_DB", "/opt/race-worker/data/race.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    sim         TEXT NOT NULL,
    track_id    TEXT NOT NULL,
    track_name  TEXT NOT NULL,
    track_config TEXT NOT NULL DEFAULT '',
    car         TEXT NOT NULL,
    series      TEXT NOT NULL DEFAULT '',
    session_type TEXT NOT NULL DEFAULT 'race',
    race_length TEXT NOT NULL DEFAULT '',
    driver      TEXT NOT NULL DEFAULT '',
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ended_at    TEXT
);

CREATE TABLE IF NOT EXISTS laps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    lap             INTEGER NOT NULL,
    lap_time        REAL,
    position        INTEGER,
    class_position  INTEGER,
    gap_ahead       REAL,
    car_ahead_num   TEXT,
    car_ahead_name  TEXT,
    car_ahead_last_lap REAL,
    gap_behind      REAL,
    car_behind_num  TEXT,
    car_behind_name TEXT,
    car_behind_last_lap REAL,
    incidents       INTEGER DEFAULT 0,
    incident_limit_dt INTEGER,
    incident_limit_dq INTEGER,
    fuel_level      REAL,
    fuel_per_lap    REAL,
    tyre_compound   TEXT,
    tyre_wear_fl    REAL,
    tyre_wear_fr    REAL,
    tyre_wear_rl    REAL,
    tyre_wear_rr    REAL,
    pit_this_lap    INTEGER DEFAULT 0,
    off_track       INTEGER DEFAULT 0,
    out_lap         INTEGER DEFAULT 0,
    balance_notes   TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    event_type      TEXT NOT NULL,
    timestamp       TEXT,
    lap             INTEGER,
    lap_dist_pct    REAL,
    corner          TEXT,
    payload         TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    text            TEXT NOT NULL,
    priority        TEXT NOT NULL DEFAULT 'normal',
    speak_when      TEXT NOT NULL DEFAULT 'straight',
    expires_at      TEXT,
    acked           INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_laps_session ON laps(session_id, lap);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, acked, expires_at);
"""


def _ensure_dir(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def init_db(path: str | None = None) -> None:
    p = path or DB_PATH
    _ensure_dir(p)
    conn = sqlite3.connect(p)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.commit()
    conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first schema (existing DBs)."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(laps)")}
    if "out_lap" not in cols:
        conn.execute(
            "ALTER TABLE laps ADD COLUMN out_lap INTEGER NOT NULL DEFAULT 0"
        )


@contextmanager
def get_db(path: str | None = None) -> Generator[sqlite3.Connection, None, None]:
    p = path or DB_PATH
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
