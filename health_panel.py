"""Operator indications. All onboard states come from one latest-only status frame."""

from itertools import groupby
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
)

# Text describes the evidence each green indication actually represents.
PROBES = (
    ("MISSION", "Mission FSM", "Reporting", "Fault", "Fresh FSM snapshot"),
    ("NAV2", "Nav2", "Active", "Fault", "Navigation lifecycle manager reports active"),
    ("ODOMETRY", "Odometry", "Fresh", "Fault", "Fresh valid pose, orientation and velocity"),
    ("SCAN", "Spatial scan", "Fresh", "Fault", "Fresh scan containing usable ranges"),
    ("GNSS", "GNSS", "Healthy", "Unhealthy", "GNSS receiver health input"),
    (
        "PERCEPTION",
        "Perception",
        "Fresh",
        "Fault",
        "Fresh usable observations; an empty frame is valid",
    ),
    ("GUARD", "Command guard", "Listening", "Fault", "Guard discovered on its input topic"),
    ("CLEANING", "Cleaning", "Available", "Fault", "Cleaning action server is available"),
)
COLORS = (
    ("#eef1f2", "#4c5c63"),
    ("#dff3e9", "#155942"),
    ("#fff0c8", "#775312"),
    ("#ffe2dc", "#8b3027"),
)


