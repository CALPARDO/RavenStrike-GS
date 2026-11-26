"""Operator indications. All onboard states come from one latest-only status frame."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
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
        self.setColumnCount(2)
        self.setHorizontalHeaderLabels(["Subsystem", "Status"])
        self.setAccessibleName("Subsystem health table")
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(27)
        self.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        names = [
            ("Radio", "Ground station radio lease"),
            ("Jetson", "Field supervisor status"),
            ("Battery", "Reported percentage. At or below 20% is a display advisory."),
            ("E-stop", "Reported emergency-stop input"),
        ]
        names.extend((name, tip) for _, name, _, _, tip in PROBES)
        self.setRowCount(len(names))
        for index, (name, tip) in enumerate(names):
            title = QTableWidgetItem(name)
            status = QTableWidgetItem()
            title.setToolTip(tip)
            status.setToolTip(tip)
            self.setItem(index, 0, title)
            self.setItem(index, 1, status)
            self.rows[name] = status
            self.indicate(name, 0, "Unknown")

    def indicate(self, name, level, state):
        cell = self.rows[name]
        if cell.text() != state or cell.data(Qt.ItemDataRole.UserRole) != level:
            cell.setText(state)
            cell.setData(Qt.ItemDataRole.UserRole, level)
            bg, fg = COLORS[level]
            cell.setBackground(QColor(bg))
            cell.setForeground(QColor(fg))

    def summary(self):
        levels = {name: row.data(Qt.ItemDataRole.UserRole) for name, row in self.rows.items()}
        faults = [name for name, level in levels.items() if level == 3]
        warnings = [name for name, level in levels.items() if level == 2]
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
        for key, name, good, fault, _ in PROBES:
            state = (snap.health >> getattr(self.status_type, key)) & 3 if snap else 0
            warning = "Not ready" if key == "NAV2" else "Degraded"
            self.indicate(name, state, (("No report" if snap else "Unknown"), good, warning, fault)[state])
