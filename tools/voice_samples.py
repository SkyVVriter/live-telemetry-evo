#!/usr/bin/env python3
"""Prepare voice samples for ElevenLabs instant voice cloning.

Reads a YAML or JSON manifest describing source audio (YouTube URLs or
local files), cuts specified segments, normalizes loudness, optionally
applies noise reduction, and exports mono 44.1 kHz WAV/MP3 files into
per-profile output folders.

**This tool does NOT upload anything to ElevenLabs.** The user must
upload the prepared samples through the ElevenLabs web UI or API
themselves. Users must have rights and consent for all source audio
per ElevenLabs' terms of service.

Requirements (must be on PATH):
  - yt-dlp   (YouTube downloads)
  - ffmpeg   (audio processing)

Manifest format (YAML or JSON)::

    profiles:
      - name: "MyEngineer"
        voice_id: "optional-existing-id"
        sources:
          - url: "https://www.youtube.com/watch?v=..."
            segments:
              - [10.0, 25.5]
              - [42.0, 58.0]
          - file: "/path/to/local/recording.wav"
            segments:
              - [0.0, 30.0]
        noise_reduce: true

Usage::

    python tools/voice_samples.py manifest.yaml --output ./voice_output
    python tools/voice_samples.py manifest.json --output ./voice_output --format mp3
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

_SAMPLE_RATE = 44100
_CHANNELS = 1
_LOUDNESS_TARGET = -16.0  # LUFS
_ELEVENLABS_RECOMMENDED_MIN_S = 60.0
_ELEVENLABS_RECOMMENDED_IDEAL_S = 120.0


def _load_manifest(path: Path) -> dict:
    """Load a YAML or JSON manifest file."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-untyped]
            return yaml.safe_load(text)
        except ImportError:
            print("WARNING: PyYAML not installed, trying JSON parse...")
            return json.loads(text)
    return json.loads(text)


def _run(args: list[str], check: bool = True,
         capture: bool = True, **kwargs) -> subprocess.CompletedProcess:
    """Run a subprocess with sensible defaults."""
    return subprocess.run(
        args, check=check,
        capture_output=capture,
        text=True,
        **kwargs,
    )


def download_audio(url: str, output_dir: Path) -> Path:
    """Download audio from a YouTube URL using yt-dlp.

    Returns the path to the downloaded audio file.
    """
    output_template = str(output_dir / "%(id)s.%(ext)s")
    _run([
        "yt-dlp",
        "--extract-audio",
        "--audio-format", "wav",
        "--audio-quality", "0",
        "--output", output_template,
        "--no-playlist",
        url,
    ])
    wavs = list(output_dir.glob("*.wav"))
    if not wavs:
        raise FileNotFoundError(f"yt-dlp produced no WAV output for {url}")
    return max(wavs, key=lambda p: p.stat().st_mtime)


def cut_segment(input_path: Path, start: float, end: float,
                output_path: Path) -> Path:
    """Cut a segment from an audio file using ffmpeg."""
    duration = end - start
    _run([
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-ss", f"{start:.3f}",
        "-t", f"{duration:.3f}",
        "-c", "copy",
        str(output_path),
    ])
    return output_path


def normalize_loudness(input_path: Path, output_path: Path,
                       target_lufs: float = _LOUDNESS_TARGET) -> Path:
    """Normalize audio loudness to target LUFS using ffmpeg loudnorm."""
    _run([
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-af", f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11",
        "-ar", str(_SAMPLE_RATE),
        "-ac", str(_CHANNELS),
        str(output_path),
    ])
    return output_path


def apply_noise_reduction(input_path: Path, output_path: Path) -> Path:
    """Apply basic noise reduction using ffmpeg's highpass+lowpass filters.

    This is a simple approach; for production quality, consider dedicated
    noise reduction tools like RNNoise (via ffmpeg's arnndn filter if
    available) or external tools.
    """
    _run([
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-af", "highpass=f=80,lowpass=f=12000,afftdn=nf=-20",
        str(output_path),
    ])
    return output_path


def convert_to_format(input_path: Path, output_path: Path,
                      fmt: str = "wav") -> Path:
    """Convert audio to the target format (wav or mp3), mono 44.1kHz."""
    codec_args: list[str] = []
    if fmt == "mp3":
        codec_args = ["-codec:a", "libmp3lame", "-b:a", "192k"]
    else:
        codec_args = ["-codec:a", "pcm_s16le"]
    _run([
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-ar", str(_SAMPLE_RATE),
        "-ac", str(_CHANNELS),
        *codec_args,
        str(output_path),
    ])
    return output_path


def get_duration(path: Path) -> float:
    """Get the duration of an audio file in seconds using ffprobe."""
    result = _run([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        str(path),
    ])
    return float(result.stdout.strip())


