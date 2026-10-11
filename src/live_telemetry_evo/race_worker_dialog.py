"""Settings dialog for Race Worker (iRacing relay) connection."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
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

from .race_worker_client import RaceWorkerClient
from .settings import load_race_worker_settings, save_race_worker_settings


class RaceWorkerDialog(QDialog):
    """UI configuration for Race Worker relay connection."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Race Worker — Настройки")
        self.setMinimumWidth(480)
        self.setStyleSheet("""
            QDialog {
                background-color: #161b22;
                color: #c9d1d9;
            }
            QLabel {
                color: #c9d1d9;
                font-size: 13px;
            }
            QLineEdit {
                background-color: #0d1117;
                color: #f0f6fc;
                border: 1px solid #30363d;
                border-radius: 4px;
                padding: 6px;
                font-family: monospace;
            }
            QLineEdit:focus {
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

        cfg = load_race_worker_settings()

        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(15)

        header = QLabel("<b>Race Worker — iRacing Relay</b>")
        header.setStyleSheet("font-size: 15px; color: #58a6ff; margin-bottom: 5px;")
        main_layout.addWidget(header)

        desc = QLabel(
            "Подключение к серверу Race Worker для передачи данных "
            "кругов и событий AI-ассистенту (RaceBot)."
        )
        desc.setStyleSheet("color: #8b949e; font-size: 11px;")
        desc.setWordWrap(True)
        main_layout.addWidget(desc)

        form = QFormLayout()
        form.setSpacing(10)

        self._url_input = QLineEdit(cfg["url"])
        self._url_input.setPlaceholderText("https://your-server.com")
        form.addRow("URL сервера:", self._url_input)

        self._token_input = QLineEdit(cfg["app_token"])
        self._token_input.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        self._token_input.setPlaceholderText("APP_TOKEN")
        form.addRow("App Token:", self._token_input)

        self._enabled_cb = QCheckBox("Включить отправку данных")
        self._enabled_cb.setChecked(cfg["enabled"])
        form.addRow("", self._enabled_cb)

        main_layout.addLayout(form)

        test_layout = QHBoxLayout()
        self._test_btn = QPushButton("Проверить связь")
        self._test_btn.clicked.connect(self._on_test)
        self._test_status = QLabel("")
        self._test_status.setStyleSheet("font-size: 12px;")
        test_layout.addWidget(self._test_btn)
        test_layout.addWidget(self._test_status)
        test_layout.addStretch()
        main_layout.addLayout(test_layout)

        main_layout.addSpacing(10)

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

    def _on_test(self) -> None:
        url = self._url_input.text().strip()
        token = self._token_input.text().strip()
        self._test_status.setText("Проверка...")
        self._test_status.setStyleSheet("color: #8b949e;")

        client = RaceWorkerClient(base_url=url, token=token)
        ok, msg = client.test_connection()
        if ok:
            self._test_status.setText(f"OK: {msg}")
            self._test_status.setStyleSheet("color: #2ecc71; font-weight: bold;")
        else:
            self._test_status.setText(f"Ошибка: {msg}")
            self._test_status.setStyleSheet("color: #e74c3c; font-weight: bold;")

    def _on_save(self) -> None:
        url = self._url_input.text().strip()
        token = self._token_input.text().strip()
        enabled = self._enabled_cb.isChecked()

        if not url:
            QMessageBox.warning(self, "Ошибка", "Укажите URL сервера.")
            return

        save_race_worker_settings(
            url=url,
            app_token=token,
            enabled=enabled,
        )
        self.accept()
