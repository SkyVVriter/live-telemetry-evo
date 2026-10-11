"""Shared fixtures for the test suite.

Mocks PySide6 so tests can run on headless Linux without the Qt runtime.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Mock PySide6 globally for all tests
for mod_name in ("PySide6", "PySide6.QtCore", "PySide6.QtWidgets",
                 "PySide6.QtGui", "PySide6.QtOpenGL"):
    sys.modules.setdefault(mod_name, MagicMock())

import pytest


@pytest.fixture
def sample_csv_path():
    return Path(__file__).parent / "sample_garage61.csv"


@pytest.fixture
def track_profile_path(tmp_path):
    import json
    profile = {
        "track_id": "274",
        "track_name": "Test Track",
        "corners": [
            {"name": "T1", "start_pct": 0.04, "apex_pct": 0.09,
             "end_pct": 0.14, "focus": True},
            {"name": "T2", "start_pct": 0.38, "apex_pct": 0.44,
             "end_pct": 0.52, "focus": False},
        ],
    }
    path = tmp_path / "track_profile.json"
    path.write_text(json.dumps(profile), encoding="utf-8")
    return path
