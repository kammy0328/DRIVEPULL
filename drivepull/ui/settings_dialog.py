from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QSpinBox, QVBoxLayout)

from ..auth import import_client_secret, load_client_config
from ..config import Settings


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.s = settings
        self.setWindowTitle("설정")
        self.setMinimumWidth(480)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(14)

        t = QLabel("다운로드")
        t.setObjectName("sectionTitle")
        lay.addWidget(t)
        form = QFormLayout()
        form.setSpacing(10)
        self.auto = QCheckBox("연결 수 자동 조절 (4개에서 시작해 빨라지는 동안만 늘림)")
        self.auto.setChecked(settings.auto_connections)
        self.auto.toggled.connect(self._auto_toggled)
        form.addRow(self.auto)
        self.conns = QSpinBox()
        self.conns.setRange(1, 32)
        self.conns.setValue(settings.connections_per_file)
        form.addRow("", self.conns)
        self.conns_lbl = form.labelForField(self.conns)
        self.files = QSpinBox()
        self.files.setRange(1, 10)
        self.files.setValue(settings.concurrent_files)
        form.addRow("동시에 받을 파일 수", self.files)
        lay.addLayout(form)
        self._auto_toggled(self.auto.isChecked())

        self.md5 = QCheckBox("완료 후 MD5로 파일 손상 검사 (로그인 모드)")
        self.md5.setChecked(settings.verify_md5)
        lay.addWidget(self.md5)
        self.quota = QCheckBox("할당량 초과 파일은 내 드라이브에 임시 사본을 만들어 받기")
        self.quota.setChecked(settings.quota_copy_fallback)
        lay.addWidget(self.quota)
        self.awake = QCheckBox("다운로드 중 PC 절전 방지 (화면은 꺼져도 됨)")
        self.awake.setChecked(settings.keep_awake)
        lay.addWidget(self.awake)
        self.notify = QCheckBox("다운로드가 모두 끝나면 Windows 알림")
        self.notify.setChecked(settings.notify_done)
        lay.addWidget(self.notify)
        self.cleanup = QCheckBox("종료할 때 받다 만 파일의 임시 파일 정리 여부 묻기 (끄면 항상 남겨서 이어받기)")
        self.cleanup.setChecked(settings.cleanup_on_exit)
        lay.addWidget(self.cleanup)

        t2 = QLabel("구글 연결")
        t2.setObjectName("sectionTitle")
        lay.addSpacing(6)
        lay.addWidget(t2)
        row = QHBoxLayout()
        self.client_lbl = QLabel()
        self.client_lbl.setObjectName("muted")
        row.addWidget(self.client_lbl, 1)
        b = QPushButton("client_secret.json 불러오기")
        b.clicked.connect(self._import_client)
        row.addWidget(b)
        lay.addLayout(row)
        self._refresh_client()

        self.key = QLineEdit(settings.api_key)
        self.key.setPlaceholderText("API 키 (선택). 로그인 없이 공개 링크 목록을 API로 읽을 때 사용")
        lay.addWidget(self.key)

        btns = QHBoxLayout()
        btns.addStretch(1)
        c = QPushButton("취소")
        c.clicked.connect(self.reject)
        ok = QPushButton("저장")
        ok.setObjectName("primary")
        ok.clicked.connect(self._save)
        btns.addWidget(c)
        btns.addWidget(ok)
        lay.addSpacing(6)
        lay.addLayout(btns)

    def _auto_toggled(self, on: bool):
        self.conns_lbl.setText("파일당 최대 연결 수" if on else "파일당 연결 수")
        self.conns.setToolTip("자동 조절이 이 값을 넘지 않음" if on else "항상 이 개수로 나눠 받음")

    def _refresh_client(self):
        cfg = load_client_config()
        if cfg:
            cid = cfg.get("installed", {}).get("client_id", "")
            self.client_lbl.setText(f"OAuth 클라이언트: {cid[:18]}…" if cid else "OAuth 클라이언트 설정됨")
        else:
            self.client_lbl.setText("OAuth 클라이언트 없음 (로그인하려면 필요)")

    def _import_client(self):
        p, _ = QFileDialog.getOpenFileName(self, "OAuth 클라이언트 JSON", "", "JSON (*.json)")
        if not p:
            return
        try:
            import_client_secret(p)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "불러오기 실패", str(e))
            return
        self._refresh_client()

    def _save(self):
        self.s.connections_per_file = self.conns.value()
        self.s.auto_connections = self.auto.isChecked()
        self.s.cleanup_on_exit = self.cleanup.isChecked()
        self.s.keep_awake = self.awake.isChecked()
        self.s.notify_done = self.notify.isChecked()
        self.s.concurrent_files = self.files.value()
        self.s.verify_md5 = self.md5.isChecked()
        self.s.quota_copy_fallback = self.quota.isChecked()
        self.s.api_key = self.key.text().strip()
        self.s.save()
        self.accept()
