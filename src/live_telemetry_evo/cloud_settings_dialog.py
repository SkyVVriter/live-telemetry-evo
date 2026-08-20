"""Settings dialog for AI Cloud Telemetry Server and Coaching Configuration."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .cloud_dispatcher import CloudDispatcher
from .settings import load_cloud_settings, save_cloud_settings


class CloudSettingsDialog(QDialog):
    """UI configuration for AI Cloud Telemetry Server connection."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI Cloud Race Engineer — Настройки")
        self.setMinimumWidth(520)
        self.setStyleSheet("""
            QDialog {
                background-color: #161b22;
                color: #c9d1d9;
            }
            QLabel {
                color: #c9d1d9;
                font-size: 13px;
            }
            QLineEdit, QComboBox {
                background-color: #0d1117;
                color: #f0f6fc;
                border: 1px solid #30363d;
                border-radius: 4px;
                padding: 6px;
                font-family: monospace;
            }
            QLineEdit:focus, QComboBox:focus {
                border: 1px solid #58a6ff;
            }
            QPushButton {
                background-color: #21262d;
                color: #c9d1d9;
                border: 1px solid #30363d;
                border-radius: 4px;
                padding: 6px 14px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #30363d;
                color: #f0f6fc;
            }
            QPushButton#saveBtn {
                background-color: #238636;
                color: #ffffff;
                border: 1px solid #2ea043;
            }
            QPushButton#saveBtn:hover {
                background-color: #2ea043;
            }
            QCheckBox {
                color: #c9d1d9;
            }
        """)

        cfg = load_cloud_settings()

        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(15)

        # Header
        header = QLabel("🏎️ <b>AI Cloud Race Engineer & Telemetry Platform</b>")
        header.setStyleSheet("font-size: 15px; color: #58a6ff; margin-bottom: 5px;")
        main_layout.addWidget(header)

        desc = QLabel("Автоматическая отправка стинтов, расчет 7 микросекторов Delta и ИИ-коучинг.")
        desc.setStyleSheet("color: #8b949e; font-size: 11px;")
        desc.setWordWrap(True)
        main_layout.addWidget(desc)

        # Form layout
        form = QFormLayout()
        form.setSpacing(10)

        self._server_input = QLineEdit(cfg["server_url"])
        self._server_input.setPlaceholderText("https://your-server.com/telemetry-api")
        form.addRow("Сервер API:", self._server_input)

        self._token_input = QLineEdit(cfg["api_token"])
        self._token_input.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        self._token_input.setPlaceholderText("Ваш Bearer токен")
        form.addRow("API Токен:", self._token_input)

        self._pilot_input = QLineEdit(cfg["pilot_id"])
        self._pilot_input.setPlaceholderText("Ваш позывной / Nickname")
        form.addRow("ID Пилота:", self._pilot_input)

        self._profile_combo = QComboBox()
        self._profile_combo.addItems([
            "TimeAttack (Квалификация & Рекорды)",
            "Endurance (Стабильность & Шины)",
            "ChassisEngineering (Подвеска & Баланс)",
        ])
        current_prof = cfg.get("coaching_profile", "TimeAttack")
        idx = 0
        for i in range(self._profile_combo.count()):
            if current_prof in self._profile_combo.itemText(i):
                idx = i
                break
        self._profile_combo.setCurrentIndex(idx)
        form.addRow("Профиль коуча:", self._profile_combo)

        self._auto_upload_cb = QCheckBox("Автоматически загружать лог после остановки записи")
        self._auto_upload_cb.setChecked(cfg["auto_upload"])
        form.addRow("", self._auto_upload_cb)

        main_layout.addLayout(form)

        # Test connection row
        test_layout = QHBoxLayout()
        self._test_btn = QPushButton("Проверить связь")
        self._test_btn.clicked.connect(self._on_test_connection)
        self._test_status = QLabel("")
        self._test_status.setStyleSheet("font-size: 12px;")
        test_layout.addWidget(self._test_btn)
        test_layout.addWidget(self._test_status)
        test_layout.addStretch()
        main_layout.addLayout(test_layout)

        offline_layout = QHBoxLayout()
        self._offline_btn = QPushButton("Догрузить оффлайн-сессии")
        self._offline_btn.setToolTip(
            "Отправить локальные CSV из папки logs, которые не ушли из-за "
            "оффлайна или обрыва SSL. Не зависит от галочки автозагрузки."
        )
        self._offline_btn.clicked.connect(self._on_upload_offline)
        self._offline_status = QLabel("")
        self._offline_status.setStyleSheet("font-size: 12px; color: #8b949e;")
        self._offline_status.setWordWrap(True)
        offline_layout.addWidget(self._offline_btn)
        offline_layout.addWidget(self._offline_status, 1)
        main_layout.addLayout(offline_layout)
        self._offline_busy = False
        self._refresh_offline_count()

        main_layout.addSpacing(10)

        # Bottom buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("Отмена")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        save_btn = QPushButton("Сохранить")
        save_btn.setObjectName("saveBtn")
        save_btn.clicked.connect(self._on_save)
        btn_layout.addWidget(save_btn)

        main_layout.addLayout(btn_layout)

    def _on_test_connection(self) -> None:
        server = self._server_input.text().strip()
        token = self._token_input.text().strip()
        self._test_status.setText("Проверка...")
        self._test_status.setStyleSheet("color: #8b949e;")

        ok, msg = CloudDispatcher.test_connection(server, token)
        if ok:
            self._test_status.setText(f"🟢 {msg}")
            self._test_status.setStyleSheet("color: #2ecc71; font-weight: bold;")
        else:
            self._test_status.setText(f"🔴 Ошибка: {msg}")
            self._test_status.setStyleSheet("color: #e74c3c; font-weight: bold;")

    def _refresh_offline_count(self) -> None:
        n = len(CloudDispatcher.pending_session_files())
        if self._offline_busy:
            return
        if n:
            self._offline_status.setText(f"В очереди: {n} файл(ов)")
            self._offline_status.setStyleSheet("font-size: 12px; color: #f0c14b;")
        else:
            self._offline_status.setText("Очередь пуста — все локальные логи уже загружены")
            self._offline_status.setStyleSheet("font-size: 12px; color: #8b949e;")

    def _on_upload_offline(self) -> None:
        if self._offline_busy:
            return
        server = self._server_input.text().strip()
        token = self._token_input.text().strip()
        if not server or not token:
            QMessageBox.warning(self, "Ошибка", "Сначала укажите адрес сервера и API токен.")
            return
        # Persist current form values so the worker thread reads them.
        save_cloud_settings(
            server_url=server,
            api_token=token,
            pilot_id=self._pilot_input.text().strip() or "Pilot",
            auto_upload=self._auto_upload_cb.isChecked(),
            coaching_profile=self._profile_combo.currentText().split(" ")[0],
        )
        pending = CloudDispatcher.pending_session_files()
        if not pending:
            self._offline_status.setText("Нечего загружать")
            self._offline_status.setStyleSheet("font-size: 12px; color: #8b949e;")
            return

        self._offline_busy = True
        self._offline_btn.setEnabled(False)
        self._offline_status.setText(f"Загрузка 0/{len(pending)}…")
        self._offline_status.setStyleSheet("font-size: 12px; color: #58a6ff;")

        def on_progress(text: str) -> None:
            QTimer.singleShot(0, lambda t=text: self._offline_status.setText(t))

        def on_complete(ok_n: int, fail_n: int, summary: str) -> None:
            def _done() -> None:
                self._offline_busy = False
                self._offline_btn.setEnabled(True)
                color = "#2ecc71" if fail_n == 0 else "#f39c12"
                self._offline_status.setStyleSheet(f"font-size: 12px; color: {color}; font-weight: bold;")
                self._offline_status.setText(summary)
                if fail_n:
                    QMessageBox.warning(self, "Догрузка", summary)
                else:
                    QMessageBox.information(self, "Догрузка", summary)
            QTimer.singleShot(0, _done)

        CloudDispatcher.upload_pending_async(on_progress, on_complete)

    def _on_save(self) -> None:
        server = self._server_input.text().strip()
        token = self._token_input.text().strip()
        pilot = self._pilot_input.text().strip() or "Pilot"
        auto_up = self._auto_upload_cb.isChecked()
        prof = self._profile_combo.currentText().split(" ")[0]

        if not server:
            QMessageBox.warning(self, "Ошибка", "Укажите адрес сервера API.")
            return

        save_cloud_settings(
            server_url=server,
            api_token=token,
            pilot_id=pilot,
            auto_upload=auto_up,
            coaching_profile=prof,
        )
        self.accept()
