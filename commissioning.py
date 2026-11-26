"""Operator-initiated commissioning checks. At most one read-only probe in flight."""
import gzip
import json
import tempfile
import time
from collections import deque
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QStandardPaths, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from display import eastern
from station import Journal

CHECKS = {
    "Ground station": [
        ("codec", "Radio message codec", "Round-trip a real application message through the installed Jazzy CDR schema."),
        ("record", "Session recording", "Write and read a disposable compressed GS journal in the log directory."),
        ("radio", "Radio round trip", "Send a unique request through the field supervisor and match its reply."),
    ],
    "Jetson and network": [
        ("resources", "Memory and storage", "Measure available RAM and workspace disk space on the Jetson."),
        ("router", "RUTX50 LAN", "Send one ICMP echo from the Jetson to its configured router address."),
    ],
    "Lynx and localization": [
        ("battery", "Battery telemetry", "Read a new timestamped platform battery percentage."),
        ("estop", "E-stop input", "Read the next platform E-stop indication."),
        ("odom", "Odometry", "Validate a new stamped pose, quaternion, coordinate frames and velocity."),
        ("gnss", "GNSS monitor", "Read the next receiver health indication from the configured GNSS monitor."),
        ("scan", "Navigation scan", "Check scan geometry and count finite in-range returns."),
        ("geographic", "RTK map alignment", "Compare a screened RTK fix to its capture-time antenna TF, then round-trip the geographic conversion."),
        ("tf", "Map transform", "Resolve a current map-to-base pose through the ROS transform tree."),
    ],
    "Camera and perception": [
        ("camera", "ZED RGB and depth", "Match a fresh RGB frame, organized XYZ image and calibration by optical frame and capture stamp."),
        ("perception", "Perception to mission", "Check that a fresh completed inference observation reaches the mission adapter."),
    ],
    "Mission and manipulation": [
        ("mission", "FSM and FOD queue", "Check a new state snapshot and validate its queue identifiers."),
        ("guard", "Command guard output", "Observe a new stamped planar command from the sole guard publisher."),
        ("nav", "Nav2 lifecycle", "Wait for a fresh lifecycle query response in the field ROS domain."),
        ("arm", "UR5e joint telemetry", "Check fresh, finite positions for each of the six arm joints."),
        ("clean", "Cleaning action interface", "Query the cleaning action for an unissued goal ID and verify the protocol reply."),
    ],
}
STATUS = {"P": "Passed", "F": "Failed", "S": "Skipped", "U": "Hardware Unavailable"}
COLORS = {"Passed": "#16634d", "Failed": "#a73030", "Running": "#1756a2",
          "Hardware Unavailable": "#7d5312", "Skipped": "#5c6571", "Not Run": "#687582"}


def local_check(key, owner):
    if key == "codec":
        msg = owner.codec.types["AppCommand"](owner.codec.stamp(), uuid4().hex, "diagnose", "commissioning", "radio", False)
        blob = owner.codec.encode(msg)
        if owner.codec.decode(blob, msg.__msgtype__) != msg:
            raise ValueError("Decoded command differs from its original fields")
        return f"Encoded and decoded {len(blob)} CDR bytes with matching fields."
    if getattr(owner, "journal", None):
        root = owner.journal.folder.parent
    else:
        root = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)) / "logs"
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="commissioning-", dir=root) as tmp:
        journal = Journal(tmp)
        token = uuid4().hex
        try:
            journal.start_recording()
            journal.log("commissioning_probe", token)
        finally:
            journal.close()
        with gzip.open(journal.record_path, "rt", encoding="utf-8") as src:
            rows = [json.loads(line) for line in src]
        if not any(row["detail"] == token and row["utc_ns"] > 0 for row in rows):
            raise ValueError("Readback did not contain the timestamped test record")
    return "Wrote, closed and reopened a timestamped compressed journal in the GS log directory."


class CommissioningDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle("Run tests")
        self.resize(880, 540)
        self.rows, self.buttons = {}, []
        self.pending = deque()
        self.active = self.request = None
        self.started = 0.
        layout = QVBoxLayout(self)
        self.hint = QLabel("Remote checks read the field ROS domain. The isolated simulation has its own test suite.")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Subsystem / test", "Status", "Duration", "Last run (ET)", ""])
        self.tree.setAlternatingRowColors(True)
        self.tree.setAccessibleName("Commissioning tests and results")
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, 5):
            self.tree.header().setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.tree, 1)
        for group, checks in CHECKS.items():
            parent = QTreeWidgetItem(self.tree, [group])
            font = parent.font(0)
            font.setBold(True)
            parent.setFont(0, font)
            keys = [key for key, _, _ in checks]
            self.run_button(parent, "Run category tests", keys)
            for key, title, desc in checks:
                item = QTreeWidgetItem(parent, [title, "Not Run", "—", "—"])
                item.setToolTip(0, desc)
                info = QTreeWidgetItem(item, [desc])
                info.setFirstColumnSpanned(True)
                info.setToolTip(0, desc)
                self.rows[key] = (item, info, desc)
                self.run_button(item, "Run", [key])
            parent.setExpanded(group == "Ground station")
        bar = QHBoxLayout()
        self.summary = QLabel("Choose a test or run a category.")
        bar.addWidget(self.summary, 1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel)
        bar.addWidget(self.cancel_button)
        self.run_all = QPushButton("Run all safe tests")
        self.run_all.setObjectName("primary")
        self.run_all.clicked.connect(lambda: self.start(list(self.rows)))
        self.buttons.append(self.run_all)
        bar.addWidget(self.run_all)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        bar.addWidget(close)
        layout.addLayout(bar)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.advance)

    def run_button(self, item, name, keys):
        button = QPushButton(name)
        button.setAccessibleName(f"{name} · {item.text(0)}")
        button.clicked.connect(lambda checked=False: self.start(keys))
        self.tree.setItemWidget(item, 4, button)
        self.buttons.append(button)

    def start(self, keys):
        if self.timer.isActive() or not keys:
            return
        self.pending.extend(keys)
        for button in self.buttons:
            button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.timer.start()
        self.advance()

    def ready(self):
        owner = self.owner
        return bool(owner.radio and owner.radio.ready and owner.station and owner.app_fresh())

    def send(self, action):
        owner = self.owner
        msg = owner.codec.types["AppCommand"](
            owner.codec.stamp(), self.request, action, "commissioning", self.active, False)
        owner.station.send(21, msg, f"{action} · {self.active}")

    def advance(self):
        if self.active:
            elapsed = time.monotonic() - self.started
            self.rows[self.active][0].setText(2, f"{elapsed:.1f} s")
            if not self.ready():
                self.complete("Failed", "Field link or supervisor status was lost during the check.")
            elif elapsed >= 10:
                self.complete("Failed", "No matching reply within 10 s. Check supervisor version, clocks and link.")
            return
        if not self.pending:
            self.timer.stop()
            self.cancel_button.setEnabled(False)
            for btn in self.buttons:
                btn.setEnabled(True)
            self.summary.setText("Finished. Expand a test to inspect its result.")
            return
        key = self.active = self.pending.popleft()
        self.started = time.monotonic()
        self.request = uuid4().hex
        item, info, desc = self.rows[key]
        self.show_status(item, "Running")
        item.setText(3, eastern())
        info.setText(0, desc)
        self.summary.setText("Checking " + item.text(0) + "…")
        try:
            if key in ("codec", "record"):
                self.complete("Passed", local_check(key, self.owner))
            elif not self.ready():
                self.complete("Hardware Unavailable", "A leased radio link and fresh Jetson supervisor status are required.")
            else:
                self.send("diagnose")
        except Exception as exc:  # noqa: BLE001 - record third-party codec and serial errors per check
            self.complete("Failed", str(exc))

    def reply(self, raw):
        if len(raw) > 512 or self.active is None:
            return
        try:
            request, key, code, detail = json.loads(raw)
        except (ValueError, TypeError, RecursionError):
            return
        if (request == self.request and key == self.active and isinstance(code, str)
                and code in STATUS and isinstance(detail, str) and 0 < len(detail) <= 150):
            self.complete(STATUS[code], detail)

    def show_status(self, item, state):
        item.setText(1, state)
        item.setForeground(1, QColor(COLORS[state]))

    def complete(self, status, message):
        key = self.active
        self.active = self.request = None
        item, info, desc = self.rows[key]
        self.show_status(item, status)
        elapsed = time.monotonic() - self.started
        item.setText(2, f"{elapsed:.1f} s")
        info.setText(0, message)
        info.setToolTip(0, desc + "\n" + message)
        if self.owner.journal:
            try:
                self.owner.journal.log("commissioning", message, test=key, status=status,
                                       duration_s=round(elapsed, 3))
            except OSError as exc:
                info.setText(0, message + " · Journal write failed: " + str(exc))

    def cancel(self):
        if self.active:
            if self.ready():
                try:
                    self.send("cancel-check")
                except (OSError, ValueError, RuntimeError):
                    pass  # The supervisor also expires each individual probe after four seconds.
            self.complete("Skipped", "Canceled by operator.")
        for key in self.pending:
            item, info, _ = self.rows[key]
            self.show_status(item, "Skipped")
            item.setText(2, "—")
            item.setText(3, "—")
            info.setText(0, "Canceled before execution.")
        self.pending.clear()
        self.advance()

    def done(self, result):
        if self.timer.isActive():
            self.cancel()
        super().done(result)
