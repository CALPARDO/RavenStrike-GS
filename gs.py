"""Run the field ground station with python gs.py. Nothing transmits on startup."""

import math
import queue
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from serial.tools import list_ports

from appearance import field_theme
from location import LocationTrack
from radio import Radio
from runway_map import RunwayMap
from station import Journal, Station
from wire import WIRE_MAP_VERSION, Codec


def build_id():
    root = Path(__file__).resolve().parent
    if getattr(sys, "frozen", False):
        stamp = root / "BUILD_ID"
        return stamp.read_text(encoding="utf-8").strip() if stamp.is_file() else "unknown"
    if (root / ".git").exists():
        try:
            return subprocess.check_output(
                [
                    "git",
                    "describe",
                    "--always",
                    "--exclude",
                    "*",
                    "--dirty",
                    "--abbrev=12",
                ],
                cwd=root,
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=1,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return "source"


def label(text, name=""):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setObjectName(name)
    return widget


def card(column, stretch=0):
    panel = QFrame()
    panel.setObjectName("card")
    column.addWidget(panel, stretch)
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(14, 12, 14, 12)
    layout.setSpacing(8)
    return layout


class EvidencePreview(QLabel):
    def __init__(self):
        super().__init__("No image")
        self.original = QPixmap()
        self.setObjectName("evidence")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumWidth(80)
        self.setFixedHeight(115)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def set_image(self, image):
        self.original = image
        if image.isNull():
            self.setText("No image")
        else:
            self.fit_image()

    def fit_image(self):
        if not self.original.isNull():
            self.setPixmap(
                self.original.scaled(
                    self.contentsRect().size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_image()


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        field_theme(QApplication.instance())
        self.build = build_id()
        self.codec = Codec()
        self.track = LocationTrack()
        self.radio = self.station = self.journal = None
        self.log_folder = self.last_rx = self.presentation = self.shown = None
        self.queue_rows = self.link_state = self.last_event = None
        self.setWindowTitle("RavenStrike | Field ground station")
        root = QWidget()
        self.setCentralWidget(root)
        shell = QVBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        header = QFrame()
        header.setObjectName("header")
        bar = QHBoxLayout(header)
        bar.setContentsMargins(18, 10, 18, 10)
        mark = QLabel()
        mark.setPixmap(self.windowIcon().pixmap(34, 34))
        bar.addWidget(mark)
        bar.addWidget(label("RavenStrike", "brand"))
        bar.addWidget(label("Ground station", "muted"))
        bar.addStretch()
        self.link = label("", "link")
        bar.addWidget(self.link)
        self.port = QComboBox()
        self.port.setEditable(True)
        self.port.setAccessibleName("Radio serial port")
        self.port.lineEdit().setPlaceholderText("Serial port")
        self.port.setMinimumWidth(105)
        self.port.setMaximumWidth(190)
        bar.addWidget(self.port)
        self.refresh = self.button(bar, "Refresh", self.ports)
        self.refresh.setToolTip("Refresh serial ports")
        self.connect_button = self.button(bar, "Connect", self.connect_radio)
        self.connect_button.setObjectName("primary")
        shell.addWidget(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        body.setObjectName("workspace")
        scroll.setWidget(body)
        shell.addWidget(scroll, 1)
        columns = QHBoxLayout(body)
        columns.setContentsMargins(16, 12, 16, 12)
        columns.setSpacing(14)
        left, right = QVBoxLayout(), QVBoxLayout()
        left.setSpacing(12)
        right.setSpacing(12)
        columns.addLayout(left, 3)
        columns.addLayout(right, 2)
        self.map_panel(left)
        self.mission_panel(right)
        self.activity_panel(right)

        footer = QFrame()
        footer.setObjectName("footer")
        foot = QHBoxLayout(footer)
        foot.setContentsMargins(16, 5, 16, 5)
        self.feedback = label("Select a radio port to connect.", "muted")
        self.feedback.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        foot.addWidget(self.feedback, 1)
        self.last_update = label("", "muted")
        foot.addWidget(self.last_update)
        foot.addWidget(label(f"Build {self.build}   ·   Wire v{WIRE_MAP_VERSION}", "muted"))
        self.copy_info = self.button(foot, "Copy diagnostics", self.copy_diagnostics)
        shell.addWidget(footer)
        self.ports()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(100)
        self.tick()
        self.resize(1120, 740)

    def map_panel(self, column):
        pane = card(column, stretch=1)
        bar = QHBoxLayout()
        pane.addLayout(bar)
        bar.addWidget(label("Mission map", "heading"))
        bar.addStretch()
        self.runway = RunwayMap()
        self.locate = self.button(bar, "Locate UGV", self.locate_robot)
        self.zoom_out = self.button(bar, "−", lambda: self.runway.change_zoom(-1))
        self.zoom_in = self.button(bar, "+", lambda: self.runway.change_zoom(1))
        for button, name in [(self.zoom_out, "Zoom out"), (self.zoom_in, "Zoom in")]:
            button.setFixedWidth(35)
            button.setAccessibleName(name)
            button.setToolTip(name)
        pane.addWidget(self.runway, 1)
        self.map_caption = label("WGS84   ·   No location selected", "muted")
        pane.addWidget(self.map_caption)
        self.robot_caption = label("", "muted")
        pane.addWidget(self.robot_caption)
        pane = card(column)
        pane.addWidget(label("Send a FOD location", "heading"))
        fields = QHBoxLayout()
        pane.addLayout(fields)
        self.coords = []
        for name, unit in (
            ("Latitude", "degrees"),
            ("Longitude", "degrees"),
            ("Altitude", "m, ellipsoid"),
        ):
            field = QVBoxLayout()
            fields.addLayout(field, 1)
            edit = QLineEdit()
            edit.setMinimumWidth(65)
            edit.setPlaceholderText(unit)
            caption = label(name, "field")
            caption.setBuddy(edit)
            field.addWidget(caption)
            field.addWidget(edit)
            self.coords.append(edit)
        row = QHBoxLayout()
        pane.addLayout(row)
        row.addStretch()
        self.preview = self.button(row, "Preview on map", self.map)
        self.submit = self.button(row, "Send FOD", self.add)
        self.submit.setObjectName("primary")

    def mission_panel(self, column):
        pane = card(column)
        pane.addWidget(label("Mission control", "heading"))
        self.status = label("")
        self.status.setWordWrap(True)
        pane.addWidget(self.status)
        self.active_target = label("", "muted")
        self.active_target.setWordWrap(True)
        pane.addWidget(self.active_target)
        self.start = self.button(pane, "Start session", self.start_session)
        row = QHBoxLayout()
        pane.addLayout(row)
        self.scan = self.button(
            row,
            "Scanning complete",
            lambda: self.confirm("scan_complete", "Finish scanning and drain the queue?"),
        )
        self.abort = self.button(
            row,
            "Abort to base",
            lambda: self.confirm("abort", "Request abort and return to base?"),
        )
        self.abort.setObjectName("abort")

    def activity_panel(self, column):
        self.tabs = QTabWidget()
        column.addWidget(self.tabs, 1)
        targets = QWidget()
        self.tabs.addTab(targets, "Targets")
        pane = QVBoxLayout(targets)
        self.queue_caption = label("Target register", "heading")
        pane.addWidget(self.queue_caption)
        self.tasks = QTableWidget(0, 2)
        self.tasks.setAccessibleName("Mission targets")
        self.tasks.setHorizontalHeaderLabels(["FOD ID", "UGV status"])
        self.tasks.verticalHeader().hide()
        self.tasks.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.tasks.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tasks.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tasks.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tasks.setShowGrid(False)
        self.tasks.setAlternatingRowColors(True)
        self.tasks.itemActivated.connect(self.locate_row)
        pane.addWidget(self.tasks)
        self.empty_targets = label("Submitted and UGV-reported targets appear here.", "muted")
        self.empty_targets.setWordWrap(True)
        self.empty_targets.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pane.addWidget(self.empty_targets, 1)
        self.timeline = QPlainTextEdit()
        self.timeline.setReadOnly(True)
        self.timeline.setAccessibleName("Selected FOD timeline")
        self.timeline.setMaximumHeight(108)
        self.timeline.setPlaceholderText("Select a FOD to see its timeline.")
        pane.addWidget(self.timeline)
        self.tasks.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tasks.currentItemChanged.connect(self.update_timeline)
        self.review_page = QWidget()
        self.tabs.addTab(self.review_page, "Review")
        review = QVBoxLayout(self.review_page)
        review.addWidget(label("Operator review", "heading"))
        self.review_text = label("")
        self.review_text.setWordWrap(True)
        review.addWidget(self.review_text)
        images = QHBoxLayout()
        review.addLayout(images)
        self.pictures = []
        for name in ("Before", "Review"):
            tile = QVBoxLayout()
            images.addLayout(tile, 1)
            tile.addWidget(label(name, "field"))
            picture = EvidencePreview()
            tile.addWidget(picture)
            self.pictures.append(picture)
        self.inspect = self.button(review, "View original images", self.inspect_images)
        actions = QHBoxLayout()
        review.addLayout(actions)
        self.approve = self.button(actions, "Continue", lambda: self.decide("approve"))
        self.approve.setObjectName("primary")
        self.retry = self.button(actions, "Clean again", lambda: self.decide("retry"))
        review.addStretch()
        activity = QWidget()
        self.tabs.addTab(activity, "Activity")
        pane = QVBoxLayout(activity)
        bar = QHBoxLayout()
        pane.addLayout(bar)
        bar.addWidget(label("Event log", "heading"))
        bar.addStretch()
        self.open_logs = self.button(bar, "Open folder", self.open_log_folder)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(400)
        self.log.setPlaceholderText("Radio and operator events appear here.")
        pane.addWidget(self.log)

    def inspect_images(self):
        st = self.station
        if not self.shown or not st or self.shown != st.review_token():
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Review images · " + st.active)
        layout = QVBoxLayout(dialog)
        row = QHBoxLayout()
        layout.addLayout(row)
        for title, preview in zip(("Before", "Review"), self.pictures):
            if preview.original.isNull():
                continue
            tile = QVBoxLayout()
            row.addLayout(tile)
            tile.addWidget(label(title, "field"))
            picture = QLabel()
            picture.setPixmap(preview.original)
            tile.addWidget(picture)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(dialog.reject)
        layout.addWidget(close)
        dialog.exec()

    def copy_diagnostics(self):
        lines = [
            f"RavenStrike GS build {self.build}",
            f"Wire map v{WIRE_MAP_VERSION}",
            f"Port: {self.port.currentText().strip() or 'none selected'}",
            f"Link: {self.link.text()}",
            f"Log folder: {self.log_folder or 'no connection yet'}",
        ]
        QApplication.clipboard().setText("\n".join(lines))
        self.feedback.setText("Diagnostics copied.")

    def update_timeline(self):
        item = self.tasks.item(self.tasks.currentRow(), 0)
        entries = self.station.timeline.get(item.text(), []) if self.station and item else []
        text = "\n".join(entries)
        if self.timeline.toPlainText() != text:
            self.timeline.setPlainText(text)
            scroll = self.timeline.verticalScrollBar()
            scroll.setValue(scroll.maximum())

    def open_log_folder(self):
        if self.log_folder and not QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self.log_folder))
        ):
            raise ValueError(f"Open the log folder manually: {self.log_folder}")

    def locate_row(self, item):
        name = self.tasks.item(item.row(), 0).text()
        location = self.runway.targets.get(name)
        if location and abs(location[0]) <= 85.0511:
            self.runway.preview(*location)
        else:
            self.event_line(f"No mappable coordinates available for {name}.")

    def event_line(self, text):
        if text != self.last_event:
            self.log.appendPlainText(time.strftime("%H:%M:%S", time.gmtime()) + " UTC   " + text)
        self.last_event = text
        self.feedback.setText(text)
        self.feedback.setToolTip(text)

    def button(self, row, name, fn):
        button = QPushButton(name)

        def invoke():
            try:
                fn()
            except OSError as err:
                self.log_failure(err)
            except (ValueError, RuntimeError) as err:
                self.event_line(str(err))
                if self.journal:
                    self.journal.log("operator_error", str(err))

        button.clicked.connect(invoke)
        row.addWidget(button)
        return button

    def ports(self):
        previous = self.port.currentText()
        self.port.clear()
        for p in sorted(list_ports.comports(), key=lambda p: (p.vid is None, p.device)):
            self.port.addItem(p.device)
        if previous:
            self.port.setCurrentText(previous)

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
                QStandardPaths.writableLocation(
                    QStandardPaths.StandardLocation.AppLocalDataLocation
                )
            )
        self.journal = Journal(root / "logs")
        self.journal.log("connect", port, build=self.build, wire_map=WIRE_MAP_VERSION)
        self.log_folder = self.journal.folder
        self.event_line("Log directory: " + str(self.log_folder))
        self.radio = Radio(port, self.codec)
        self.station = Station(self.codec, self.radio, self.journal)
        self.track = LocationTrack()
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
        self.runway.reset_view()
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
            self.runway.reset_view()
            self.event_line("Start requested · Awaiting UGV acceptance")

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
        self.event_line("Sent " + mission + "; awaiting UGV report")
        for e in self.coords:
            e.clear()

    def confirm(self, command, prompt):
        if QMessageBox.question(self, "Mission control", prompt) == QMessageBox.StandardButton.Yes:
            self.station.command(command)
            self.event_line(
                "Scanning complete requested"
                if command == "scan_complete"
                else "Abort to base requested"
            )
            self.tick()

    def decide(self, decision):
        self.station.decide(self.shown, decision)
        self.event_line("Sent " + decision + "; awaiting UGV report")
        self.tick()

    def locate_robot(self):
        if self.runway.robot:
            self.runway.center = self.runway.robot[:2]
            self.runway.failed.clear()
            self.runway.update()

    def update_location(self, ready):
        pose = self.track.current() if ready else None
        mappable = pose if pose and abs(pose[0]) <= 85.0511 else None
        if self.runway.robot != mappable or (mappable and self.runway.center is None):
            self.runway.robot = mappable
            if mappable and self.runway.center is None:
                self.locate_robot()
            self.runway.update()
        self.locate.setEnabled(bool(mappable))
        if pose:
            lat, lon, bearing = pose
            course = "heading unavailable" if bearing is None else f"{bearing:.1f}° true"
            caption = f"UGV   {lat:.7f}°, {lon:.7f}°   ·   {course}"
        elif ready and self.track.position:
            caption = "UGV location stale"
        elif ready:
            caption = "Waiting for a fresh UGV location"
        else:
            caption = "UGV location unavailable"
        self.robot_caption.setText(caption)

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
                    if topic == 20:
                        if self.radio.ready and self.track.accept(msg):
                            self.last_rx = time.monotonic()
                            self.journal.log(
                                "location",
                                "UGV position",
                                received=when,
                                source_ns=self.track.stamp,
                                pose=self.track.position,
                            )
                        continue
                    self.station.receive(topic, msg, when)
                    self.last_rx = time.monotonic()
                    parts = [
                        getattr(msg, field, "")
                        for field in ("mission_id", "state", "status", "notes", "kind")
                    ]
                    self.event_line("UGV  ·  " + "  ·  ".join(part for part in parts if part))
                except OSError:
                    raise
                except Exception as err:  # noqa: BLE001 - reject malformed incoming data and keep the operator loop alive
                    self.journal.log("invalid_message", str(err), received=when, topic=topic)
                    self.event_line("Invalid message: " + str(err))
            else:
                detail = data.get("detail", str(data))
                self.journal.log(event, detail, received=when, fields=data)
                if event == "ack":
                    self.station.task_ack(data["topic"], data["wire_id"], when)
                if event not in ("ack", "nack"):
                    self.event_line(event + " " + detail)

    def log_failure(self, err):
        if self.radio:
            self.radio.close()
        if self.journal:
            try:
                self.journal.close()
            except OSError:
                pass
        self.radio = self.journal = self.station = None
        self.runway.reset_view()
        self.connect_button.setText("Connect")
        self.last_rx = None
        self.event_line("Ground station disconnected. Check log storage: " + str(err))

    def tick(self):
        if self.radio:
            try:
                self.drain()
            except OSError as err:
                self.log_failure(err)
        if self.radio:
            self.radio.ui_seen = time.monotonic()
        ready = bool(self.radio and self.radio.ready)
        self.update_location(ready)
        st = self.station
        self.port.setEnabled(self.radio is None)
        self.refresh.setEnabled(self.radio is None)
        state = "Disconnected"
        if self.radio:
            if ready:
                state = "Radio ready"
            elif self.radio.engine.lost or self.radio.overflow.is_set():
                state = "Link lost"
            elif self.radio.engine.port_error:
                state = "Port unavailable"
            elif self.radio.engine.config_match is False:
                state = "Contract mismatch"
            else:
                state = "Connecting"
        if self.link_state != state:
            self.link_state = state
            bg, fg = {
                "Disconnected": ("#e9eff0", "#476069"),
                "Connecting": ("#fff1cb", "#754b08"),
                "Link lost": ("#ffe4de", "#96382d"),
                "Port unavailable": ("#ffe4de", "#96382d"),
                "Contract mismatch": ("#ffe4de", "#96382d"),
                "Radio ready": ("#d6f1e8", "#125d4b"),
            }[state]
            self.link.setText(state)
            self.link.setStyleSheet(f"background: {bg}; color: {fg};")
        self.open_logs.setEnabled(self.log_folder is not None)
        self.start.setText(
            "Retry start" if st and st.state == "awaiting acceptance" else "Start session"
        )
        self.start.setEnabled(
            ready
            and (
                not st.session
                or st.state
                in (
                    "completed",
                    "canceled",
                    "halted",
                    "rejected",
                    "awaiting acceptance",
                )
            )
        )
        self.abort.setEnabled(bool(ready and st.operating))
        self.submit.setEnabled(
            bool(ready and st.operating and not st.scanned and not st.scanning_requested)
        )
        self.scan.setText(
            "Scanning closed"
            if st and st.scanned
            else "Scan requested"
            if st and st.scanning_requested
            else "Scanning complete"
        )
        self.abort.setText("Abort requested" if st and st.abort_requested else "Abort to base")
        self.scan.setEnabled(self.submit.isEnabled())
        self.last_update.setText(
            f"Last UGV message   {time.monotonic() - self.last_rx:.0f}s ago" if self.last_rx else ""
        )
        note = st.note if st else ""
        if note == "OK_TO_MOVE_FORWARD":
            note = "Waiting for operator review"
        elif note.isupper() and "_" in note:
            note = note.replace("_", " ").capitalize()
        self.status.setText(
            "   ·   ".join(x for x in (st.callsign, st.area, st.state.capitalize(), note) if x)
            if st and st.session
            else "No active session"
        )
        active = st.active if st else ""
        self.active_target.setText("Active target   " + (active or "—"))
        if self.runway.active != active:
            self.runway.active = active
            self.runway.update()
        location = self.runway.center
        self.zoom_out.setEnabled(bool(location and self.runway.zoom > 3))
        self.zoom_in.setEnabled(bool(location and self.runway.zoom < 19))
        self.map_caption.setText(
            f"WGS84   ·   Map center   {location[0]:.5f}°, {location[1]:.5f}°"
            if location
            else "WGS84   ·   No location selected"
        )
        self.update_targets(st, active)
        token = st.review_token() if st else None
        before = st.latest.get((st.active, "before")) if st and token else None
        if (token, before) != self.presentation:
            self.presentation = (token, before)
            if token and token != self.shown:
                self.tabs.setCurrentWidget(self.review_page)
            self.tabs.setTabText(1, "Review •" if token else "Review")
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
            for label, key in zip(self.pictures, ("before", "review")):
                label.set_image(QPixmap())
                if token:
                    item = st.latest.get((st.active, key))
                    path = st.records[item][1] if item else None
                    if path:
                        reader = QImageReader(str(path))
                        size = reader.size()
                        if 0 < size.width() <= 320 and 0 < size.height() <= 240:
                            label.set_image(QPixmap.fromImage(reader.read()))
        image_ok = bool(
            token
            and (st.records[token][0] == "cycle_complete" or not self.pictures[1].pixmap().isNull())
        )
        self.inspect.setEnabled(any(not image.original.isNull() for image in self.pictures))
        self.approve.setEnabled(ready and image_ok)
        self.retry.setEnabled(
            bool(ready and token and st.records[token][0] != "cycle_complete" and image_ok)
        )

    def update_targets(self, st, active):
        queued = st.queue if st else []
        reports = st.targets if st else {}
        names = dict.fromkeys(
            ([active] if active else [])
            + queued
            + list(reports)
            + (list(st.timeline) if st else [])
        )
        positions = {name: i for i, name in enumerate(queued, 1)}
        rows = []
        for name in names:
            role = (
                "Active"
                if name == active
                else f"Queue {positions[name]}"
                if name in positions
                else ""
            )
            rows.append((name, " · ".join(filter(None, (role, reports.get(name, ""))))))
        caption = "Target register"
        if st and st.last_status >= 0:
            caption += f"   ·   {len(queued)} queued"
        self.queue_caption.setText(caption)
        if rows == self.queue_rows:
            self.update_timeline()
            return
        previous = self.tasks.item(self.tasks.currentRow(), 0)
        selected = previous.text() if previous else active
        self.tasks.blockSignals(True)
        self.queue_rows = rows
        self.tasks.setVisible(bool(rows))
        self.empty_targets.setVisible(not rows)
        self.tasks.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, value in enumerate(row):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.tasks.setItem(i, j, item)
        if rows:
            self.tasks.selectRow(next((i for i, row in enumerate(rows) if row[0] == selected), 0))
        self.tasks.blockSignals(False)
        self.update_timeline()

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
    window.show()
    if "--self-test" in sys.argv:
        # Exercise the packaged GUI, wire map and dynamically loaded CDR store offline.
        app.processEvents()
        if getattr(sys, "frozen", False) and window.build == "unknown":
            raise RuntimeError("Packaged build ID is missing")
        radio = Radio("", window.codec)
        task = window.codec.gps("offline", "fod-1", "UGV-1", 1.0, 2.0, 0.0)
        if window.codec.decode(window.codec.encode(task), task.__msgtype__) != task:
            raise RuntimeError("Packaged GPS task failed the CDR round trip")
        location = window.codec.types["RobotLocation"](window.codec.stamp(), 0, 0, 65535)
        if (
            window.codec.decode(window.codec.encode(location), radio.engine.topics[20].type)
            != location
        ):
            raise RuntimeError("Packaged robot location failed the CDR round trip")
        if window.submit.isEnabled() or radio.ready:
            raise RuntimeError("Disconnected station accepted mission input")
        window.close()
    else:
        sys.exit(app.exec())
