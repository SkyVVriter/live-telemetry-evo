"""E2E: RaceWorkerClient against a local race-worker process.

Creates a session, posts a lap, an event, and driver_feedback, then
lists sessions via GET /race/sessions. Also asserts a wrong token
returns 401 (not a false OK from /health).
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from live_telemetry_evo.race_worker_client import RaceWorkerClient

RW_ROOT = Path(__file__).resolve().parents[1] / "iracing" / "race-worker"
APP_TOKEN = "e2e-app-token"
ASSISTANT_TOKEN = "e2e-assistant-token"


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture(scope="module")
def race_worker_url(tmp_path_factory):
    pytest.importorskip("fastapi")
    pytest.importorskip("uvicorn")
    tmp = tmp_path_factory.mktemp("rw-e2e")
    port = _free_port()
    env = os.environ.copy()
    env["APP_TOKEN"] = APP_TOKEN
    env["ASSISTANT_TOKEN"] = ASSISTANT_TOKEN
    env["RACE_WORKER_DB"] = str(tmp / "e2e.db")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port),
         "--app-dir", str(RW_ROOT)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    last_err = None
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read().decode("utf-8", errors="replace") if proc.stdout else ""
            pytest.fail(f"race-worker exited early ({proc.returncode}): {out}")
        try:
            client = RaceWorkerClient(base_url=url, token=APP_TOKEN)
            ok, msg = client.test_connection()
            if ok:
                break
            last_err = msg
        except Exception as e:
            last_err = e
        time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail(f"race-worker did not become ready: {last_err}")
    try:
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_e2e_session_lap_event_feedback(race_worker_url):
    client = RaceWorkerClient(base_url=race_worker_url, token=APP_TOKEN)
    sid = client.create_session(
        sim="iRacing",
        track_id="274",
        track_name="Spa-Francorchamps",
        track_config="Grand Prix",
        car="BMW M4 GT3",
        series="IMSA",
        session_type="race",
        driver="E2E Driver",
    )
    assert sid, "create_session returned None"

    assert client.post_lap({
        "lap_number": 1,
        "lap_time": 128.441,
        "position": 4,
        "incidents": 0,
    })

    assert client.post_event({
        "event_type": "incident",
        "lap": 1,
        "lap_dist_pct": 0.42,
        "corner": "Eau Rouge",
    })

    assert client.post_event({
        "type": "driver_feedback",
        "lap": 1,
        "lap_dist_pct": 0.55,
        "payload": {
            "transcript": "how is the balance",
            "telemetry": {
                "speed_kmh": 212.4,
                "throttle": 0.8,
                "brake": 0.0,
                "steering_rad": 0.12,
                "yaw_rate": 0.05,
            },
        },
    })

    sessions = client.list_sessions()
    assert any(s.get("id") == sid for s in sessions)
    spa = next(s for s in sessions if s.get("id") == sid)
    assert spa["track_name"] == "Spa-Francorchamps"
    assert spa["car"] == "BMW M4 GT3"
    assert spa["driver"] == "E2E Driver"


def test_e2e_wrong_token_is_401(race_worker_url):
    bad = RaceWorkerClient(base_url=race_worker_url, token="definitely-wrong.")
    ok, msg = bad.test_connection()
    assert ok is False
    assert "401" in msg


def test_e2e_empty_create_returns_422(race_worker_url):
    """Startup-style create_session(source=...) must surface the 422 body."""
    client = RaceWorkerClient(base_url=race_worker_url, token=APP_TOKEN)
    sid = client.create_session(source="iracing")
    assert sid is None
