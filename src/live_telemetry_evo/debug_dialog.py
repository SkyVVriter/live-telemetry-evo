"""Debug / diagnostics dialog: 'Отладка'.

Shows app version, log file path, device diagnostics, and a
one-click "copy report" that puts a full diagnostic snapshot into the
clipboard — everything a developer needs to debug a user's setup
without a remote session. API keys and tokens are masked.
"""
from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .logbook import get_log_dir, get_log_path, log, read_last_lines
from .ptt.input_binding import run_diagnostics as joy_diagnostics
from .ptt.recorder import list_input_devices


def _get_version() -> str:
    try:
        from .updater import get_current_version
        v = get_current_version()
        if v:
            return v
    except Exception:
        pass
    try:
        from importlib.metadata import version
        return version("live-telemetry-evo")
    except Exception:
        return "unknown"


def _mask_secret(value: str) -> str:
    """Mask an API key / token for safe display."""
    if not value or len(value) < 8:
        return "***" if value else "(empty)"
    return value[:4] + "…" + value[-4:]


def build_report() -> str:
    """Build a full diagnostic report string (no secrets)."""
    lines: list[str] = []
    lines.append("=== Live Telemetry Evo — Diagnostic Report ===")
    lines.append("")

    lines.append(f"Version:  {_get_version()}")
    lines.append(f"Python:   {sys.version.split()[0]}")
    lines.append(f"OS:       {platform.platform()}")
    lines.append(f"Frozen:   {getattr(sys, 'frozen', False)}")
    lines.append(f"Exe:      {sys.executable}")
    log_path = get_log_path()
    lines.append(f"Log file: {log_path or '(not opened)'}")
    lines.append("")

    lines.append("--- Joystick Backends ---")
    lines.append(joy_diagnostics())

    lines.append("--- Audio Input Devices ---")
    devs = list_input_devices()
    if devs:
        for d in devs:
            ha = f" [{d.hostapi_name}]" if d.hostapi_name else ""
            dflt = " (default)" if d.is_default else ""
            lines.append(f"  {d.name}{ha}{dflt}")
    else:
        lines.append("  (none detected or sounddevice unavailable)")
    lines.append("")

    lines.append("--- Race Worker ---")
    try:
        from .settings import load_race_worker_settings
        rw = load_race_worker_settings()
        lines.append(f"  Enabled: {rw['enabled']}")
        lines.append(f"  URL:     {rw['url'] or '(empty)'}")
        lines.append(f"  Token:   {_mask_secret(rw['app_token'])}")
        if rw["enabled"] and rw["url"]:
            try:
                from .race_worker_client import RaceWorkerClient
                client = RaceWorkerClient(base_url=rw["url"],
                                          token=rw["app_token"])
                ok, msg = client.test_connection()
                lines.append(f"  Health:  {'OK' if ok else 'FAIL'} — {msg}")
            except Exception as e:
                lines.append(f"  Health:  ERROR — {e}")
    except Exception:
        lines.append("  (settings unavailable)")
    lines.append("")

    lines.append("--- TTS / STT Config ---")
    try:
        from .tts.settings_dialog import load_tts_settings
        tts = load_tts_settings()
        lines.append(f"  TTS engine: {tts['engine']}")
        lines.append(f"  TTS voice:  {tts['voice']}")
    except Exception:
        pass
    try:
        from .settings import load_ptt_settings, _read
        ptt = load_ptt_settings()
        lines.append(f"  STT engine: {ptt['stt_engine']}")
        lines.append(f"  STT key:    {_mask_secret(ptt.get('whisper_api_key', ''))}")
        data = _read()
        el = data.get("elevenlabs", {})
        if isinstance(el, dict):
            lines.append(f"  EL key:     {_mask_secret(str(el.get('api_key', '')))}")
    except Exception:
        pass
    lines.append("")

    lines.append("--- Last 200 Log Lines ---")
    log_lines = read_last_lines(200)
    if log_lines:
        for ll in log_lines:
            lines.append(f"  {ll}")
    else:
        lines.append("  (no log lines)")
    lines.append("")
    lines.append("=== End of Report ===")
    return "\n".join(lines)


_DARK_STYLESHEET = """
    QDialog { background-color: #161b22; color: #c9d1d9; }
    QLabel { color: #c9d1d9; font-size: 13px; }
    QPlainTextEdit {
        background-color: #0d1117; color: #8b949e;
        font-family: monospace; font-size: 11px;
        border: 1px solid #30363d; border-radius: 4px;
    }
    QPushButton {
        background-color: #21262d; color: #c9d1d9;
        border: 1px solid #30363d; border-radius: 4px;
        padding: 6px 14px; font-weight: bold;
    }
    QPushButton:hover { background-color: #30363d; color: #f0f6fc; }
"""


class DebugDialog(QDialog):
    """Debug / diagnostics dialog."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Отладка — Diagnostics")
        self.setMinimumSize(620, 500)
        self.setStyleSheet(_DARK_STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        header = QLabel("<b>Отладка / Diagnostics</b>")
        header.setStyleSheet("font-size: 15px; color: #58a6ff;")
        layout.addWidget(header)

        version = _get_version()
        log_path = get_log_path()

        info = QLabel(
            f"Версия: <b>{version}</b><br>"
            f"Лог: <code>{log_path or 'не открыт'}</code>"
        )
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setWordWrap(True)
        layout.addWidget(info)

        btn_row = QHBoxLayout()
        open_logs_btn = QPushButton("Открыть папку логов")
        open_logs_btn.clicked.connect(self._open_logs_folder)
        btn_row.addWidget(open_logs_btn)
        copy_btn = QPushButton("Копировать отчёт")
        copy_btn.clicked.connect(self._copy_report)
        btn_row.addWidget(copy_btn)
        self._copy_status = QLabel("")
        self._copy_status.setStyleSheet("color: #2ea043; font-size: 11px;")
        btn_row.addWidget(self._copy_status)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._report_text = QPlainTextEdit()
        self._report_text.setReadOnly(True)
        layout.addWidget(self._report_text)

        self._report = build_report()
        self._report_text.setPlainText(self._report)
        log("[debug] diagnostics dialog opened")

    def _open_logs_folder(self) -> None:
        from PySide6.QtCore import QUrl
        log_dir = get_log_dir()
        if log_dir and log_dir.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_dir)))
        else:
            from .paths import app_data_dir
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(app_data_dir())))

    def _copy_report(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._report)
            self._copy_status.setText("✓ Скопировано")
            log("[debug] report copied to clipboard")
