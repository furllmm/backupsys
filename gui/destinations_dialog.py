"""
gui.destinations_dialog
=========================

Lets the user manage backup destinations (local/USB/SMB/NFS folder, SFTP,
WebDAV, S3-compatible). Backed by the same DestinationManager, so all
changes are persisted consistently.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QCheckBox,
    QWidget,
)

from destinations.manager import (
    DestinationManager,
    DestinationValidationError,
    build_destination,
    detect_external_drives,
)


class _LocalFields(QWidget):
    kind = "local"

    def __init__(self, params: Optional[dict] = None):
        super().__init__()
        layout = QFormLayout(self)
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("/mnt/backup-drive or a Syncthing/mounted-share folder")
        if params:
            self.path_edit.setText(params.get("path", ""))
        layout.addRow("Folder path:", self.path_edit)

    def params(self) -> dict:
        return {"path": self.path_edit.text().strip()}


class _SFTPFields(QWidget):
    kind = "sftp"

    def __init__(self, params: Optional[dict] = None):
        super().__init__()
        layout = QFormLayout(self)
        self.host_edit = QLineEdit()
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(22)
        self.username_edit = QLineEdit()
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.remote_root_edit = QLineEdit()
        self.remote_root_edit.setPlaceholderText("/backups")
        if params:
            self.host_edit.setText(params.get("host", ""))
            self.port_spin.setValue(int(params.get("port", 22)))
            self.username_edit.setText(params.get("username", "") or "")
            self.password_edit.setText(params.get("password", "") or "")
            self.remote_root_edit.setText(params.get("remote_root", "") or "/backups")
        layout.addRow("Host:", self.host_edit)
        layout.addRow("Port:", self.port_spin)
        layout.addRow("Username:", self.username_edit)
        layout.addRow("Password:", self.password_edit)
        layout.addRow("Remote path:", self.remote_root_edit)

    def params(self) -> dict:
        return {
            "host": self.host_edit.text().strip(),
            "port": self.port_spin.value(),
            "username": self.username_edit.text().strip() or None,
            "password": self.password_edit.text() or None,
            "remote_root": self.remote_root_edit.text().strip() or "/backups",
        }


class _WebDAVFields(QWidget):
    kind = "webdav"

    def __init__(self, params: Optional[dict] = None):
        super().__init__()
        layout = QFormLayout(self)
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://cloud.example.com/remote.php/dav/files/me/")
        self.username_edit = QLineEdit()
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        if params:
            self.url_edit.setText(params.get("base_url", ""))
            self.username_edit.setText(params.get("username", "") or "")
            self.password_edit.setText(params.get("password", "") or "")
        layout.addRow("WebDAV URL:", self.url_edit)
        layout.addRow("Username:", self.username_edit)
        layout.addRow("Password:", self.password_edit)
        hint = QLabel("Works with Nextcloud, ownCloud, Seafile, or any standard WebDAV server.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #9a9a9a; font-size: 11px;")
        layout.addRow(hint)

    def params(self) -> dict:
        return {
            "base_url": self.url_edit.text().strip(),
            "username": self.username_edit.text().strip() or None,
            "password": self.password_edit.text() or None,
        }


class _S3Fields(QWidget):
    kind = "s3"

    def __init__(self, params: Optional[dict] = None):
        super().__init__()
        layout = QFormLayout(self)
        self.bucket_edit = QLineEdit()
        self.access_key_edit = QLineEdit()
        self.secret_key_edit = QLineEdit()
        self.secret_key_edit.setEchoMode(QLineEdit.Password)
        self.endpoint_edit = QLineEdit()
        self.endpoint_edit.setPlaceholderText("http://minio.local:9000 (blank = real AWS S3)")
        if params:
            self.bucket_edit.setText(params.get("bucket", ""))
            self.access_key_edit.setText(params.get("access_key", "") or "")
            self.secret_key_edit.setText(params.get("secret_key", "") or "")
            self.endpoint_edit.setText(params.get("endpoint_url", "") or "")
        layout.addRow("Bucket:", self.bucket_edit)
        layout.addRow("Access key:", self.access_key_edit)
        layout.addRow("Secret key:", self.secret_key_edit)
        layout.addRow("Endpoint URL:", self.endpoint_edit)
        hint = QLabel("Works with AWS S3, MinIO, or any other S3-compatible self-hosted service.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #9a9a9a; font-size: 11px;")
        layout.addRow(hint)

    def params(self) -> dict:
        return {
            "bucket": self.bucket_edit.text().strip(),
            "access_key": self.access_key_edit.text().strip(),
            "secret_key": self.secret_key_edit.text(),
            "endpoint_url": self.endpoint_edit.text().strip() or None,
        }


class _ExternalDriveFields(QWidget):
    kind = "external"

    def __init__(self, params: Optional[dict] = None):
        super().__init__()
        layout = QFormLayout(self)
        self.path_combo = QComboBox()
        self.path_combo.setEditable(True)
        self.path_combo.setPlaceholderText("Select or type a path…")
        # Populate with detected drives
        detected = detect_external_drives()
        if detected:
            self.path_combo.addItem("— Detected drives —", None)
            for label, mp in detected:
                self.path_combo.addItem(f"{label}  ({mp})", mp)
            self.path_combo.insertSeparator(self.path_combo.count())
        self.path_combo.addItem("Type a custom path…", None)
        if params:
            self.path_combo.setCurrentText(params.get("path", ""))
        layout.addRow("Drive / Path:", self.path_combo)
        hint = QLabel(
            "Detected external drives are listed above. If your drive isn't "
            "shown, type its mount point manually (e.g. D:\\ on Windows, "
            "/media/user/Drive on Linux)."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #9a9a9a; font-size: 11px;")
        layout.addRow(hint)

    def params(self) -> dict:
        text = self.path_combo.currentText().strip()
        data = self.path_combo.currentData()
        if data is not None:
            return {"path": data}
        # Maybe the user selected "Type a custom path…" or typed something
        # If the text looks like a detected drive entry, extract the path
        if "  (" in text and text.endswith(")"):
            mp = text.split("  (", 1)[1].rstrip(")")
            return {"path": mp}
        return {"path": text}


_KIND_FIELD_CLASSES = {
    "Local folder / USB / mounted SMB-NFS": _LocalFields,
    "External / Removable Drive": _ExternalDriveFields,
    "SFTP": _SFTPFields,
    "WebDAV (Nextcloud/ownCloud/Seafile/...)": _WebDAVFields,
    "S3-compatible (AWS/MinIO/...)": _S3Fields,
}


class AddDestinationDialog(QDialog):
    def __init__(self, parent=None, existing=None):
        super().__init__(parent)
        self._existing = existing
        if existing:
            self.setWindowTitle(f"Edit Destination: {existing.name}")
        else:
            self.setWindowTitle("Add Destination")
        self.resize(420, 320)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit()
        if existing:
            self.name_edit.setText(existing.name)
        form.addRow("Name:", self.name_edit)

        self.kind_combo = QComboBox()
        self.kind_combo.addItems(list(_KIND_FIELD_CLASSES.keys()))
        if existing:
            # Select the matching kind label
            for label, cls in _KIND_FIELD_CLASSES.items():
                if cls.kind == existing.kind:
                    self.kind_combo.setCurrentText(label)
                    break
            self.kind_combo.setEnabled(False)  # can't change kind
        form.addRow("Type:", self.kind_combo)
        layout.addLayout(form)

        self.fields_container = QVBoxLayout()
        layout.addLayout(self.fields_container)
        self._current_fields: Optional[QWidget] = None
        self.kind_combo.currentTextChanged.connect(self._rebuild_fields)
        self._rebuild_fields(self.kind_combo.currentText())

        options_form = QFormLayout()
        self.compress_checkbox = QCheckBox("Enable compression")
        self.compress_checkbox.setChecked(existing.compression if existing else True)
        self.encrypt_checkbox = QCheckBox("Enable encryption (passphrase supplied at push time, never stored)")
        if existing:
            self.encrypt_checkbox.setChecked(existing.encryption)
        self.bandwidth_spin = QSpinBox()
        self.bandwidth_spin.setRange(0, 1_000_000)
        self.bandwidth_spin.setSpecialValueText("Unlimited")
        self.bandwidth_spin.setSuffix(" kbps")
        if existing and existing.bandwidth_limit_kbps:
            self.bandwidth_spin.setValue(existing.bandwidth_limit_kbps)
        options_form.addRow(self.compress_checkbox)
        options_form.addRow(self.encrypt_checkbox)
        options_form.addRow("Bandwidth limit:", self.bandwidth_spin)
        layout.addLayout(options_form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _rebuild_fields(self, kind_label: str) -> None:
        if self._current_fields is not None:
            self._current_fields.setParent(None)
        field_cls = _KIND_FIELD_CLASSES[kind_label]
        params = self._existing.params if self._existing else None
        self._current_fields = field_cls(params=params)
        self.fields_container.addWidget(self._current_fields)

    def result_data(self) -> dict:
        return {
            "name": self.name_edit.text().strip(),
            "kind": self._current_fields.kind,
            "params": self._current_fields.params(),
            "compression": self.compress_checkbox.isChecked(),
            "encryption": self.encrypt_checkbox.isChecked(),
            "bandwidth_limit_kbps": self.bandwidth_spin.value() or None,
        }


class DestinationsDialog(QDialog):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Backup Destinations")
        self.resize(500, 400)
        self.store = store
        self.manager = DestinationManager(store)

        layout = QVBoxLayout(self)
        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget)

        button_row = QHBoxLayout()
        add_btn = QPushButton("Add…")
        add_btn.clicked.connect(self._add_destination)
        edit_btn = QPushButton("Edit…")
        edit_btn.clicked.connect(self._edit_selected)
        test_btn = QPushButton("Test Connection")
        test_btn.clicked.connect(self._test_selected)
        delete_btn = QPushButton("Delete")
        delete_btn.clicked.connect(self._delete_selected)
        for b in (add_btn, edit_btn, test_btn, delete_btn):
            button_row.addWidget(b)
        layout.addLayout(button_row)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)

        self._refresh()

    def _refresh(self) -> None:
        self.list_widget.clear()
        for dest in self.manager.list():
            flags = []
            if dest.compression:
                flags.append("compressed")
            if dest.encryption:
                flags.append("encrypted")
            if dest.bandwidth_limit_kbps:
                flags.append(f"{dest.bandwidth_limit_kbps}kbps")
            label = f"{dest.name}  [{dest.kind}]" + (f"  ({', '.join(flags)})" if flags else "")
            item = QListWidgetItem(label)
            item.setData(1000, dest.id)
            self.list_widget.addItem(item)

    def _selected_id(self) -> Optional[str]:
        item = self.list_widget.currentItem()
        return item.data(1000) if item else None

    def _add_destination(self) -> None:
        dialog = AddDestinationDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        data = dialog.result_data()
        if not data["name"]:
            QMessageBox.warning(self, "Name required", "Please enter a name for the destination.")
            return
        try:
            self.manager.create(
                name=data["name"],
                kind=data["kind"],
                params=data["params"],
                compression=data["compression"],
                encryption=data["encryption"],
                bandwidth_limit_kbps=data["bandwidth_limit_kbps"],
            )
        except DestinationValidationError as exc:
            QMessageBox.critical(self, "Could not add destination", str(exc))
            return
        self._refresh()

    def _edit_selected(self) -> None:
        dest_id = self._selected_id()
        if not dest_id:
            QMessageBox.information(self, "Select a destination", "Choose a destination from the list first.")
            return
        config = self.manager.get(dest_id)
        dialog = AddDestinationDialog(self, existing=config)
        if dialog.exec() != QDialog.Accepted:
            return
        data = dialog.result_data()
        if not data["name"]:
            QMessageBox.warning(self, "Name required", "Please enter a name for the destination.")
            return
        try:
            self.manager.update(
                dest_id,
                name=data["name"],
                params=data["params"],
                compression=data["compression"],
                encryption=data["encryption"],
                bandwidth_limit_kbps=data["bandwidth_limit_kbps"],
            )
        except DestinationValidationError as exc:
            QMessageBox.critical(self, "Could not update destination", str(exc))
            return
        self._refresh()

    def _test_selected(self) -> None:
        dest_id = self._selected_id()
        if not dest_id:
            QMessageBox.information(self, "Select a destination", "Choose a destination from the list first.")
            return
        config = self.manager.get(dest_id)
        try:
            destination = build_destination(config)
        except (DestinationValidationError, ImportError) as exc:
            QMessageBox.critical(self, "Cannot test destination", str(exc))
            return
        ok, message = destination.test_connection()
        (QMessageBox.information if ok else QMessageBox.warning)(self, "Connection test", message)

    def _delete_selected(self) -> None:
        dest_id = self._selected_id()
        if not dest_id:
            QMessageBox.information(self, "Select a destination", "Choose a destination from the list first.")
            return
        confirm = QMessageBox.question(
            self, "Delete destination?",
            "This only removes the destination's local configuration -- any data already "
            "pushed to it is left untouched. Continue?",
        )
        if confirm == QMessageBox.Yes:
            self.manager.delete(dest_id)
            self._refresh()
