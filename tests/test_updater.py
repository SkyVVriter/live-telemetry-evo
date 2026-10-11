"""Tests for updater: SemVer parsing, channel selection, SHA256 verification."""
import hashlib
from pathlib import Path

import pytest

from live_telemetry_evo.updater import (
    SemVer,
    _parse_sha256sums,
    choose_release,
    parse_semver,
    verify_sha256,
)


class TestSemVer:
    def test_parse_simple(self):
        sv = parse_semver("v1.2.3")
        assert sv == SemVer(1, 2, 3)

    def test_parse_prerelease(self):
        sv = parse_semver("v0.9.0-test.3")
        assert sv == SemVer(0, 9, 0, ("test", "3"))

    def test_parse_no_v(self):
        sv = parse_semver("1.2.3")
        assert sv == SemVer(1, 2, 3)

    def test_parse_invalid(self):
        assert parse_semver("not-a-version") is None

    def test_ordering_major(self):
        assert parse_semver("2.0.0") > parse_semver("1.9.9")

    def test_ordering_minor(self):
        assert parse_semver("1.3.0") > parse_semver("1.2.9")

    def test_ordering_patch(self):
        assert parse_semver("1.2.4") > parse_semver("1.2.3")

    def test_release_beats_prerelease(self):
        assert parse_semver("1.0.0") > parse_semver("1.0.0-test.1")

    def test_prerelease_ordering(self):
        assert parse_semver("1.0.0-test.2") > parse_semver("1.0.0-test.1")
        assert parse_semver("1.0.0-test.10") > parse_semver("1.0.0-test.2")

    def test_equality(self):
        assert parse_semver("v1.0.0") == parse_semver("1.0.0")


class TestChooseRelease:
    def _releases(self):
        return [
            {"tag_name": "v1.0.0"},
            {"tag_name": "v1.1.0"},
            {"tag_name": "v1.2.0-test.1"},
            {"tag_name": "v1.2.0-test.2"},
            {"tag_name": "v1.2.0"},
            {"tag_name": "v0.9.0"},
        ]

    def test_prod_channel(self):
        current = parse_semver("v1.0.0")
        rel = choose_release(self._releases(), "prod", current)
        assert rel is not None
        assert rel["tag_name"] == "v1.2.0"

    def test_prod_up_to_date(self):
        current = parse_semver("v1.2.0")
        rel = choose_release(self._releases(), "prod", current)
        assert rel is None

    def test_test_channel(self):
        current = parse_semver("v1.1.0")
        rel = choose_release(self._releases(), "test", current)
        assert rel is not None
        assert rel["tag_name"] == "v1.2.0-test.2"

    def test_test_channel_older(self):
        current = parse_semver("v1.2.0-test.2")
        rel = choose_release(self._releases(), "test", current)
        assert rel is None

    def test_prod_skips_prereleases(self):
        releases = [
            {"tag_name": "v2.0.0-test.1"},
            {"tag_name": "v2.0.0-test.2"},
        ]
        current = parse_semver("v1.0.0")
        rel = choose_release(releases, "prod", current)
        assert rel is None

    def test_empty_releases(self):
        current = parse_semver("v1.0.0")
        assert choose_release([], "prod", current) is None


class TestSHA256:
    def test_verify_matching(self, tmp_path):
        content = b"hello world test content"
        path = tmp_path / "test.exe"
        path.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert verify_sha256(path, expected) is True

    def test_verify_mismatch(self, tmp_path):
        path = tmp_path / "test.exe"
        path.write_bytes(b"some content")
        assert verify_sha256(path, "0" * 64) is False

    def test_verify_case_insensitive(self, tmp_path):
        content = b"test"
        path = tmp_path / "test.exe"
        path.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest().upper()
        assert verify_sha256(path, expected) is True

    def test_parse_sha256sums(self):
        text = "abc123  LiveTelemetryEvo-1.0.0.exe\ndef456  other.txt\n"
        result = _parse_sha256sums(text)
        assert result == {
            "LiveTelemetryEvo-1.0.0.exe": "abc123",
            "other.txt": "def456",
        }

    def test_parse_sha256sums_empty(self):
        assert _parse_sha256sums("") == {}