def process_profile(profile: dict, output_base: Path,
                    out_format: str = "wav") -> dict:
    """Process a single voice profile from the manifest.

    Returns a report dict with profile name, segment count, total duration,
    and any errors.
    """
    name = profile.get("name", "unnamed")
    noise_reduce = profile.get("noise_reduce", False)
    profile_dir = output_base / _safe_name(name)
    profile_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "name": name,
        "voice_id": profile.get("voice_id", ""),
        "segments": 0,
        "total_duration_s": 0.0,
        "errors": [],
        "files": [],
    }

    segment_idx = 0
    with tempfile.TemporaryDirectory(prefix="voice_samples_") as tmpdir:
        tmp = Path(tmpdir)
        for source in profile.get("sources", []):
            try:
                source_path = _resolve_source(source, tmp)
            except Exception as e:
                report["errors"].append(f"Source error: {e}")
                continue

            for seg in source.get("segments", []):
                if not isinstance(seg, (list, tuple)) or len(seg) < 2:
                    report["errors"].append(f"Invalid segment: {seg}")
                    continue
                start, end = float(seg[0]), float(seg[1])
                segment_idx += 1
                seg_name = f"segment_{segment_idx:03d}"

                try:
                    cut_path = tmp / f"{seg_name}_cut.wav"
                    cut_segment(source_path, start, end, cut_path)

                    if noise_reduce:
                        nr_path = tmp / f"{seg_name}_nr.wav"
                        apply_noise_reduction(cut_path, nr_path)
                        cut_path = nr_path

                    norm_path = tmp / f"{seg_name}_norm.wav"
                    normalize_loudness(cut_path, norm_path)

                    ext = out_format.lower()
                    final_path = profile_dir / f"{seg_name}.{ext}"
                    convert_to_format(norm_path, final_path, fmt=ext)

                    dur = get_duration(final_path)
                    report["segments"] += 1
                    report["total_duration_s"] += dur
                    report["files"].append(str(final_path))

                except Exception as e:
                    report["errors"].append(
                        f"Segment {segment_idx} ({start}-{end}): {e}")

    return report


def _resolve_source(source: dict, tmp_dir: Path) -> Path:
    """Resolve a source entry to a local audio file path."""
    if "file" in source:
        path = Path(source["file"])
        if not path.exists():
            raise FileNotFoundError(f"Local file not found: {path}")
        return path
    if "url" in source:
        return download_audio(source["url"], tmp_dir)
    raise ValueError("Source must have 'file' or 'url' key")


def _safe_name(name: str) -> str:
    """Sanitize a profile name for use as a directory name."""
    return "".join(c if c.isalnum() or c in "._- " else "_" for c in name).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare voice samples for ElevenLabs cloning",
        epilog=(
            "NOTE: This tool does NOT upload anything to ElevenLabs. "
            "Users must have rights and consent for all source audio "
            "per ElevenLabs' terms of service."
        ),
    )
    parser.add_argument("manifest", type=Path,
                        help="YAML or JSON manifest file")
    parser.add_argument("--output", "-o", type=Path, default=Path("voice_output"),
                        help="Output directory (default: ./voice_output)")
    parser.add_argument("--format", "-f", choices=("wav", "mp3"), default="wav",
                        help="Output format (default: wav)")
    args = parser.parse_args(argv)

    if not args.manifest.exists():
        print(f"ERROR: manifest not found: {args.manifest}", file=sys.stderr)
        return 1

    manifest = _load_manifest(args.manifest)
    profiles = manifest.get("profiles", [])
    if not profiles:
        print("ERROR: no profiles found in manifest", file=sys.stderr)
        return 1

    args.output.mkdir(parents=True, exist_ok=True)
    print(f"Processing {len(profiles)} profile(s)...\n")

    for profile in profiles:
        report = process_profile(profile, args.output, args.format)
        _print_report(report)

    return 0


def _print_report(report: dict) -> None:
    name = report["name"]
    dur = report["total_duration_s"]
    segs = report["segments"]
    print(f"Profile: {name}")
    if report["voice_id"]:
        print(f"  Voice ID: {report['voice_id']}")
    print(f"  Segments: {segs}")
    print(f"  Total clean duration: {dur:.1f}s")

    if dur < _ELEVENLABS_RECOMMENDED_MIN_S:
        print(f"  ⚠ Below recommended minimum ({_ELEVENLABS_RECOMMENDED_MIN_S:.0f}s)")
    elif dur < _ELEVENLABS_RECOMMENDED_IDEAL_S:
        print(f"  ✓ Meets minimum ({_ELEVENLABS_RECOMMENDED_MIN_S:.0f}s), "
              f"ideal is {_ELEVENLABS_RECOMMENDED_IDEAL_S:.0f}s+")
    else:
        print(f"  ✓ Meets recommended duration ({_ELEVENLABS_RECOMMENDED_IDEAL_S:.0f}s+)")

    for err in report.get("errors", []):
        print(f"  ERROR: {err}")
    print()


if __name__ == "__main__":
    raise SystemExit(main())