class HealthPanel(QTableWidget):
    def __init__(self, status_type):
        super().__init__()
        self.status_type = status_type
        self.rows = {}
        self.setColumnCount(4)
        self.setHorizontalHeaderLabels(["Subsystem / signal", "Status", "Last observed", "Value"])
        self.observed = {}
        self.values = {}
        self.field_controls = False
        self.remote_trigger = False
        self.waiting_for = ""
        self.diag_received = 0
        self.setAccessibleName("Subsystem health table")
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(27)
        self.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        names = [
            ("Radio", "Ground station radio lease"),
            ("Jetson", "Field supervisor status"),
            ("Battery", "Reported percentage. At or below 20% is a display advisory."),
            ("E-stop", "Reported emergency-stop input"),
        ]
        names.extend((name, tip) for _, name, _, _, tip in PROBES)
        names.append(("Camera", "Fresh stamped camera calibration stream; image pixels are not inspected"))
        groups = {"Radio":"Radio", "Jetson":"Controller", "Battery":"Lynx", "E-stop":"Lynx",
                  "Mission FSM":"FSM", "Nav2":"Controller", "Odometry":"Lynx", "Spatial scan":"LiDAR",
                  "GNSS":"Lynx", "Perception":"Perception", "Command guard":"Controller",
                  "Cleaning":"Collector", "Camera":"Perception"}
        order = ("Radio", "Controller", "Lynx", "LiDAR", "Perception", "Collector", "FSM")
        names.sort(key=lambda row: (order.index(groups[row[0]]), row[0]))
        self.categories = {}
        for group, members in groupby(names, key=lambda row: groups[row[0]]):
            index = self.rowCount()
            self.insertRow(index)
            toggle = QToolButton()
            toggle.setText(group)
            toggle.setAutoRaise(True)
            toggle.setObjectName("subsystemGroup")
            toggle.setCheckable(True)
            toggle.setArrowType(Qt.ArrowType.RightArrow)
            toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            toggle.setAccessibleName(f"{group} subsystem signals")
            self.setCellWidget(index, 0, toggle)
            summary = QTableWidgetItem()
            self.setItem(index, 1, summary)
            children = []
            for name, tip in members:
                row = self.rowCount()
                self.insertRow(row)
                title = QTableWidgetItem("   " + name)
                title.setToolTip(tip)
                self.setItem(row, 0, title)
                status = QTableWidgetItem()
                status.setToolTip(tip)
                self.setItem(row, 1, status)
                self.setItem(row, 2, QTableWidgetItem("—"))
                self.setItem(row, 3, QTableWidgetItem("—"))
                self.rows[name] = status
                self.indicate(name, 0, "Unknown")
                children.append(status)
                self.setRowHidden(row, True)
            self.categories[group] = (toggle, summary, children)
            toggle.toggled.connect(lambda opened, key=group: self.expand_group(key, opened))

    def expand_group(self, group, opened):
        toggle, _, children = self.categories[group]
        toggle.setArrowType(Qt.ArrowType.DownArrow if opened else Qt.ArrowType.RightArrow)
        for cell in children:
            self.setRowHidden(cell.row(), not opened)

    def indicate(self, name, level, state):
        cell = self.rows[name]
        if cell.text() != state or cell.data(Qt.ItemDataRole.UserRole) != level:
            cell.setText(state)
            cell.setData(Qt.ItemDataRole.UserRole, level)
            bg, fg = COLORS[level]
            cell.setBackground(QColor(bg))
            cell.setForeground(QColor(fg))

    def reset_observations(self):
        self.observed.clear()
        self.values.clear()
        self.diag_received = 0
        self.field_controls = False
        self.remote_trigger = False
        self.waiting_for = ""

    def diagnostics(self, data, ns):
        if not isinstance(data, list) or len(data) not in (4, 5):
            return
        ages, speed, distance, trigger = data[:4]
        waiting = data[4] if len(data) == 5 else ""
        if not isinstance(waiting, str) or len(waiting) > 96:
            return
        names = ("Mission FSM", "Nav2", "Odometry", "Spatial scan", "GNSS", "Perception",
                 "Command guard", "Cleaning", "Camera", "Battery", "E-stop")
        if (not isinstance(ages, list) or len(ages) != len(names)
                or any(type(age) is not int or not -1 <= age <= 65535 for age in ages)
                or any(v is not None and (type(v) is not int or abs(v) > 100000) for v in (speed, distance))
                or type(trigger) is not int or not 0 <= trigger <= 3):
            return
        for name, age in zip(names, ages):
            if 0 <= age < 65535:
                self.observed[name] = ns / 1e9 - age
            else:
                self.observed.pop(name, None)
        self.diag_received = time.monotonic()
        self.remote_trigger = bool(trigger & 1)
        self.field_controls = bool(trigger & 2)
        self.waiting_for = waiting
        self.values["Odometry"] = f"{speed / 100:.2f} m/s" if speed is not None and ages[2] in range(4) else "—"
        self.values["Spatial scan"] = f"{distance / 100:.2f} m nearest" if distance is not None and ages[3] in range(4) else "—"

    def can_pause(self):
        return self.field_controls and time.monotonic() - self.diag_received < 8

    def can_trigger(self):
        return self.remote_trigger and time.monotonic() - self.diag_received < 8

    def summary(self):
        levels = {name: row.data(Qt.ItemDataRole.UserRole) for name, row in self.rows.items()}
        faults = sorted(name for name, level in levels.items() if level == 3)
        warnings = sorted(name for name, level in levels.items() if level == 2)
        if faults or warnings:
            return "Check " + ", ".join(faults + warnings)
        count = sum(level == 1 for level in levels.values())
        if count == len(levels):
            return "All systems reporting OK"
        return (
            f"Systems reporting OK · {count} of {len(levels)}"
            if count
            else "Awaiting system status"
        )

    def display(self, link, snap):
        level = {
            "Radio ready": 1,
            "Waiting for Jetson": 2,
            "Link lost": 3,
            "Port unavailable": 3,
            "Contract mismatch": 3,
        }.get(link, 0)
        self.indicate("Radio", level, "Ready" if level == 1 else link or "Disconnected")
        self.indicate("Jetson", 1 if snap else 0, "Reporting" if snap else "Unknown")
        battery = snap.battery_percent if snap else -1
        known = 0 <= battery <= 100
        missing_level = 3 if snap and snap.mission_adapter_online else 0
        self.indicate(
            "Battery",
            (2 if battery <= 20 else 1) if known else missing_level,
            ("Low" if battery <= 20 else "Reporting") if known else ("No data" if snap else "Unknown"),
        )
        stop = snap.estop if snap else -1
        self.indicate(
            "E-stop", {0: 1, 1: 3}.get(stop, missing_level), {0: "Released", 1: "Active"}.get(stop, "No data" if snap else "Unknown")
        )
        if snap:
            self.observed["Jetson"] = snap.stamp.sec + snap.stamp.nanosec / 1e9
        camera_time = self.observed.get("Camera")
        camera_ok = bool(snap and camera_time and time.time() - camera_time < 8)
        self.indicate("Camera", 1 if camera_ok else 2 if snap and camera_time else 0,
                      "Calibration stream" if camera_ok else "Stale" if snap and camera_time else "No report")
        self.values["Battery"] = f"{battery}%" if known else "—"
        self.values["E-stop"] = {0:"Released", 1:"Active"}.get(stop, "—")
        now = time.time()
        for name, cell in self.rows.items():
            observed = self.observed.get(name)
            value = self.values.get(name, "—") if snap else "—"
            if name in ("Spatial scan", "Odometry") and time.monotonic() - self.diag_received >= 8:
                value = "—"
            age = max(0, int(now - observed)) if observed is not None else None
            last_seen = ("—" if age is None else "Just now" if age == 0 else
                         "1 second ago" if age == 1 else f"{age} seconds ago")
            for col, text in ((2, last_seen), (3, value)):
                item = self.item(cell.row(), col)
                if item.text() != text:
                    item.setText(text)
                    item.setToolTip(text)
        for key, name, good, fault, _ in PROBES:
            state = (snap.health >> getattr(self.status_type, key)) & 3 if snap else 0
            warning = "Not ready" if key == "NAV2" else "Degraded"
            self.indicate(name, state, (("No report" if snap else "Unknown"), good, warning, fault)[state])
        for _, summary, children in self.categories.values():
            levels = [cell.data(Qt.ItemDataRole.UserRole) for cell in children]
            if 3 in levels:
                level, text = 3, "Fault"
            elif 2 in levels:
                level, text = 2, "Check signals"
            elif all(level == 1 for level in levels):
                level, text = 1, "Reporting"
            else:
                level, text = 0, f"{levels.count(1)} of {len(children)} reporting"
            if summary.text() != text:
                summary.setText(text)
                summary.setForeground(QColor(COLORS[level][1]))
