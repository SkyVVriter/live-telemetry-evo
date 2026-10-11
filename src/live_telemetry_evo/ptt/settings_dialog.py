"""PTT Settings dialog: 'Радио (PTT)'.

Device selection, level meter, PTT binding, mode selector, STT engine,
max duration.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..logbook import log
from ..settings import load_ptt_settings, save_ptt_settings
from .input_binding import PTTBinding
from .recorder import AudioRecorder, list_input_devices
from .stt import list_stt_engines


_DARK_STYLESHEET = """
    QDialog { background-color: #161b22; color: #c9d1d9; }
    QLabel { color: #c9d1d9; font-size: 13px; }
    QComboBox, QDoubleSpinBox, QLineEdit {
        background-color: #0d1117; color: #f0f6fc;
        border: 1px solid #30363d; border-radius: 4px;
        padding: 6px; font-family: monospace;
    }
    QComboBox:focus, QLineEdit:focus { border: 1px solid #58a6ff; }
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
    QProgressBar {
        border: 1px solid #30363d; border-radius: 3px;
        background: #0d1117; text-align: center; color: #c9d1d9;
    }
    QProgressBar::chunk { background: #2ea043; border-radius: 2px; }
"""


class PTTSettingsDialog(QDialog):
    """PTT configuration dialog."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Радио (PTT) — Настройки")
        self.setMinimumWidth(520)
        self.setStyleSheet(_DARK_STYLESHEET)

        cfg = load_ptt_settings()

        main = QVBoxLayout(self)
        main.setSpacing(12)

        header = QLabel("<b>Радио (PTT)</b>")
        header.setStyleSheet("font-size: 15px; color: #58a6ff;")
        main.addWidget(header)

        desc = QLabel(
            "Push-to-talk для голосовых команд и общения с AI-инженером."
        )
        desc.setStyleSheet("color: #8b949e; font-size: 11px;")
        desc.setWordWrap(True)
        main.addWidget(desc)

        # --- Audio device ---
        audio_group = QGroupBox("Аудио")
        audio_layout = QFormLayout(audio_group)
        audio_layout.setSpacing(8)

        self._device_combo = QComboBox()
        self._devices = list_input_devices()
        self._device_combo.addItem("(системный по умолчанию)", None)
        for d in self._devices:
            label = f"{d.name}" + (" ★" if d.is_default else "")
            self._device_combo.addItem(label, d.index)
        if cfg["device_index"] is not None:
            for i in range(self._device_combo.count()):
                if self._device_combo.itemData(i) == cfg["device_index"]:
                    self._device_combo.setCurrentIndex(i)
                    break
        audio_layout.addRow("Устройство:", self._device_combo)

        self._level_bar = QProgressBar()
        self._level_bar.setRange(0, 100)
        self._level_bar.setValue(0)
        self._level_bar.setMaximumHeight(16)
        audio_layout.addRow("Уровень:", self._level_bar)

        test_layout = QHBoxLayout()
        self._test_btn = QPushButton("Тест микрофона")
        self._test_btn.setCheckable(True)
        self._test_btn.clicked.connect(self._on_test_toggle)
        test_layout.addWidget(self._test_btn)
        test_layout.addStretch()
        audio_layout.addRow("", test_layout)

        main.addWidget(audio_group)

        # --- PTT Binding ---
        bind_group = QGroupBox("Кнопка PTT")
        bind_layout = QFormLayout(bind_group)
        bind_layout.setSpacing(8)

        self._bind_label = QLabel("Не назначена")
        if cfg["binding"]:
            try:
                b = PTTBinding.from_dict(cfg["binding"])
                self._bind_label.setText(b.display_name)
            except Exception:
                pass
        self._bind_label.setStyleSheet("color: #f0f6fc; font-weight: bold;")
        bind_layout.addRow("Текущая:", self._bind_label)

        bind_btn_layout = QHBoxLayout()
        self._assign_btn = QPushButton("Назначить...")
        self._assign_btn.clicked.connect(self._on_assign)
        bind_btn_layout.addWidget(self._assign_btn)
        self._clear_btn = QPushButton("Сбросить")
        self._clear_btn.clicked.connect(self._on_clear_binding)
        bind_btn_layout.addWidget(self._clear_btn)
        bind_btn_layout.addStretch()
        bind_layout.addRow("", bind_btn_layout)

        main.addWidget(bind_group)

        # --- Mode ---
        mode_group = QGroupBox("Режим")
        mode_layout = QFormLayout(mode_group)
        mode_layout.setSpacing(8)

        self._mode_combo = QComboBox()
        self._mode_combo.addItem("Удержание (hold)", "hold")
        self._mode_combo.addItem("Переключение (toggle)", "toggle")
        idx = self._mode_combo.findData(cfg["mode"])
        if idx >= 0:
            self._mode_combo.setCurrentIndex(idx)
        mode_layout.addRow("Режим:", self._mode_combo)

        self._duration_spin = QDoubleSpinBox()
        self._duration_spin.setRange(3.0, 60.0)
        self._duration_spin.setSingleStep(1.0)
        self._duration_spin.setDecimals(0)
        self._duration_spin.setSuffix(" с")
        self._duration_spin.setValue(cfg["max_duration_s"])
        mode_layout.addRow("Макс. длительность:", self._duration_spin)

        main.addWidget(mode_group)

        # --- STT Engine ---
        stt_group = QGroupBox("Распознавание речи (STT)")
        stt_layout = QFormLayout(stt_group)
        stt_layout.setSpacing(8)

        self._stt_combo = QComboBox()
        engines = list_stt_engines()
        self._stt_combo.addItems(engines if engines else ["whisper-api"])
        idx = self._stt_combo.findText(cfg["stt_engine"])
        if idx >= 0:
            self._stt_combo.setCurrentIndex(idx)
        stt_layout.addRow("Движок:", self._stt_combo)

        self._api_key_input = QLineEdit(cfg["whisper_api_key"])
        self._api_key_input.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        self._api_key_input.setPlaceholderText("sk-... или OPENAI_API_KEY из env")
        stt_layout.addRow("API Key:", self._api_key_input)

        main.addWidget(stt_group)

        # --- Buttons ---
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

        # Internal state
        self._recorder = AudioRecorder()
        self._level_timer = QTimer(self)
        self._level_timer.setInterval(50)
        self._level_timer.timeout.connect(self._update_level)
        self._binding_data: dict | None = cfg["binding"]
        self._assigning = False

    def _on_test_toggle(self, checked: bool) -> None:
        if checked:
            dev = self._device_combo.currentData()
            self._recorder.set_device(dev)
            if self._recorder.start():
                self._test_btn.setText("Остановить")
                self._level_timer.start()
            else:
                self._test_btn.setChecked(False)
        else:
            self._recorder.stop()
            self._level_timer.stop()
            self._level_bar.setValue(0)
            self._test_btn.setText("Тест микрофона")

    def _update_level(self) -> None:
        self._level_bar.setValue(int(self._recorder.rms_level * 100))

    def _on_assign(self) -> None:
        if self._assigning:
            return
        self._assigning = True
        self._assign_btn.setText("Нажмите кнопку...")
        self._assign_btn.setEnabled(False)

        from .input_binding import InputListener
        self._assign_listener = InputListener()
        self._assign_listener.start_assign(self._on_binding_captured)

    def _on_binding_captured(self, binding: PTTBinding) -> None:
        self._assigning = False
        self._binding_data = binding.to_dict()
        self._bind_label.setText(binding.display_name)
        self._assign_btn.setText("Назначить...")
        self._assign_btn.setEnabled(True)

    def _on_clear_binding(self) -> None:
        self._binding_data = None
        self._bind_label.setText("Не назначена")

    def _on_save(self) -> None:
        if self._recorder.is_recording:
            self._recorder.stop()
            self._level_timer.stop()

        save_ptt_settings(
            enabled=True,
            mode=self._mode_combo.currentData() or "hold",
            max_duration_s=self._duration_spin.value(),
            device_index=self._device_combo.currentData(),
            stt_engine=self._stt_combo.currentText(),
            whisper_api_key=self._api_key_input.text().strip(),
            binding=self._binding_data,
        )
        self.accept()

    def closeEvent(self, event) -> None:
        if self._recorder.is_recording:
            self._recorder.stop()
            self._level_timer.stop()
        super().closeEvent(event)
