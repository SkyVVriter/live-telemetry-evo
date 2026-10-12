"""TTS Settings dialog for voice engine selection and configuration.

When the ElevenLabs engine is selected, the dialog shows per-voice profile
management (name, voice_id, model, stability/similarity) and a character
usage counter with budget display.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSpinBox,
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


_DARK_STYLESHEET = """
    QDialog { background-color: #161b22; color: #c9d1d9; }
    QLabel { color: #c9d1d9; font-size: 13px; }
    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QListWidget {
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
    QGroupBox {
        color: #c9d1d9; border: 1px solid #30363d;
        border-radius: 6px; margin-top: 10px; padding-top: 14px;
    }
    QGroupBox::title {
        subcontrol-origin: margin; left: 12px; padding: 0 4px;
        color: #58a6ff; font-weight: bold;
    }
"""


class TTSSettingsDialog(QDialog):
    """TTS engine and voice configuration dialog."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Голосовой движок — Настройки TTS")
        self.setMinimumWidth(520)
        self.setStyleSheet(_DARK_STYLESHEET)

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
        required = ["edge-tts", "elevenlabs"]
        for name in required:
            if name not in engines:
                engines.append(name)
        self._engine_combo.addItems(engines)
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

        # --- ElevenLabs-specific panel (hidden until engine == elevenlabs) --
        self._el_group = QGroupBox("ElevenLabs")
        el_layout = QVBoxLayout(self._el_group)

        el_form = QFormLayout()
        el_form.setSpacing(6)

        self._el_api_key = QLineEdit()
        self._el_api_key.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        self._el_api_key.setPlaceholderText("xi-... или ELEVENLABS_API_KEY из env")
        el_form.addRow("API Key:", self._el_api_key)

        el_layout.addLayout(el_form)

        # Voice profiles
        profiles_label = QLabel("<b>Голосовые профили</b>")
        profiles_label.setStyleSheet("font-size: 12px; color: #8b949e;")
        el_layout.addWidget(profiles_label)

        self._profile_list = QListWidget()
        self._profile_list.setMaximumHeight(100)
        self._profile_list.currentRowChanged.connect(self._on_profile_selected)
        el_layout.addWidget(self._profile_list)

        profile_btns = QHBoxLayout()
        add_btn = QPushButton("+")
        add_btn.setFixedWidth(36)
        add_btn.clicked.connect(self._on_add_profile)
        del_btn = QPushButton("−")
        del_btn.setFixedWidth(36)
        del_btn.clicked.connect(self._on_del_profile)
        profile_btns.addWidget(add_btn)
        profile_btns.addWidget(del_btn)
        profile_btns.addStretch()
        el_layout.addLayout(profile_btns)

        pf = QFormLayout()
        pf.setSpacing(6)
        self._pf_name = QLineEdit()
        pf.addRow("Имя:", self._pf_name)
        self._pf_voice_id = QLineEdit()
        self._pf_voice_id.setPlaceholderText("voice_id из ElevenLabs")
        pf.addRow("Voice ID:", self._pf_voice_id)
        self._pf_model = QComboBox()
        from .elevenlabs_engine import AVAILABLE_MODELS
        self._pf_model.addItems(AVAILABLE_MODELS)
        pf.addRow("Модель:", self._pf_model)
        self._pf_stability = QDoubleSpinBox()
        self._pf_stability.setRange(0.0, 1.0)
        self._pf_stability.setSingleStep(0.05)
        self._pf_stability.setDecimals(2)
        pf.addRow("Stability:", self._pf_stability)
        self._pf_similarity = QDoubleSpinBox()
        self._pf_similarity.setRange(0.0, 1.0)
        self._pf_similarity.setSingleStep(0.05)
        self._pf_similarity.setDecimals(2)
        pf.addRow("Similarity:", self._pf_similarity)
        self._pf_style = QDoubleSpinBox()
        self._pf_style.setRange(0.0, 1.0)
        self._pf_style.setSingleStep(0.05)
        self._pf_style.setDecimals(2)
        pf.addRow("Style:", self._pf_style)
        self._pf_speed = QDoubleSpinBox()
        self._pf_speed.setRange(0.5, 2.0)
        self._pf_speed.setSingleStep(0.05)
        self._pf_speed.setDecimals(2)
        self._pf_speed.setValue(1.0)
        pf.addRow("Speed:", self._pf_speed)
        self._pf_language = QLineEdit()
        self._pf_language.setPlaceholderText("en, ru, de, … (пусто = авто)")
        pf.addRow("Language:", self._pf_language)
        el_layout.addLayout(pf)

        # Character budget
        budget_layout = QHBoxLayout()
        budget_layout.addWidget(QLabel("Бюджет символов/мес:"))
        self._el_budget = QSpinBox()
        self._el_budget.setRange(100, 1_000_000)
        self._el_budget.setSingleStep(1000)
        budget_layout.addWidget(self._el_budget)
        self._el_usage_label = QLabel("")
        self._el_usage_label.setStyleSheet("color: #8b949e; font-size: 11px;")
        budget_layout.addWidget(self._el_usage_label)
        budget_layout.addStretch()
        el_layout.addLayout(budget_layout)

        main.addWidget(self._el_group)

        # Load ElevenLabs state
        self._el_profiles: list = []
        self._load_elevenlabs_state()
        self._el_group.setVisible(cfg["engine"] == "elevenlabs")

        # Preview + bottom buttons
        preview_layout = QHBoxLayout()
        preview_btn = QPushButton("Проверить")
        preview_btn.clicked.connect(self._on_preview)
        preview_layout.addWidget(preview_btn)
        self._preview_status = QLabel("")
        self._preview_status.setStyleSheet("color: #8b949e; font-size: 11px;")
        preview_layout.addWidget(self._preview_status)
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

    def _load_elevenlabs_state(self) -> None:
        engine = get_engine("elevenlabs")
        if engine is None:
            return
        from .elevenlabs_engine import ElevenLabsEngine
        if not isinstance(engine, ElevenLabsEngine):
            return
        self._el_api_key.setText(engine._api_key)
        self._el_profiles = engine.get_profiles()
        self._el_budget.setValue(engine.get_usage().budget)
        usage = engine.get_usage()
        self._el_usage_label.setText(
            f"Использовано: {usage.chars_used}/{usage.budget} "
            f"(осталось {usage.remaining})"
        )
        self._refresh_profile_list()
        self._el_profiles_active_idx = engine._active_profile_idx

    def _refresh_profile_list(self) -> None:
        self._profile_list.clear()
        for p in self._el_profiles:
            self._profile_list.addItem(f"{p.name} ({p.voice_id[:12]}...)")
        if self._el_profiles:
            self._profile_list.setCurrentRow(0)

    def _on_profile_selected(self, row: int) -> None:
        if row < 0 or row >= len(self._el_profiles):
            return
        self._save_current_profile_edits()
        p = self._el_profiles[row]
        self._pf_name.setText(p.name)
        self._pf_voice_id.setText(p.voice_id)
        idx = self._pf_model.findText(p.model_id)
        if idx >= 0:
            self._pf_model.setCurrentIndex(idx)
        self._pf_stability.setValue(p.stability)
        self._pf_similarity.setValue(p.similarity_boost)
        self._pf_style.setValue(p.style)
        self._pf_speed.setValue(p.speed)
        self._pf_language.setText(p.language)

    def _save_current_profile_edits(self) -> None:
        row = self._profile_list.currentRow()
        if row < 0 or row >= len(self._el_profiles):
            return
        p = self._el_profiles[row]
        name = self._pf_name.text().strip()
        if name:
            p.name = name
        vid = self._pf_voice_id.text().strip()
        if vid:
            p.voice_id = vid
        p.model_id = self._pf_model.currentText()
        p.stability = self._pf_stability.value()
        p.similarity_boost = self._pf_similarity.value()
        p.style = self._pf_style.value()
        p.speed = self._pf_speed.value()
        p.language = self._pf_language.text().strip()

    def _on_add_profile(self) -> None:
        from .elevenlabs_engine import VoiceProfile
        self._el_profiles.append(VoiceProfile(name=f"Profile {len(self._el_profiles) + 1}"))
        self._refresh_profile_list()
        self._profile_list.setCurrentRow(len(self._el_profiles) - 1)

    def _on_del_profile(self) -> None:
        row = self._profile_list.currentRow()
        if row >= 0 and len(self._el_profiles) > 1:
            self._el_profiles.pop(row)
            self._refresh_profile_list()

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
        self._el_group.setVisible(name == "elevenlabs")

    def _on_preview(self) -> None:
        engine_name = self._engine_combo.currentText()
        engine = get_engine(engine_name)
        if engine is None:
            self._preview_status.setText(f"Движок '{engine_name}' недоступен")
            self._preview_status.setStyleSheet("color: #e74c3c; font-size: 11px;")
            log(f"[tts/dialog] engine '{engine_name}' not available for preview")
            return

        if engine_name == "elevenlabs":
            self._save_current_profile_edits()
            from .elevenlabs_engine import ElevenLabsEngine
            if isinstance(engine, ElevenLabsEngine):
                api_key = self._el_api_key.text().strip()
                row = self._profile_list.currentRow()
                engine.configure(
                    api_key=api_key,
                    active_profile=max(0, row),
                    profiles=self._el_profiles,
                )
        else:
            voice_id = (self._voice_combo.currentData()
                        or self._voice_combo.currentText())
            engine.configure(
                voice=voice_id,
                rate=self._rate_input.text().strip(),
                volume=self._vol_input.text().strip(),
            )

        self._preview_status.setText("Синтез...")
        self._preview_status.setStyleSheet("color: #8b949e; font-size: 11px;")

        import threading
        def _do_preview():
            try:
                engine.speak("Тест голосового движка", Priority.NORMAL)
            except Exception as e:
                log(f"[tts/dialog] preview failed: {e}")

        threading.Thread(target=_do_preview, daemon=True).start()

    def _on_save(self) -> None:
        engine_name = self._engine_combo.currentText()
        voice = (self._voice_combo.currentData()
                 or self._voice_combo.currentText())
        rate = self._rate_input.text().strip() or "+0%"
        volume = self._vol_input.text().strip() or "+0%"
        save_tts_settings(engine_name, voice, rate, volume)

        if engine_name == "elevenlabs":
            self._save_current_profile_edits()
            engine = get_engine("elevenlabs")
            from .elevenlabs_engine import ElevenLabsEngine
            if isinstance(engine, ElevenLabsEngine):
                engine.configure(
                    api_key=self._el_api_key.text().strip(),
                    active_profile=max(0, self._profile_list.currentRow()),
                    profiles=self._el_profiles,
                    budget=self._el_budget.value(),
                )

        self.accept()
