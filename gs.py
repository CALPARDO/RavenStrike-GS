"""Run the field ground station with python gs.py. Nothing transmits on startup."""

import math
import queue
import sys
import time
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, QTimer
from PySide6.QtGui import QImageReader, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from serial.tools import list_ports

from radio import Radio
from runway_map import RunwayMap
from station import Journal, Station
from wire import Codec


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.codec = Codec()
        self.radio = self.station = self.journal = None
        self.shown = None
        self.presentation = None
        self.last_rx = None
        self.setWindowTitle("RavenStrike | Field ground station")
        widget = QWidget()
        self.setCentralWidget(widget)
        layout = QVBoxLayout(widget)
        title = QLabel("RavenStrike   /   Ground station")
        title.setStyleSheet("font-size:22px; font-weight:600")
        layout.addWidget(title)
        connection = QHBoxLayout()
        layout.addLayout(connection)
        self.port = QComboBox()
        self.port.setEditable(True)
        connection.addWidget(self.port, 1)
        self.refresh = self.button(connection, "Refresh ports", self.ports)
        self.connect_button = self.button(connection, "Connect", self.connect_radio)
        self.link = QLabel()
        connection.addWidget(self.link)
        controls = QHBoxLayout()
        layout.addLayout(controls)
        self.start = self.button(controls, "Start session", self.start_session)
        self.scan = self.button(
            controls,
            "Scanning complete",
            lambda: self.confirm("scan_complete", "Finish scanning and drain the queue?"),
        )
        self.abort = self.button(
            controls,
            "Abort to base",
            lambda: self.confirm("abort", "Request abort and return to base?"),
        )
        self.status = QLabel("No active session")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        form = QHBoxLayout()
        layout.addLayout(form)
        self.coords = []
        for field in ("Latitude °", "Longitude °", "Ellipsoid altitude m"):
            edit = QLineEdit()
            edit.setPlaceholderText(field)
            edit.setAccessibleName(field)
            self.coords.append(edit)
            form.addWidget(edit)
        self.button(form, "Preview on map", self.map)
        self.submit = self.button(form, "Send FOD", self.add)
        self.runway = RunwayMap()
        layout.addWidget(self.runway)
        self.tasks = QPlainTextEdit()
        self.tasks.setReadOnly(True)
        self.tasks.setPlaceholderText("Submitted locations and the UGV-reported queue")
        layout.addWidget(self.tasks)
        box = QGroupBox("Operator review")
        layout.addWidget(box)
        review = QGridLayout(box)
        self.pictures = []
        for i, name in enumerate(("Before", "Review")):
            label = QLabel(name)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setMinimumSize(200, 100)
            review.addWidget(label, 0, i)
            self.pictures.append(label)
        self.review_text = QLabel()
        self.review_text.setTextFormat(Qt.TextFormat.PlainText)
        review.addWidget(self.review_text, 1, 0, 1, 2)
        actions = QHBoxLayout()
        review.addLayout(actions, 2, 0, 1, 2)
        self.approve = self.button(actions, "Continue", lambda: self.decide("approve"))
        self.retry = self.button(actions, "Clean again", lambda: self.decide("retry"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(400)
        self.log.setPlaceholderText("Radio and mission events")
        layout.addWidget(self.log)
        self.ports()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(100)
        self.tick()

    def button(self, row, name, fn):
        button = QPushButton(name)

        def invoke():
            try:
                fn()
            except OSError as err:
                self.log_failure(err)
            except (ValueError, RuntimeError) as err:
                self.log.appendPlainText(str(err))
                if self.journal:
                    self.journal.log("operator_error", str(err))

        button.clicked.connect(invoke)
        row.addWidget(button)
        return button

    def ports(self):
        self.port.clear()
        for p in sorted(list_ports.comports(), key=lambda p: (p.vid is None, p.device)):
            self.port.addItem(p.device)

    def connect_radio(self):
        if self.radio:
            if (
                self.station.operating
                and QMessageBox.question(self, "Disconnect", "Disconnect and stop the heartbeat?")
                != QMessageBox.StandardButton.Yes
            ):
                return
            self.disconnect()
            return
        port = self.port.currentText().strip()
        if not port:
            raise ValueError("Select the USB radio serial port.")
        root = Path(__file__).resolve().parent
        if getattr(sys, "frozen", False):
            root = Path(
                QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
            )
        self.journal = Journal(root / "logs")
        self.journal.log("connect", port)
        self.log.appendPlainText("Log directory: " + str(self.journal.folder))
        self.radio = Radio(port, self.codec)
        self.station = Station(self.codec, self.radio, self.journal)
        self.radio.start()
        self.last_rx = None
        self.connect_button.setText("Disconnect")

    def disconnect(self):
        if self.radio:
            self.radio.close()
            self.drain()
        if self.journal:
            self.journal.log("disconnect", "Operator closed connection")
            self.journal.close()
        self.radio = self.station = self.journal = None
        self.connect_button.setText("Connect")
        self.last_rx = None
        self.tick()

    def start_session(self):
        if self.station.state == "awaiting acceptance":
            self.station.retry_start()
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Start supervised session")
        form = QFormLayout(dialog)
        edits = []
        for label, default in [
            ("UGV callsign", "UGV-1"),
            ("Area ID", ""),
            ("Home map X (m)", ""),
            ("Home map Y (m)", ""),
            ("Home heading (degrees)", ""),
        ]:
            edit = QLineEdit(default)
            form.addRow(label, edit)
            edits.append(edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        form.addRow(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec():
            callsign, area, x, y, yaw = [e.text().strip() for e in edits]
            self.station.start(callsign, area, (float(x), float(y), math.radians(float(yaw))))
            self.runway.targets.clear()
            self.runway.update()

    def map(self):
        lat, lon = [float(e.text()) for e in self.coords[:2]]
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("Enter valid latitude and longitude first.")
        self.runway.preview(lat, lon)

    def add(self):
        coords = tuple(float(e.text()) for e in self.coords)
        mission = self.station.add(*coords)
        self.runway.targets[mission] = coords[:2]
        if abs(coords[0]) <= 85.0511:
            self.runway.preview(*coords[:2])
        self.log.appendPlainText("Sent " + mission + "; awaiting UGV report")
        for e in self.coords:
            e.clear()

    def confirm(self, command, prompt):
        if QMessageBox.question(self, "Mission control", prompt) == QMessageBox.StandardButton.Yes:
            self.station.command(command)

    def decide(self, decision):
        self.station.decide(self.shown, decision)
        self.log.appendPlainText("Sent " + decision + "; awaiting UGV report")
        self.tick()

    def drain(self):
        for _ in range(2048):
            try:
                when, event, data = self.radio.events.get_nowait()
            except queue.Empty:
                break
            if event == "incoming":
                topic = data["topic"]
                try:
                    msg = self.codec.decode(data["payload"], self.radio.engine.topics[topic].type)
                    self.station.receive(topic, msg, when)
                    self.last_rx = time.monotonic()
                    self.log.appendPlainText(
                        getattr(msg, "mission_id", "")
                        + " "
                        + (getattr(msg, "notes", "") or getattr(msg, "kind", ""))
                    )
                except OSError:
                    raise
                except Exception as err:  # noqa: BLE001 - reject malformed incoming data and keep the operator loop alive
                    self.journal.log("invalid_message", str(err), received=when, topic=topic)
                    self.log.appendPlainText("Invalid message: " + str(err))
            else:
                detail = data.get("detail", str(data))
                self.journal.log(event, detail, received=when, fields=data)
                if event not in ("ack", "nack"):
                    self.log.appendPlainText(event + " " + detail)

    def log_failure(self, err):
        if self.radio:
            self.radio.close()
        if self.journal:
            try:
                self.journal.close()
            except OSError:
                pass
        self.radio = self.journal = self.station = None
        self.connect_button.setText("Connect")
        self.log.appendPlainText("Ground station disconnected. Check log storage: " + str(err))

    def tick(self):
        if self.radio:
            try:
                self.drain()
            except OSError as err:
                self.log_failure(err)
        if self.radio:
            self.radio.ui_seen = time.monotonic()
        ready = bool(self.radio and self.radio.ready)
        st = self.station
        self.port.setEnabled(self.radio is None)
        self.refresh.setEnabled(self.radio is None)
        self.link.setText(
            ("Ready" if ready else "Link unavailable") if self.radio else "Disconnected"
        )
        self.start.setText(
            "Retry start" if st and st.state == "awaiting acceptance" else "Start session"
        )
        self.start.setEnabled(
            ready
            and (
                not st.session
                or st.state
                in ("completed", "canceled", "halted", "rejected", "awaiting acceptance")
            )
        )
        self.abort.setEnabled(bool(ready and st.operating))
        self.submit.setEnabled(
            bool(ready and st.operating and not st.scanned and not st.scanning_requested)
        )
        self.scan.setEnabled(self.submit.isEnabled())
        age = (
            f"   ·   Last UGV update {time.monotonic() - self.last_rx:.0f}s ago"
            if self.last_rx
            else ""
        )
        self.status.setText(
            f"{st.callsign}   {st.state}   {st.note}   {age}"
            if st and st.session
            else "No active session"
        )
        active = st.active if st else ""
        if self.runway.active != active:
            self.runway.active = active
            self.runway.update()
        queue_text = ""
        if st:
            queue_text = (
                "Active: "
                + (st.active or "—")
                + "    Queue: "
                + (", ".join(st.queue) or "—")
                + "\n"
            )
            queue_text += "\n".join(f"{k}    {v}" for k, v in st.targets.items())
        if self.tasks.toPlainText() != queue_text:
            self.tasks.setPlainText(queue_text)
        token = st.review_token() if st else None
        before = st.latest.get((st.active, "before")) if st and token else None
        if (token, before) != self.presentation:
            self.presentation = (token, before)
            self.shown = token
            kind = st.records[token][0] if token else ""
            self.approve.setText("Approve removal" if kind == "after" else "Continue")
            self.retry.setText("Search again" if kind == "not_found" else "Clean again")
            display = {
                "cycle_complete": "Cleaning cycle finished · Removal unverified",
                "after": "Inspect the after image",
                "not_found": "FOD not found · Inspect the search area",
            }
            self.review_text.setText(
                st.active + "   " + display[kind] if token else "Awaiting current review"
            )
            for label, name, key in zip(self.pictures, ("Before", "Review"), ("before", "review")):
                label.setText(name)
                if token:
                    item = st.latest.get((st.active, key))
                    path = st.records[item][1] if item else None
                    if path:
                        reader = QImageReader(str(path))
                        size = reader.size()
                        if 0 < size.width() <= 320 and 0 < size.height() <= 240:
                            label.setPixmap(QPixmap.fromImage(reader.read()))
        image_ok = bool(
            token
            and (st.records[token][0] == "cycle_complete" or not self.pictures[1].pixmap().isNull())
        )
        self.approve.setEnabled(ready and image_ok)
        self.retry.setEnabled(
            bool(ready and token and st.records[token][0] != "cycle_complete" and image_ok)
        )

    def closeEvent(self, event):
        if (
            self.station
            and self.station.operating
            and QMessageBox.question(self, "Exit", "Exit and stop the GS heartbeat?")
            != QMessageBox.StandardButton.Yes
        ):
            event.ignore()
            return
        self.disconnect()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setApplicationName("RavenStrike-GS")
    window = Window()
    window.resize(880, 680)
    window.show()
    if "--self-test" in sys.argv:
        # Exercise the packaged GUI, wire map and dynamically loaded CDR store offline.
        app.processEvents()
        radio = Radio("", window.codec)
        task = window.codec.gps("offline", "fod-1", "UGV-1", 1.0, 2.0, 0.0)
        if window.codec.decode(window.codec.encode(task), task.__msgtype__) != task:
            raise RuntimeError("Packaged GPS task failed the CDR round trip")
        if window.submit.isEnabled() or radio.ready:
            raise RuntimeError("Disconnected station accepted mission input")
        window.close()
    else:
        sys.exit(app.exec())
