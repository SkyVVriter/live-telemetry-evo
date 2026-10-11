"""TTS Settings dialog for voice engine selection and configuration."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..logbook import log
from ..settings import _read, _write
from .base import Priority
from .registry import get_engine, list_engines


def load_tts_settings() -> dict:
    data = _read()
    tts = data.get("tts", {})
    if not isinstance(tts, dict):
        tts = {}
    return {
        "engine": str(tts.get("engine", "edge-tts")),
        "voice": str(tts.get("voice", "ru-RU-DmitryNeural")),
        "rate": str(tts.get("rate", "+0%")),
        "volume": str(tts.get("volume", "+0%")),
    }


def save_tts_settings(engine: str, voice: str, rate: str, volume: str) -> None:
    data = _read()
    data["tts"] = {
        "engine": engine,
        "voice": voice,
        "rate": rate,
        "volume": volume,
    }
    _write(data)


class TTSSettingsDialog(QDialog):
    """TTS engine and voice configuration dialog."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Голосовой движок — Настройки TTS")
        self.setMinimumWidth(460)
        self.setStyleSheet("""
            QDialog { background-color: #161b22; color: #c9d1d9; }
            QLabel { color: #c9d1d9; font-size: 13px; }
            QLineEdit, QComboBox {
                background-color: #0d1117; color: #f0f6fc;
                border: 1px solid #30363d; border-radius: 4px;
                padding: 6px; font-family: monospace;
            }
            QLineEdit:focus, QComboBox:focus { border: 1px solid #58a6ff; }
            QPushButton {
                background-color: #21262d; color: #c9d1d9;
                border: 1px solid #30363d; border-radius: 4px;
                padding: 6px 14px; font-weight: bold;
            }
            QPushButton:hover { background-color: #30363d; color: #f0f6fc; }
            QPushButton#saveBtn {
                background-color: #238636; color: #fff;
                border: 1px solid #2ea043;
            }
            QPushButton#saveBtn:hover { background-color: #2ea043; }
        """)

        cfg = load_tts_settings()

        main = QVBoxLayout(self)
        main.setSpacing(12)

        header = QLabel("<b>Голосовой движок (TTS)</b>")
        header.setStyleSheet("font-size: 15px; color: #58a6ff;")
        main.addWidget(header)

        form = QFormLayout()
        form.setSpacing(8)

        self._engine_combo = QComboBox()
        engines = list_engines()
        self._engine_combo.addItems(engines if engines else ["edge-tts"])
        idx = self._engine_combo.findText(cfg["engine"])
        if idx >= 0:
            self._engine_combo.setCurrentIndex(idx)
        self._engine_combo.currentTextChanged.connect(self._on_engine_changed)
        form.addRow("Движок:", self._engine_combo)

        self._voice_combo = QComboBox()
        self._populate_voices(cfg["engine"], cfg["voice"])
        form.addRow("Голос:", self._voice_combo)

        self._rate_input = QLineEdit(cfg["rate"])
        self._rate_input.setPlaceholderText("+0%, +20%, -10%")
        form.addRow("Скорость:", self._rate_input)

        self._vol_input = QLineEdit(cfg["volume"])
        self._vol_input.setPlaceholderText("+0%, +50%")
        form.addRow("Громкость:", self._vol_input)

        main.addLayout(form)

        preview_layout = QHBoxLayout()
        preview_btn = QPushButton("Превью")
        preview_btn.clicked.connect(self._on_preview)
        preview_layout.addWidget(preview_btn)
        preview_layout.addStretch()
        main.addLayout(preview_layout)

        main.addSpacing(8)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel = QPushButton("Отмена")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        save = QPushButton("Сохранить")
        save.setObjectName("saveBtn")
        save.clicked.connect(self._on_save)
        btns.addWidget(save)
        main.addLayout(btns)

    def _populate_voices(self, engine_name: str, current_voice: str) -> None:
        self._voice_combo.clear()
        engine = get_engine(engine_name)
        if engine:
            for v in engine.list_voices():
                self._voice_combo.addItem(v["name"], v["id"])
        idx = self._voice_combo.findData(current_voice)
        if idx >= 0:
            self._voice_combo.setCurrentIndex(idx)

    def _on_engine_changed(self, name: str) -> None:
        self._populate_voices(name, "")

    def _on_preview(self) -> None:
        engine_name = self._engine_combo.currentText()
        engine = get_engine(engine_name)
        if engine is None:
            return
        voice_id = self._voice_combo.currentData() or self._voice_combo.currentText()
        engine.configure(
            voice=voice_id,
            rate=self._rate_input.text().strip(),
            volume=self._vol_input.text().strip(),
        )
        import threading
        threading.Thread(
            target=lambda: engine.speak("Тест голосового движка", Priority.NORMAL),
            daemon=True,
        ).start()

    def _on_save(self) -> None:
        engine = self._engine_combo.currentText()
        voice = self._voice_combo.currentData() or self._voice_combo.currentText()
        rate = self._rate_input.text().strip() or "+0%"
        volume = self._vol_input.text().strip() or "+0%"
        save_tts_settings(engine, voice, rate, volume)
        self.accept()
