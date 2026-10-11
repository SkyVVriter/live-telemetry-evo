#!/usr/bin/env python3
"""Mock iRacing app that simulates a race: creates a session, pushes laps and
events, polls for TTS messages, then ends the session and exports data.

Usage:
    python scripts/mock_app.py [--base-url http://127.0.0.1:8091]
                               [--app-token TOKEN]
                               [--assistant-token TOKEN]
                               [--laps 10]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time

import httpx

CORNERS = ["T1", "T2", "T3", "T4", "T5", "T6", "T8", "T10", "T12", "T15"]
EVENT_TYPES = ["incident", "slide", "spin", "overtake", "overtaken"]


def main():
    parser = argparse.ArgumentParser(description="Mock iRacing app for race-worker")
    parser.add_argument("--base-url", default="http://127.0.0.1:8091")
    parser.add_argument("--app-token", default="changeme-app-token")
    parser.add_argument("--assistant-token", default="changeme-assistant-token")
    parser.add_argument("--laps", type=int, default=10)
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    app_h = {"Authorization": f"Bearer {args.app_token}"}
    asst_h = {"Authorization": f"Bearer {args.assistant_token}"}

    with httpx.Client(base_url=base, timeout=10) as c:
        # Health
        r = c.get("/race/health")
        print(f"Health: {r.json()}")

        # Create session
        r = c.post("/race/sessions", json={
            "sim": "iRacing",
            "track_id": "274",
            "track_name": "Spa-Francorchamps",
            "track_config": "Grand Prix",
            "car": "Dallara P217 LMP2",
            "series": "IMSA Endurance Series",
            "session_type": "race",
            "race_length": f"{args.laps} laps",
            "driver": "MockDriver",
        }, headers=app_h)
        r.raise_for_status()
        session = r.json()
        sid = session["id"]
        print(f"Session created: {sid}")

        # Queue a message (as assistant)
        c.post(f"/race/sessions/{sid}/messages", json={
            "text": "Fuel target is 2.1 litres per lap, stay on plan",
            "priority": "normal",
            "expires_in_s": 300,
            "speak_when": "straight",
        }, headers=asst_h)
        print("Queued strategy message")

        incidents = 0
        for lap_num in range(1, args.laps + 1):
            base_time = 128.5 + random.uniform(-2.0, 3.0)
            new_inc = random.choice([0, 0, 0, 0, 1, 2])
            incidents += new_inc

            lap_data = {
                "lap": lap_num,
                "lap_time": round(base_time, 3),
                "position": random.randint(1, 20),
                "class_position": random.randint(1, 8),
                "gap_ahead": round(random.uniform(0.3, 5.0), 3),
                "car_ahead_num": str(random.randint(1, 99)),
                "car_ahead_name": "Competitor A",
                "car_ahead_last_lap": round(base_time + random.uniform(-1, 1), 3),
                "gap_behind": round(random.uniform(0.3, 5.0), 3),
                "car_behind_num": str(random.randint(1, 99)),
                "car_behind_name": "Competitor B",
                "car_behind_last_lap": round(base_time + random.uniform(-1, 1), 3),
                "incidents": incidents,
                "incident_limit_dt": 17,
                "incident_limit_dq": 25,
                "fuel_level": round(80.0 - lap_num * 2.1, 1),
                "fuel_per_lap": 2.1,
                "tyre_compound": "medium",
                "pit_this_lap": lap_num == args.laps // 2,
                "off_track": new_inc > 0,
                "balance_notes": [
                    {"corner": random.choice(CORNERS), "phase": "entry",
                     "type": random.choice(["understeer", "oversteer"]),
                     "magnitude": round(random.uniform(2, 8), 1)},
                ] if random.random() > 0.5 else [],
            }

            r = c.post(f"/race/sessions/{sid}/laps", json=lap_data, headers=app_h)
            r.raise_for_status()
            print(f"  Lap {lap_num}: {base_time:.3f}s  P{lap_data['position']}  inc={incidents}")

            # Random events
            if random.random() > 0.6:
                evt_type = random.choice(EVENT_TYPES)
                c.post(f"/race/sessions/{sid}/events", json={
                    "event_type": evt_type,
                    "lap": lap_num,
                    "lap_dist_pct": round(random.random(), 3),
                    "corner": random.choice(CORNERS),
                    "payload": {"detail": f"auto-generated {evt_type}"},
                }, headers=app_h)
                print(f"    Event: {evt_type}")

            # Pit events
            if lap_data["pit_this_lap"]:
                c.post(f"/race/sessions/{sid}/events", json={
                    "event_type": "pit_in", "lap": lap_num,
                }, headers=app_h)
                c.post(f"/race/sessions/{sid}/events", json={
                    "event_type": "pit_out", "lap": lap_num,
                }, headers=app_h)
                print("    Pit stop")

            # Poll messages (as app)
            msgs = c.get(f"/race/sessions/{sid}/messages/next", headers=app_h).json()
            for m in msgs:
                print(f"    TTS [{m['priority']}]: {m['text']}")
                c.post(f"/race/sessions/{sid}/messages/{m['id']}/ack", headers=app_h)

            time.sleep(0.05)

        # Queue another message before ending
        c.post(f"/race/sessions/{sid}/messages", json={
            "text": "Great drive, well done!",
            "priority": "low",
            "expires_in_s": 60,
            "speak_when": "now",
        }, headers=asst_h)

        # Simulate assistant polling state
        state = c.get(f"/race/sessions/{sid}/state?n=3", headers=asst_h).json()
        print(f"\nAssistant state poll:")
        print(f"  Last lap: {state['latest_lap']['lap'] if state['latest_lap'] else 'none'}")
        print(f"  Laps returned: {len(state['last_laps'])}")
        print(f"  Events: {len(state['new_events'])}")
        print(f"  Pending messages: {state['pending_messages']}")

        # End session
        c.post(f"/race/sessions/{sid}/end", headers=app_h)
        print(f"\nSession {sid} ended")

        # Export
        export = c.get(f"/race/sessions/{sid}/export", headers=asst_h).json()
        print(f"Export: {len(export['laps'])} laps, {len(export['events'])} events, {len(export['messages'])} messages")


if __name__ == "__main__":
    main()
