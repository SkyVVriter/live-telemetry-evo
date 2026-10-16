"""PTT Settings dialog: 'Радио (PTT)'.

Device selection, level meter, PTT binding, mode selector, STT engine,
max duration.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..logbook import log
from ..settings import load_ptt_settings, save_ptt_settings
from .ffb_safe import normalize_wheel_backend
from .input_binding import (
    ASSIGN_TIMEOUT_S, PTTBinding, list_joysticks, run_diagnostics,
)
from .pygame_joy import get_pygame_worker
from .recorder import (
    AudioRecorder,
    DEFAULT_MIC_GRACE_S,
    list_input_devices,
    list_output_devices,
)
from .stt import list_stt_engines


class _BindingSignalBridge(QObject):
    """Queued signal bridge: pynput/pygame threads emit here; the GUI
    thread receives and updates the dialog widgets safely."""
    binding_captured = Signal(object)


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

        dev_row = QHBoxLayout()
        self._device_combo = QComboBox()
        self._device_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents)
        dev_row.addWidget(self._device_combo, 1)
        refresh_btn = QPushButton("⟳")
        refresh_btn.setFixedWidth(32)
        refresh_btn.setToolTip("Обновить список устройств")
        refresh_btn.clicked.connect(self._refresh_devices)
        dev_row.addWidget(refresh_btn)
        audio_layout.addRow("Микрофон:", dev_row)
        self._populate_device_combo(cfg)

        out_row = QHBoxLayout()
        self._output_combo = QComboBox()
        self._output_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents)
        out_row.addWidget(self._output_combo, 1)
        out_refresh = QPushButton("⟳")
        out_refresh.setFixedWidth(32)
        out_refresh.setToolTip("Обновить список устройств вывода")
        out_refresh.clicked.connect(self._refresh_output_devices)
        out_row.addWidget(out_refresh)
        audio_layout.addRow("Динамик (TTS):", out_row)
        self._populate_output_combo(cfg)
        out_hint = QLabel(
            "winsound всегда играет в Windows default. Выбранный динамик "
            "использует WASAPI (sounddevice). Пока открыт Bluetooth HFP-микрофон, "
            "стерео-выход наушников может молчать — поток закрывается сразу "
            "после отпускания PTT."
        )
        out_hint.setStyleSheet("color: #8b949e; font-size: 11px;")
        out_hint.setWordWrap(True)
        audio_layout.addRow("", out_hint)

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
        self._test_out_btn = QPushButton("Тест динамика")
        self._test_out_btn.clicked.connect(self._on_test_output)
        test_layout.addWidget(self._test_out_btn)
        test_layout.addStretch()
        audio_layout.addRow("", test_layout)

        self._grace_spin = QDoubleSpinBox()
        self._grace_spin.setRange(0.0, 2.0)
        self._grace_spin.setSingleStep(0.05)
        self._grace_spin.setDecimals(2)
        self._grace_spin.setSuffix(" с")
        self._grace_spin.setValue(float(cfg.get("mic_grace_s", DEFAULT_MIC_GRACE_S)))
        audio_layout.addRow("Пауза после PTT:", self._grace_spin)
        grace_hint = QLabel(
            "Ожидание первых сэмплов Bluetooth после отпускания. "
            "Короткое PTT (~1 с) иначе может дать пустую запись."
        )
        grace_hint.setStyleSheet("color: #8b949e; font-size: 11px;")
        grace_hint.setWordWrap(True)
        audio_layout.addRow("", grace_hint)

        self._keep_warm = QCheckBox(
            "Держать микрофон тёплым (быстрее короткое PTT; "
            "Bluetooth HFP может выключить стерео)")
        self._keep_warm.setChecked(bool(cfg.get("mic_keep_warm", False)))
        audio_layout.addRow("", self._keep_warm)

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

        self._backend_combo = QComboBox()
        self._backend_combo.addItem("winmm (безопасно, только чтение, 32 кн.)", "winmm")
        self._backend_combo.addItem("rawhid (безопасно, 128 кн., без FFB)", "rawhid")
        self._backend_combo.addItem("DirectInput", "dinput")
        self._backend_combo.addItem("pygame", "pygame")
        self._backend_combo.addItem("выкл", "off")
        saved_backend = normalize_wheel_backend(cfg.get("wheel_backend"))
        idx = self._backend_combo.findData(saved_backend)
        if idx >= 0:
            self._backend_combo.setCurrentIndex(idx)
        self._backend_combo.currentIndexChanged.connect(self._on_backend_changed)
        bind_layout.addRow("Опрос руля (Wheel polling backend):",
                           self._backend_combo)
        backend_hint = QLabel(
            "winmm не трогает FFB (joyGetPosEx — 32 кнопки, обычно R16). "
            "rawhid — Raw Input, до 128 кнопок, без CreateFile/FFB. "
            "DirectInput / pygame могут инвертировать отдачу в iRacing."
        )
        backend_hint.setStyleSheet("color: #8b949e; font-size: 11px;")
        backend_hint.setWordWrap(True)
        bind_layout.addRow("", backend_hint)

        bind_btn_layout = QHBoxLayout()
        self._assign_btn = QPushButton("Назначить...")
        self._assign_btn.clicked.connect(self._on_assign)
        bind_btn_layout.addWidget(self._assign_btn)
        self._cancel_assign_btn = QPushButton("Отмена")
        self._cancel_assign_btn.setVisible(False)
        self._cancel_assign_btn.clicked.connect(self._on_cancel_assign)
        bind_btn_layout.addWidget(self._cancel_assign_btn)
        self._clear_btn = QPushButton("Сбросить")
        self._clear_btn.clicked.connect(self._on_clear_binding)
        bind_btn_layout.addWidget(self._clear_btn)
        bind_btn_layout.addStretch()
        bind_layout.addRow("", bind_btn_layout)

        self._joy_label = QLabel("")
        self._joy_label.setStyleSheet("color: #8b949e; font-size: 11px;")
        self._joy_label.setWordWrap(True)
        self._refresh_joystick_info()
        bind_layout.addRow("Джойстики:", self._joy_label)

        diag_layout = QHBoxLayout()
        diag_btn = QPushButton("Диагностика устройств")
        diag_btn.clicked.connect(self._on_diagnostics)
        diag_layout.addWidget(diag_btn)
        diag_layout.addStretch()
        bind_layout.addRow("", diag_layout)

        self._diag_text = QPlainTextEdit()
        self._diag_text.setReadOnly(True)
        self._diag_text.setMaximumHeight(120)
        self._diag_text.setStyleSheet(
            "background: #0d1117; color: #8b949e; font-family: monospace; "
            "font-size: 11px; border: 1px solid #30363d; border-radius: 4px;")
        self._diag_text.setVisible(False)
        bind_layout.addRow("", self._diag_text)

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
        if not engines:
            engines = ["elevenlabs-scribe"]
        self._stt_combo.addItems(engines)
        idx = self._stt_combo.findText(cfg["stt_engine"])
        if idx >= 0:
            self._stt_combo.setCurrentIndex(idx)
        self._stt_combo.currentTextChanged.connect(self._on_stt_engine_changed)
        stt_layout.addRow("Движок:", self._stt_combo)

        self._api_key_input = QLineEdit(cfg["whisper_api_key"])
        self._api_key_input.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        self._api_key_input.setPlaceholderText("sk-... или OPENAI_API_KEY из env")
        stt_layout.addRow("API Key:", self._api_key_input)

        self._stt_key_hint = QLabel("")
        self._stt_key_hint.setStyleSheet("color: #8b949e; font-size: 11px;")
        self._stt_key_hint.setWordWrap(True)
        stt_layout.addRow("", self._stt_key_hint)
        self._on_stt_engine_changed(self._stt_combo.currentText())

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
        self._assign_listener = None
        self._binding_bridge = _BindingSignalBridge(self)
        self._binding_bridge.binding_captured.connect(
            self._on_binding_captured_gui,
            Qt.ConnectionType.QueuedConnection)
        self._assign_timeout = QTimer(self)
        self._assign_timeout.setSingleShot(True)
        self._assign_timeout.setInterval(int(ASSIGN_TIMEOUT_S * 1000))
        self._assign_timeout.timeout.connect(self._on_assign_timeout)
        self._maybe_start_pygame()
        QTimer.singleShot(300, self._refresh_joystick_info)

    def _on_test_toggle(self, checked: bool) -> None:
        if checked:
            from .recorder import resolve_input_device
            combo_idx = self._device_combo.currentIndex() - 1
            name = ""
            hostapi = ""
            idx = self._device_combo.currentData()
            if 0 <= combo_idx < len(self._devices):
                d = self._devices[combo_idx]
                name, hostapi, idx = d.name, d.hostapi_name, d.index
            resolved, err = resolve_input_device(name, hostapi, idx)
            self._recorder.set_device(resolved, name)
            if err and resolved is None:
                log(f"[ptt/dialog] mic test: {err}")
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

    def _populate_device_combo(self, cfg: dict) -> None:
        self._device_combo.clear()
        self._devices = list_input_devices()
        self._device_combo.addItem("(системный по умолчанию)", None)
        for d in self._devices:
            ha = f" [{d.hostapi_name}]" if d.hostapi_name else ""
            label = f"{d.name}{ha}" + (" ★" if d.is_default else "")
            self._device_combo.addItem(label, d.index)
        saved_name = cfg.get("device_name", "")
        saved_ha = cfg.get("device_hostapi", "")
        if saved_name:
            for i, d in enumerate(self._devices, start=1):
                if d.name == saved_name and (not saved_ha or d.hostapi_name == saved_ha):
                    self._device_combo.setCurrentIndex(i)
                    break
        elif cfg.get("device_index") is not None:
            for i in range(self._device_combo.count()):
                if self._device_combo.itemData(i) == cfg["device_index"]:
                    self._device_combo.setCurrentIndex(i)
                    break

    def _refresh_devices(self) -> None:
        cfg = load_ptt_settings()
        self._populate_device_combo(cfg)

    def _populate_output_combo(self, cfg: dict) -> None:
        self._output_combo.clear()
        self._output_devices = list_output_devices()
        self._output_combo.addItem("(системный по умолчанию)", None)
        for d in self._output_devices:
            ha = f" [{d.hostapi_name}]" if d.hostapi_name else ""
            label = f"{d.name}{ha}" + (" ★" if d.is_default else "")
            self._output_combo.addItem(label, d.index)
        saved_name = cfg.get("output_device_name", "")
        saved_ha = cfg.get("output_device_hostapi", "")
        if saved_name:
            for i, d in enumerate(self._output_devices, start=1):
                if d.name == saved_name and (
                        not saved_ha or d.hostapi_name == saved_ha):
                    self._output_combo.setCurrentIndex(i)
                    break
        elif cfg.get("output_device_index") is not None:
            for i in range(self._output_combo.count()):
                if self._output_combo.itemData(i) == cfg["output_device_index"]:
                    self._output_combo.setCurrentIndex(i)
                    break

    def _refresh_output_devices(self) -> None:
        cfg = load_ptt_settings()
        self._populate_output_combo(cfg)

    def _selected_output(self) -> tuple[object, str, str]:
        idx = self._output_combo.currentData()
        name = ""
        hostapi = ""
        combo_idx = self._output_combo.currentIndex() - 1
        if 0 <= combo_idx < len(self._output_devices):
            d = self._output_devices[combo_idx]
            name = d.name
            hostapi = d.hostapi_name
            idx = d.index
        return idx, name, hostapi

    def _on_test_output(self) -> None:
        idx, name, hostapi = self._selected_output()
        from ..tts.playback import set_playback_device
        set_playback_device(name, hostapi, idx)
        try:
            from .ptt_controller import _generate_beep, _play_wav_bytes
            import io
            import wave
            pcm = _generate_beep(880, 0.25, 16000)
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(pcm)
            _play_wav_bytes(buf.getvalue())
            log(f"[ptt/dialog] output test played to {name or 'Windows default'!r}")
        except Exception as e:
            log(f"[ptt/dialog] output test failed: {e}")

    def _wheel_backend(self) -> str:
        return normalize_wheel_backend(self._backend_combo.currentData())

    def _on_backend_changed(self) -> None:
        self._maybe_start_pygame()
        self._refresh_joystick_info()

    def _maybe_start_pygame(self) -> None:
        if self._wheel_backend() != "pygame":
            return
        try:
            get_pygame_worker().ensure_started()
        except Exception:
            pass
        QTimer.singleShot(300, self._refresh_joystick_info)

    def _on_diagnostics(self) -> None:
        report = run_diagnostics(active_backend=self._wheel_backend())
        self._diag_text.setPlainText(report)
        self._diag_text.setVisible(True)
        log(f"[ptt/dialog] device diagnostics:\n{report}")

    def _on_stt_engine_changed(self, engine_name: str) -> None:
        if engine_name == "elevenlabs-scribe":
            self._api_key_input.setPlaceholderText(
                "оставьте пустым — используется ключ из TTS")
            self._stt_key_hint.setText(
                "Используется API-ключ ElevenLabs из настроек TTS. "
                "Введите ключ здесь только для отдельного ключа STT.")
        elif engine_name == "whisper-api":
            self._api_key_input.setPlaceholderText(
                "sk-... или OPENAI_API_KEY из env")
            self._stt_key_hint.setText("")
        else:
            self._api_key_input.setPlaceholderText("")
            self._stt_key_hint.setText("")

    def _refresh_joystick_info(self) -> None:
        joys = list_joysticks(backend=self._wheel_backend())
        if joys:
            lines = [f"{j['name']} ({j['buttons']} кн.) [{j.get('backend', '')}]"
                     for j in joys]
            self._joy_label.setText("\n".join(lines))
        else:
            backend = self._wheel_backend()
            if backend == "off":
                self._joy_label.setText("опрос выключен")
            else:
                self._joy_label.setText(f"не обнаружены ({backend})")

    def _on_assign(self) -> None:
        if self._assigning:
            return
        self._assigning = True
        self._bind_label.setText("Нажмите клавишу или кнопку руля...")
        self._bind_label.setStyleSheet("color: #f0ad4e; font-weight: bold;")
        self._assign_btn.setVisible(False)
        self._clear_btn.setVisible(False)
        self._cancel_assign_btn.setVisible(True)

        from .input_binding import InputListener
        self._assign_listener = InputListener(
            wheel_backend=self._wheel_backend())
        self._assign_listener.start_assign(self._on_binding_captured_thread)
        self._assign_timeout.start()

    def _on_binding_captured_thread(self, binding: PTTBinding) -> None:
        # Worker thread: queued signal only — never touch widgets or join.
        # pylint: disable-next=no-member
        self._binding_bridge.binding_captured.emit(binding)

    def _on_binding_captured_gui(self, binding) -> None:
        self._assign_timeout.stop()
        self._stop_assign_listener()
        self._assigning = False
        self._binding_data = binding.to_dict()
        self._bind_label.setText(binding.display_name)
        self._bind_label.setStyleSheet("color: #f0f6fc; font-weight: bold;")
        self._assign_btn.setVisible(True)
        self._clear_btn.setVisible(True)
        self._cancel_assign_btn.setVisible(False)

    def _stop_assign_listener(self) -> None:
        lis = self._assign_listener
        self._assign_listener = None
        if lis is not None:
            lis.stop_assign()

    def _on_assign_timeout(self) -> None:
        if not self._assigning:
            return
        log("[ptt/dialog] assign timed out")
        self._on_cancel_assign()
        self._bind_label.setText("Назначение: время вышло")

    def _on_cancel_assign(self) -> None:
        self._assign_timeout.stop()
        self._stop_assign_listener()
        self._assigning = False
        if self._binding_data:
            try:
                b = PTTBinding.from_dict(self._binding_data)
                self._bind_label.setText(b.display_name)
            except Exception:
                self._bind_label.setText("Не назначена")
        else:
            self._bind_label.setText("Не назначена")
        self._bind_label.setStyleSheet("color: #f0f6fc; font-weight: bold;")
        self._assign_btn.setVisible(True)
        self._clear_btn.setVisible(True)
        self._cancel_assign_btn.setVisible(False)

    def _on_clear_binding(self) -> None:
        self._binding_data = None
        self._bind_label.setText("Не назначена")

    def _on_save(self) -> None:
        if self._recorder.is_recording:
            self._recorder.stop()
            self._level_timer.stop()

        dev_idx = self._device_combo.currentData()
        dev_name = ""
        dev_ha = ""
        combo_idx = self._device_combo.currentIndex() - 1
        if 0 <= combo_idx < len(self._devices):
            d = self._devices[combo_idx]
            dev_name = d.name
            dev_ha = d.hostapi_name

        out_idx, out_name, out_ha = self._selected_output()
        save_ptt_settings(
            enabled=True,
            mode=self._mode_combo.currentData() or "hold",
            max_duration_s=self._duration_spin.value(),
            device_index=dev_idx,
            device_name=dev_name,
            device_hostapi=dev_ha,
            output_device_index=out_idx,
            output_device_name=out_name,
            output_device_hostapi=out_ha,
            mic_grace_s=self._grace_spin.value(),
            mic_keep_warm=self._keep_warm.isChecked(),
            stt_engine=self._stt_combo.currentText(),
            whisper_api_key=self._api_key_input.text().strip(),
            binding=self._binding_data,
            wheel_backend=self._wheel_backend(),
        )
        from ..tts.playback import configure_playback_from_ptt_settings
        configure_playback_from_ptt_settings(load_ptt_settings())
        self.accept()

    def closeEvent(self, event) -> None:
        self._assign_timeout.stop()
        self._stop_assign_listener()
        if self._recorder.is_recording:
            self._recorder.stop()
            self._level_timer.stop()
        super().closeEvent(event)

    def keyPressEvent(self, event) -> None:
        if self._assigning and event.key() == Qt.Key.Key_Escape:
            self._on_cancel_assign()
            return
        super().keyPressEvent(event)
