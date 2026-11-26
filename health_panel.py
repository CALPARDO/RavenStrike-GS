"""Operator indications. All onboard states come from one latest-only status frame."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QLabel, QWidget

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


class HealthPanel(QWidget):
    def __init__(self, status_type):
        super().__init__()
        self.status_type = status_type
        self.rows = {}
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        names = [
            ("Radio", "Ground station radio lease"),
            ("Jetson", "Field supervisor status"),
            ("Battery", "Reported percentage. At or below 20% is a display advisory."),
            ("E-stop", "Reported emergency-stop input"),
        ]
        names.extend((name, tip) for _, name, _, _, tip in PROBES)
        for index, (name, tip) in enumerate(names):
            widget = QLabel()
            widget.setToolTip(tip)
            layout.addWidget(widget, index // 2, index % 2, Qt.AlignmentFlag.AlignLeft)
            self.rows[name] = widget
            self.indicate(name, 0, "Unknown")

    def indicate(self, name, level, state):
        widget = self.rows[name]
        text = f"{name} · {state}"
        if widget.text() != text or widget.property("level") != level:
            widget.setText(text)
            widget.setProperty("level", level)
            bg, fg = COLORS[level]
            widget.setStyleSheet(f"background:{bg};color:{fg};border-radius:4px;padding:3px 6px;")

    def summary(self):
        levels = {name: row.property("level") for name, row in self.rows.items()}
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
