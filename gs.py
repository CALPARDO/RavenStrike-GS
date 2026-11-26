"""Run the field ground station with python gs.py. Nothing transmits on startup."""

import faulthandler
import json
import math
import queue
import secrets
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from uuid import uuid4

# The frozen executable also hosts the map worker. It opens no window or radio.
if __name__ == "__main__" and sys.argv[1:2] == ["--fetch-map-tiles"]:
    from map_tiles import main as fetch_map_tiles

    sys.exit(fetch_map_tiles(sys.argv[2]))

from PySide6.QtCore import QSettings, QStandardPaths, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QImageReader,
    QPixmap,
    QTextBlockUserData,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from serial.tools import list_ports

from appearance import field_theme, icon
from commissioning import CommissioningDialog
from demo_replay import DemoReplay, SimulationView
from demo_site import SITE, SITE_ZOOM, demo_obstacles
from display import TargetNames, eastern
from fsm_panel import FsmPanel
from health_panel import PROBES, HealthPanel
from location import LocationTrack, telemetry_time_ok
from map_tracks import route_points
from radio import Radio
from runway_map import RunwayMap
from session_files import JournalReplay, export_evidence
from station import Journal, Station
from wire import CONTRACT, WIRE_MAP_VERSION, Codec


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


def coordinate_value(text, name):
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value):
        raise ValueError(f"Enter a finite number for {name}.")
    return value


def label(text, name=""):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setObjectName(name)
    return widget


class EvidencePreview(QLabel):
    def __init__(self):
        super().__init__("No image")
        self.original = QPixmap()
        self.setObjectName("evidence")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumWidth(80)
        self.setMinimumHeight(90)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)

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


class ActivityLog(QPlainTextEdit):
    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        entry = self.cursorForPosition(event.position().toPoint()).block().userData()
        if entry and event.button() == Qt.MouseButton.LeftButton:
            self.targetPicked.emit(entry.mission)

    targetPicked = Signal(str)


class CommandButton(QPushButton):
    """One action for the button, menu and keyboard shortcut."""

    def __init__(self, action):
        super().__init__(action.text())
        self.action = action
        self.clicked.connect(self.action.trigger)

    def setIcon(self, icon):
        self.action.setIcon(icon)
        super().setIcon(icon)

    def setEnabled(self, enabled):
        self.action.setEnabled(bool(enabled))
        super().setEnabled(bool(enabled))

    def setText(self, text):
        self.action.setText(text)
        super().setText(text)


SIM_ACTIVE = ("running", "pausing", "paused", "resuming", "stopping")


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        field_theme(QApplication.instance())
        self.build = build_id()
        self.codec = Codec()
        self.tests_dialog = None
        self.app_snapshot = None
        self.status_warning = ""
        self.app_stamp = self.app_received = self.pending_until = 0
        self.track = LocationTrack()
        self.radio = self.station = self.journal = None
        self.log_folder = self.last_rx = self.presentation = self.shown = None
        self.queue_rows = self.link_state = self.last_event = None
        self.leave_job = None
        self.health_key = None
        self.activity_entries = deque(maxlen=400)
        self.simulation = False
        self.setWindowTitle("RavenStrike | Mission control")
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
        switch = QWidget()
        switch.setObjectName("workspaceSwitch")
        controls = QHBoxLayout(switch)
        controls.setContentsMargins(3, 3, 3, 3)
        controls.setSpacing(2)
        group = QButtonGroup(self)
        self.field_button = QPushButton("Field")
        self.sim_button = QPushButton("Simulation")
        for button in (self.field_button, self.sim_button):
            button.setObjectName("workspaceButton")
            button.setCheckable(True)
            group.addButton(button)
            controls.addWidget(button)
        self.field_button.setChecked(True)
        self.field_button.clicked.connect(self.request_field)
        self.sim_button.clicked.connect(lambda: self.set_workspace(True))
        bar.addWidget(switch)
        bar.addStretch()
        self.identity = label("Field console", "headerText")
        bar.addWidget(self.identity)
        self.link = self.button(bar, "Disconnected", lambda: self.radio_dialog.show())
        self.link.setAccessibleName("Radio connection status and settings")
        self.link.setObjectName("link")
        self.link.setIcon(icon("radio"))
        self.link.setToolTip("Open radio connection settings")
        self.record_button = self.button(bar, "Record", self.toggle_record)
        self.stops = QToolButton()
        self.stops.setText("Stop mission")
        self.stops.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.stops.setIcon(icon("stop"))
        self.stops.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.stops.clicked.connect(self.command_action("Stop mission", self.stop_mission).trigger)
        self.stops.setToolTip("Stop autonomous navigation and cleaning in this session")
        self.stops.setObjectName("stopMenu")
        self.stops.setAccessibleName("Mission and simulation stop controls")
        self.stop_menu = QMenu(self.stops)
        self.stops.setMenu(self.stop_menu)
        bar.addWidget(self.stops)
        shell.addWidget(header)
        self.settings = QSettings("RavenStrike", "FieldConsole")
        self.draft = None
        self.radio_dialog = QDialog(self)
        self.radio_dialog.setWindowTitle("Radio connection")
        connection = QVBoxLayout(self.radio_dialog)
        connection.addWidget(label("Serial port", "field"))
        self.port = QComboBox()
        self.port.setEditable(True)
        self.port.setAccessibleName("Radio serial port")
        self.port.lineEdit().setPlaceholderText("Serial port")
        connection.addWidget(self.port)
        buttons = QHBoxLayout()
        connection.addLayout(buttons)
        self.refresh = self.button(buttons, "Refresh", self.ports)
        self.connect_button = self.button(buttons, "Connect", self.connect_radio)
        self.connect_button.setObjectName("primary")

        body = QWidget()
        body.setObjectName("workspace")
        shell.addWidget(body, 1)
        workspace = QVBoxLayout(body)
        workspace.setContentsMargins(12, 8, 12, 8)
        workspace.setSpacing(8)
        self.build_telemetry(workspace)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        workspace.addWidget(self.splitter, 1)
        self.tabs = QTabWidget()
        self.splitter.addWidget(self.tabs)
        self.map_page = QStackedWidget()
        self.field_map = QWidget()
        self.map_page.addWidget(self.field_map)
        self.tabs.addTab(self.map_page, icon("map"), "Mission map")
        self.map_panel(QVBoxLayout(self.field_map))
        inspector = QFrame()
        inspector.setObjectName("card")
        inspector.setMinimumWidth(310)
        self.sidebar = QStackedWidget()
        self.sidebar.addWidget(inspector)
        self.splitter.addWidget(self.sidebar)
        self.activity_panel(QVBoxLayout(inspector))
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([900, 350])

        footer = QFrame()
        footer.setObjectName("footer")
        foot = QHBoxLayout(footer)
        foot.setContentsMargins(16, 5, 16, 5)
        self.feedback = label("", "muted")
        self.feedback.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        foot.addWidget(self.feedback, 1)
        self.last_update = label("", "muted")
        foot.addWidget(self.last_update)
        self.clock = label("", "clock")
        self.clock.setToolTip("US Eastern time · adjusts for daylight saving")
        foot.addWidget(self.clock)
        self.copy_info = self.button(foot, "Copy diagnostics", self.copy_diagnostics)
        self.copy_info.setObjectName("mapTool")
        shell.addWidget(footer)
        self.copy_info.hide()  # Available under Help.
        self.scrim = QFrame(root)
        self.scrim.setObjectName("scrim")
        self.scrim.hide()
        self.review_page.finished.connect(lambda _: self.scrim.hide())
        self.menus()
        self.ports()
        saved = self.settings.value("splitter")
        if saved:
            self.splitter.restoreState(saved)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(100)
        self.tick()
        self.resize(1280, 735)

    def build_telemetry(self, workspace):
        self.telemetry_strip = QFrame()
        self.telemetry_strip.setObjectName("telemetry")
        workspace.addWidget(self.telemetry_strip)
        meters = QHBoxLayout(self.telemetry_strip)
        meters.setContentsMargins(12, 4, 10, 4)
        meters.setSpacing(8)
        self.instruments = []
        for name in ("Battery", "E-stop", "Heading"):
            if self.instruments:
                separator = QFrame()
                separator.setFrameShape(QFrame.Shape.VLine)
                separator.setObjectName("rule")
                meters.addWidget(separator)
            meters.addWidget(label(name, "field"))
            value = label("—", "meter")
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            value.setMinimumWidth(34)
            meters.addWidget(value)
            self.instruments.append(value)
        self.health_summary = label("", "muted")
        self.health_summary.setWordWrap(True)
        self.health_summary.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        meters.addWidget(self.health_summary, 1)
        self.health_action = QAction(icon("health"), "Subsystem health", self)
        self.health_action.setCheckable(True)
        self.health_details = QToolButton()
        self.health_details.setObjectName("healthToggle")
        self.health_details.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.health_details.setDefaultAction(self.health_action)
        meters.addWidget(self.health_details)

        drawer = QDialog(self)
        drawer.setWindowTitle("Subsystem health")
        drawer.resize(760, 470)
        details = QVBoxLayout(drawer)
        details.setContentsMargins(16, 14, 16, 12)
        details.setSpacing(10)
        details.addWidget(label("Subsystem health", "heading"))
        self.health_panel = HealthPanel(self.codec.types["FieldStatus"])
        details.addWidget(self.health_panel)
        self.health_action.toggled.connect(drawer.setVisible)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setAccessibleName("Close subsystem health")
        buttons.rejected.connect(drawer.reject)
        drawer.finished.connect(lambda _: self.health_action.setChecked(False))
        details.addWidget(buttons)

    def map_panel(self, column):
        pane = column
        bar = QHBoxLayout()
        pane.addLayout(bar)
        self.map_heading = label("Map", "heading")
        bar.addWidget(self.map_heading)
        bar.addStretch()
        self.runway = RunwayMap()
        bar.addWidget(self.runway.layer_switch())
        self.runway.pointPicked.connect(self.pick_location)
        self.runway.targetPicked.connect(self.select_target)
        self.locate = self.button(bar, "Locate UGV", self.locate_robot)
        self.fit_route = self.button(bar, "Fit route", self.fit_targets)
        self.locate.setIcon(icon("locate"))
        self.fit_route.setIcon(icon("fit"))
        for control in (self.locate, self.fit_route):
            control.setObjectName("mapTool")
        pane.addWidget(self.runway, 1)
        legend = QHBoxLayout()
        pane.addLayout(legend)
        for text, role in (("┅  Nav2 plan", "planKey"), ("━  GPS track", "trackKey")):
            legend.addWidget(label(text, role))
        self.obstacle_key = label("◆  Sim obstacle", "obstacleKey")
        legend.addWidget(self.obstacle_key)
        self.robot_caption = label("", "muted")
        self.robot_caption.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.robot_caption.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        legend.addWidget(self.robot_caption, 1)
        self.clear_track = self.button(legend, "Clear track", self.clear_gps)
        self.clear_track.setObjectName("mapTool")
        self.clear_track.setToolTip("Clear the displayed GPS trail. New fixes start a new trail.")

        self.draft_dialog = QDialog(self)
        self.draft_dialog.setWindowTitle("Add FOD location")
        pane = QVBoxLayout(self.draft_dialog)
        pane.addWidget(label("WGS84 target", "heading"))
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
            edit.textChanged.connect(self.invalidate_draft)
        row = QHBoxLayout()
        pane.addLayout(row)
        row.addStretch()
        self.preview = self.button(row, "Preview on map", self.map)
        self.submit = self.button(row, "Send FOD", self.add)
        self.submit.setObjectName("primary")

    def mission_panel(self, column):
        self.status = label("No active session", "muted")
        self.status.setWordWrap(True)
        column.addWidget(self.status)
        self.phase_readout = label("", "phase")
        column.addWidget(self.phase_readout)
        self.active_target = label("", "muted")
        self.active_target.setWordWrap(True)
        column.addWidget(self.active_target)
        row = QHBoxLayout()
        column.addLayout(row)
        self.start = self.button(row, "Start session", self.start_session)
        self.start.setObjectName("primary")
        self.add_target = self.button(row, "Add FOD…", self.open_draft)
        self.add_target.setObjectName("targetAction")
        self.add_target.setIcon(icon("target"))
        self.scan = self.button(
            row,
            "Scanning complete",
            lambda: self.confirm("scan_complete", "Finish scanning and drain the queue?"),
        )
        self.halt = self.command_action(
            "Halt field mission in place",
            lambda: self.confirm("cancel", "Halt the field mission here and retain unfinished targets?"),
        )
        self.abort = self.command_action(
            "Abort field mission and return to base",
            lambda: self.confirm("abort", "Request abort and return to base?"),
        )
        self.abort.setIcon(icon("return"))
        self.field_pause = self.button(column, "Pause mission", self.toggle_field_pause)
        self.scan_state = label("Scanning closed", "closed")
        row.addWidget(self.scan_state)

    @staticmethod
    def target_table(name):
        table = QTableWidget(0, 2)
        table.setAccessibleName(name)
        table.setHorizontalHeaderLabels(["FOD ID", "UGV status"])
        table.verticalHeader().hide()
        table.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setShowGrid(False)
        table.setFrameShape(QFrame.Shape.NoFrame)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setDefaultSectionSize(30)
        table.setMinimumHeight(65)
        return table

    def activity_panel(self, column):
        pane = column
        pane.setContentsMargins(14, 12, 14, 12)
        self.inspector = label("Field mission", "heading")
        pane.addWidget(self.inspector)
        self.mission_panel(pane)
        self.queue_caption = label("Target register", "section")
        pane.addWidget(self.queue_caption)
        self.tasks = self.target_table("Mission targets")
        self.tasks.itemActivated.connect(self.locate_row)
        pane.addWidget(self.tasks, 1)
        self.empty_targets = label("No targets in this session", "muted")
        self.empty_targets.setWordWrap(True)
        self.empty_targets.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        pane.addWidget(self.empty_targets, 1)
        self.selection_info = label("", "muted")
        self.selection_info.setWordWrap(True)
        pane.addWidget(self.selection_info)
        self.timeline = QPlainTextEdit()
        self.timeline.setObjectName("timeline")
        self.timeline.setReadOnly(True)
        self.timeline.setAccessibleName("Selected FOD timeline")
        self.timeline.setMinimumHeight(40)
        self.timeline.setMaximumHeight(115)
        self.timeline.setPlaceholderText("Select a FOD to see its timeline.")
        pane.addWidget(self.timeline)
        self.tasks.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tasks.currentItemChanged.connect(self.update_timeline)
        self.open_review = self.button(pane, "Open operator review", self.show_review)
        self.open_review.setObjectName("reviewAction")
        self.review_page = QDialog(self)
        self.review_page.setWindowTitle("Operator review")
        self.review_page.setWindowModality(Qt.WindowModality.WindowModal)
        review = QVBoxLayout(self.review_page)
        review.setContentsMargins(28, 24, 28, 20)
        review.setSpacing(14)
        self.review_heading = label("Operator review", "reviewHeading")
        review.addWidget(self.review_heading)
        self.review_text = label("", "reviewSubtitle")
        self.review_text.setWordWrap(True)
        review.addWidget(self.review_text)
        images = QHBoxLayout()
        review.addLayout(images, 1)
        self.pictures = []
        self.picture_titles = []
        for name in ("Before", "Review"):
            tile = QVBoxLayout()
            images.addLayout(tile, 1)
            caption = label(name, "field")
            tile.addWidget(caption)
            self.picture_titles.append(caption)
            picture = EvidencePreview()
            tile.addWidget(picture, 1)
            self.pictures.append(picture)
        self.cycle_summary = label(
            "The UGV completed its cleaning cycle. Continuing leaves removal unverified.",
            "reviewNotice",
        )
        self.cycle_summary.setWordWrap(True)
        review.insertWidget(2, self.cycle_summary)
        self.inspect = self.button(review, "View original images", self.inspect_images)
        self.inspect.setObjectName("mapTool")
        review.setAlignment(self.inspect, Qt.AlignmentFlag.AlignLeft)
        actions = QHBoxLayout()
        review.addLayout(actions)
        self.later = self.button(actions, "Review later", self.review_page.reject)
        self.later.setObjectName("mapTool")
        actions.addStretch()
        self.retry = self.button(actions, "Clean again", lambda: self.decide("retry"))
        self.approve = self.button(actions, "Continue", lambda: self.decide("approve"))
        self.approve.setObjectName("primary")
        for button in self.review_page.findChildren(QPushButton):
            button.setAutoDefault(False)
        self.fsm_panel = FsmPanel(self.codec.types["FieldStatus"])
        self.tabs.addTab(self.fsm_panel, icon("flow"), "State machine")
        activity = self.activity = QWidget()
        self.tabs.addTab(activity, icon("log"), "Activity")
        pane = QVBoxLayout(activity)
        bar = QHBoxLayout()
        pane.addLayout(bar)
        bar.addWidget(label("Event log", "heading"))
        bar.addStretch()
        self.source_filters = {}
        for source in ("GS", "UGV"):
            toggle = QCheckBox(source)
            toggle.setChecked(True)
            toggle.toggled.connect(self.filter_activity)
            self.source_filters[source] = toggle
            bar.addWidget(toggle)
        self.open_logs = self.button(bar, "Open folder", self.open_log_folder)
        self.log = ActivityLog()
        self.log.setReadOnly(True)
        self.log.targetPicked.connect(self.select_target)
        self.log.setMaximumBlockCount(400)
        self.log.setPlaceholderText("Radio and operator events appear here.")
        pane.addWidget(self.log)

        self.stop_menu.addAction(self.abort)
        self.stop_menu.addAction(self.scan.action)
        self.stop_menu.addSeparator()
        self.trigger_estop = self.command_action("Trigger Lynx base E-stop", self.platform_estop)
        self.stop_menu.addAction(self.trigger_estop)
        self.simulation_panel()

    def simulation_panel(self):
        self.sim_view = SimulationView()
        self.map_page.addWidget(self.sim_view)
        panel = QFrame()
        panel.setObjectName("card")
        self.sidebar.addWidget(panel)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        layout.addWidget(label("Simulation control", "heading"))
        layout.addWidget(label("Seed", "field"))
        self.seed = QLineEdit()
        self.seed.setAccessibleName("Mission seed")
        self.seed.setPlaceholderText("Random each run")
        layout.addWidget(self.seed)
        self.catalog = json.loads((CONTRACT / "apps.json").read_text())
        self.launch_app = self.button(layout, "Run mission", lambda: self.app_command("start"))
        self.launch_app.setObjectName("primary")
        self.pause_app = self.button(layout, "Pause", self.toggle_sim_pause)
        self.stop_app = self.button(layout, "Stop simulation", lambda: self.app_command("stop"))
        self.stop_app.setObjectName("stopSimulation")
        self.stop_app.setIcon(icon("stop"))
        layout.addWidget(label("Job status", "section"))
        self.job_label = label("", "muted")
        self.job_label.setWordWrap(True)
        layout.addWidget(self.job_label)
        stats = QFormLayout()
        stats.setVerticalSpacing(8)
        stats.setHorizontalSpacing(18)
        self.sim_values = []
        for caption in ("Seed used", "Targets queued", "Heading"):
            value = label("—", "meter")
            if caption == "Seed used":
                seed_row = QHBoxLayout()
                seed_row.addWidget(value)
                self.copy_seed = self.button(seed_row, "Copy", lambda: QApplication.clipboard().setText(str(self.sim_view.seed)))
                self.copy_seed.setObjectName("mapTool")
                self.copy_seed.setAccessibleName("Copy seed used")
                seed_row.addStretch()
                stats.addRow(label(caption, "field"), seed_row)
            else:
                stats.addRow(label(caption, "field"), value)
            self.sim_values.append(value)
        layout.addLayout(stats)
        layout.addWidget(label("FOD queue", "section"))
        self.sim_tasks = self.target_table("Simulated mission targets")
        layout.addWidget(self.sim_tasks)
        self.sim_queue_info = label("Awaiting a queue report", "muted")
        layout.addWidget(self.sim_queue_info)
        self.sim_queue_key = None
        self.sim_names = TargetNames()
        layout.addStretch()

    def menus(self):
        def action(menu, title, callback, shortcut=None):
            item = menu.addAction(title)
            item.triggered.connect(callback)
            if shortcut:
                item.setShortcut(shortcut)
            return item

        files = self.menuBar().addMenu("&File")
        self.open_recording_action = action(files, "Open recording…", self.open_recording, "Ctrl+O")
        self.demo_button = QAction("Open example recording…", self)
        self.demo_button.triggered.connect(self.show_demo)
        files.addAction(self.demo_button)
        files.addAction(self.open_logs.action)
        self.export_action = action(files, "Export session evidence…", self.export_session)
        files.addSeparator()
        action(files, "Quit", self.close, "Ctrl+Q").setMenuRole(QAction.MenuRole.QuitRole)

        view = self.menuBar().addMenu("&View")
        for i in range(self.tabs.count()):
            action(
                view,
                self.tabs.tabText(i),
                lambda checked=False, i=i: self.tabs.setCurrentIndex(i),
                f"Ctrl+{i + 1}",
            )
        view.addSeparator()
        self.follow = view.addAction("Follow vehicle")
        self.follow.setCheckable(True)
        self.follow.setChecked(self.settings.value("follow", False, type=bool))
        self.follow.toggled.connect(lambda checked: self.locate_robot() if checked else None)
        self.runway.panned.connect(lambda: self.follow.setChecked(False))
        self.fit_route.action.setText("Fit route and targets")
        view.addAction(self.fit_route.action)
        self.site_action = action(view, "Center on demo site", self.locate_site)
        action(view, "Restore default layout", self.restore_layout)

        mission = self.mission_menu = self.menuBar().addMenu("&Mission")
        for item in (self.start.action, self.add_target.action, self.scan.action, self.halt, self.abort):
            mission.addAction(item)
        self.add_target.action.setShortcut("Ctrl+N")

        tools = self.menuBar().addMenu("&Tools")
        action(tools, "Radio connection…", self.radio_dialog.show)
        tools.addAction(self.health_action)
        action(tools, "Run Tests…", self.open_tests)
        action(tools, "Preferences…", self.preferences).setMenuRole(
            QAction.MenuRole.PreferencesRole
        )
        help_menu = self.menuBar().addMenu("&Help")
        action(help_menu, "Operator guide", self.show_guide)
        help_menu.addAction(self.copy_info.action)
        action(
            help_menu,
            "About RavenStrike",
            lambda: QMessageBox.about(
                self,
                "RavenStrike GS",
                f"Field ground station\nBuild {self.build} · Wire v{WIRE_MAP_VERSION}",
            ),
        ).setMenuRole(QAction.MenuRole.AboutRole)

    def open_tests(self):
        if self.tests_dialog is None:
            self.tests_dialog = CommissioningDialog(self)
        self.tests_dialog.show()
        self.tests_dialog.raise_()
        self.tests_dialog.activateWindow()

    def show_guide(self):
        QMessageBox.information(
            self,
            "Operator guide",
            "Connect the USB radio from the header. Wait for Radio ready, then start a supervised session.\n\n"
            "Use Add FOD or click the map. Enter ellipsoid altitude, preview the location and send. Select a marker, target row or target event to inspect its history.\n\n"
            "Finish scanning closes submissions. Review opens automatically. Use Open operator review in the inspector to return to a deferred decision. Abort to base requests a return.\n\n"
            "Open recordings and export evidence from File. Select Simulation in the header to run a seeded Nav2 mission.",
        )

    def open_recording(self):
        if self.radio:
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open session recording",
            str(self.log_folder or ""),
            "GS recordings (*.jsonl *.jsonl.gz)",
        )
        if path:
            try:
                dialog = JournalReplay(Path(path), self)
            except (ValueError, OSError, EOFError) as err:
                self.event_line(f"Cannot open recording · {err}")
                return
            dialog.exec()
            dialog.deleteLater()

    def export_session(self):
        if self.log_folder:
            dest, _ = QFileDialog.getSaveFileName(
                self,
                "Export session evidence",
                self.log_folder.name + ".zip",
                "ZIP archive (*.zip)",
            )
            if dest:
                try:
                    export_evidence(self.log_folder, Path(dest))
                    self.event_line("Evidence exported to " + dest)
                except (ValueError, OSError) as err:
                    self.event_line(f"Export failed · {err}")

    def preferences(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Preferences")
        layout = QVBoxLayout(dialog)
        follow = QCheckBox("Follow the vehicle when opening the console")
        follow.setChecked(self.settings.value("follow", False, type=bool))
        layout.addWidget(follow)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        layout.addWidget(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec():
            self.settings.setValue("follow", follow.isChecked())
            self.follow.setChecked(follow.isChecked())

    def field_busy(self):
        return bool(self.station and self.station.session and self.station.state not in
                    ("completed", "canceled", "halted", "rejected"))

    def request_field(self):
        snap = self.app_snapshot
        if self.simulation and (self.pending_until or (snap and snap.state in SIM_ACTIVE)):
            self.sim_button.setChecked(True)
            if self.pending_until or not self.app_fresh():
                self.event_line("Wait for current Jetson job status before switching to Field.")
                return
            job_id = snap.job_id
            if snap.state == "stopping":
                self.leave_job = job_id
                return
            answer = QMessageBox.question(self, "Switch to Field", "End the running simulation and switch to Field?",
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                          QMessageBox.StandardButton.No)
            if (answer == QMessageBox.StandardButton.Yes and self.app_fresh()
                    and self.app_snapshot.job_id == job_id):
                self.leave_job = job_id
                self.stop_app.action.trigger()
            return
        self.set_workspace(False)

    def toggle_record(self):
        if not self.journal:
            return
        self.drain()
        if self.journal.recording:
            self.journal.serial_data(*self.radio.transport.take_capture(False))
            self.journal.stop_recording()
            self.event_line(f"Recording saved · {self.journal.record_path}")
        elif not self.simulation and self.app_fresh() and self.app_snapshot.fsm_state == self.app_snapshot.FSM_BRING_UP_SYSTEM:
            self.journal.start_recording()
            self.radio.transport.take_capture(True)
            self.journal.log("record_context", "Field · Bring up system", build=self.build,
                             wire_version=WIRE_MAP_VERSION, source_ns=self.app_stamp)
            self.event_line(f"Recording · {self.journal.record_path.name}")
        self.tick()

    def platform_estop(self):
        if (not self.simulation and self.radio and self.radio.ready and self.app_fresh()
                and self.app_snapshot.state not in SIM_ACTIVE and self.health_panel.can_trigger()):
            msg = self.codec.types["AppCommand"](self.codec.stamp(), uuid4().hex, "trigger", "lynx-estop", "", False)
            self.station.send(21, msg, "Trigger Lynx base E-stop")
            self.event_line("Lynx E-stop requested · awaiting hardware input")

    def stop_mission(self):
        if not self.simulation and self.station and self.radio.ready and self.station.can_halt:
            if self.health_panel.can_pause():
                self.station.command("stop")
                self.event_line("Stop requested · awaiting measured stop")
            else:
                self.station.command("cancel")
                self.event_line("Controller capabilities unavailable · requested legacy halt in place")
            self.tick()
        else:
            self.event_line("No active mission stop available. Check the Lynx base E-stop in the menu.")

    def toggle_field_pause(self):
        if not self.simulation and self.station and self.radio.ready and self.health_panel.can_pause():
            command = "continue" if self.station.state == "paused" else "pause"
            self.station.command(command)
            self.event_line(f"Mission {command} requested · awaiting controller status")

    def toggle_sim_pause(self):
        if self.app_fresh():
            self.app_command("continue" if self.app_snapshot.state == "paused" else "pause")

    def set_workspace(self, simulation):
        previous = self.simulation
        self.simulation = bool(simulation and not self.field_busy())
        if self.journal and previous != self.simulation:
            self.journal.log("workspace", "Simulation" if self.simulation else "Field")
        self.field_button.setChecked(not self.simulation)
        self.sim_button.setChecked(self.simulation)
        self.map_page.setCurrentWidget(self.sim_view if self.simulation else self.field_map)
        self.sidebar.setCurrentIndex(int(self.simulation))
        self.mission_menu.menuAction().setVisible(not self.simulation)
        for item in (self.site_action, self.follow, self.fit_route.action, self.health_action):
            item.setVisible(not self.simulation)
        if self.simulation:
            self.draft_dialog.hide()
            self.invalidate_draft()
            self.review_page.reject()
            self.health_action.setChecked(False)
        self.tick()

    def restore_layout(self):
        self.tabs.setCurrentWidget(self.map_page)
        self.splitter.setSizes([900, 350])
        self.follow.setChecked(self.settings.value("follow", False, type=bool))
        self.health_action.setChecked(False)

    def clear_gps(self):
        if self.journal:
            self.journal.log("track_clear", "Operator cleared displayed GPS track")
        self.runway.tracks.fixes.clear()
        self.runway.update()
        self.clear_track.setEnabled(False)

    def fit_targets(self):
        self.tabs.setCurrentWidget(self.map_page)
        self.follow.setChecked(False)
        self.runway.fit_targets()

    def open_draft(self):
        self.tabs.setCurrentWidget(self.map_page)
        self.draft_dialog.show()
        self.draft_dialog.raise_()

    def pick_location(self, lat, lon):
        self.follow.setChecked(False)
        for edit, value in zip(self.coords[:2], (lat, lon)):
            edit.setText(f"{value:.7f}")
        self.open_draft()
        self.map()

    def invalidate_draft(self):
        self.draft = None
        self.runway.preview_location = None
        self.runway.update()
        if hasattr(self, "submit"):
            self.submit.setEnabled(False)

    def app_command(self, operation):
        snap = self.app_snapshot
        if not self.radio or not self.radio.ready or not self.app_fresh():
            raise ValueError("Wait for Jetson status.")
        if not self.simulation or self.field_busy() or self.pending_until:
            raise ValueError("Simulation control is unavailable.")
        if operation in ("stop", "pause", "continue"):
            allowed = {"stop": SIM_ACTIVE[:-1], "pause": ("running",), "continue": ("paused",)}
            if snap.state not in allowed[operation] or not snap.job_id:
                raise ValueError("No running simulation.")
            request, key, scenario = snap.job_id, snap.app, ""
        elif operation == "start":
            if snap.state not in ("idle", "finished", "failed", "stopped"):
                raise ValueError("Wait for the current job to finish.")
            key = "seeded-mission"
            scenario = self.seed.text().strip() or str(secrets.randbits(32))
            if len(scenario) > 10 or not scenario.isascii() or not scenario.isdecimal() or int(scenario) > self.catalog[key]["seed_max"]:
                raise ValueError("Seed must be between 0 and 4294967295.")
            scenario = str(int(scenario))
            request = uuid4().hex
        else:
            raise ValueError("Unknown simulation command.")
        msg = self.codec.types["AppCommand"](
            self.codec.stamp(), request, operation, key, scenario, False
        )
        self.station.send(21, msg, f"{operation.capitalize()} {key} {scenario}".strip())
        self.pending_until = time.monotonic() + 5
        self.pending_job = request
        self.pending_op = operation
        self.tick()

    def app_fresh(self):
        return (
            self.app_snapshot is not None
            and time.monotonic() - self.app_received < 3
            and telemetry_time_ok(self.app_stamp, time.time_ns())
        )

    def update_cockpit(self, ready):
        snap = self.app_snapshot if ready and self.app_fresh() else None
        idle = not (self.station and self.station.session) and (
            self.app_snapshot is None
            or (snap is not None and snap.state == "idle" and not snap.mission_adapter_online)
        )
        view = self.sim_view
        running_sim = bool(snap and snap.job_id == view.job and snap.state in SIM_ACTIVE[:-1])
        now_ms = time.time_ns() // 1_000_000
        fresh_sim = running_sim and telemetry_time_ok(view.stamp * 1_000_000, now_ms * 1_000_000)
        phase, state_name = self.fsm_panel.states.get(view.code, ("Unknown phase", "Unknown state"))
        if view.stamp:
            phase_label = state_name.replace("_", " ").capitalize()
            if fresh_sim:
                tag = {"paused": "Paused", "pausing": "Pausing", "resuming": "Resuming"}.get(snap.state, "Live")
                view.phase.setText(f"{tag} · {phase_label}")
                view.detail.setText("Live track · local metres")
            else:
                view.phase.setText(f"Last known · {phase_label}")
                if not snap:
                    reason = "Telemetry unavailable"
                elif running_sim:
                    reason = "Telemetry stale"
                else:
                    reason = f"Simulation {snap.state}"
                view.detail.setText(f"{reason} · last sample {eastern(view.stamp / 1000)} ET")
        else:
            if running_sim:
                view.phase.setText("Waiting for first FSM sample")
                view.detail.setText("The Jetson is starting the mission.")
            elif snap and snap.state == "idle":
                view.phase.setText("No simulation running")
                view.detail.setText("Select Run mission to start.")
            else:
                view.phase.setText(f"Simulation {snap.state}" if snap else "Waiting for Jetson status")
                view.detail.setText(self.status_warning or ("Connect the radio." if not self.radio else ""))
        historical = not fresh_sim
        if view.plot.historical != historical or view.plot.show_robot != bool(view.stamp):
            view.plot.historical = historical
            view.plot.show_robot = bool(view.stamp)
            view.plot.update()
        for widget, value in zip(self.sim_values, (
            str(view.seed) if view.seed is not None else "—",
            str(view.queued) if view.stamp else "—",
            f"{view.plot.trace['fixes'][-1][3]}°" if view.stamp else "—",
        )):
            widget.setText(value)
        queue_key = (view.job, view.queue)
        if self.sim_queue_key != queue_key:
            if self.sim_queue_key is None or self.sim_queue_key[0] != view.job:
                self.sim_names = TargetNames()
            self.sim_queue_key = queue_key
            self.sim_tasks.setRowCount(len(view.queue or ()))
            for i, (name, selected) in enumerate(view.queue or ()):
                item = QTableWidgetItem(self.sim_names(name))
                item.setToolTip(name)
                self.sim_tasks.setItem(i, 0, item)
                self.sim_tasks.setItem(i, 1, QTableWidgetItem("Active" if selected else "Queued"))
            self.sim_tasks.setVisible(bool(view.queue))
        queue_current = fresh_sim and now_ms - view.queue_stamp < 9000
        queue_text = ("Awaiting a queue report" if view.queue is None else
                      "No targets queued" if not view.queue else "Controller queue")
        if view.queue is not None and not queue_current:
            queue_text += " · last known"
        self.sim_queue_info.setText(queue_text)
        if self.simulation:
            self.fsm_panel.display_simulation(view.code, view.stamp, fresh_sim)
        else:
            self.fsm_panel.display(snap, ready, idle)
        pose = self.track.current() if ready else None
        values = ["—", "—", "—"]
        if snap:
            if 0 <= snap.battery_percent <= 100:
                values[0] = f"{snap.battery_percent}%"
            values[1] = {0: "Released", 1: "Active"}.get(snap.estop, "—")
        if pose and pose[2] is not None:
            values[2] = f"{pose[2]:.0f}° T"
        for widget, value in zip(self.instruments, values):
            widget.setText(value)
        if self.pending_until and time.monotonic() >= self.pending_until:
            self.pending_until = 0
            self.event_line("Jetson did not confirm the request. Check the reported job state.")
        pending = bool(self.pending_until)
        available = bool(self.simulation and not self.field_busy() and snap and snap.state in
                         ("idle", "stopped", "finished", "failed") and not pending)
        self.seed.setEnabled(not pending and not (snap and snap.state in SIM_ACTIVE))
        self.launch_app.setEnabled(available)
        active_job = bool(snap and snap.state in SIM_ACTIVE)
        self.stop_app.setVisible(active_job)
        self.launch_app.setVisible(not active_job)
        self.stop_app.setEnabled(bool(self.simulation and snap and snap.state in SIM_ACTIVE[:-1] and not pending))
        self.copy_seed.setEnabled(view.seed is not None)
        self.pause_app.setVisible(active_job)
        self.pause_app.setEnabled(bool(self.simulation and snap and snap.state in ("running", "paused") and not pending))
        self.pause_app.setText("Continue" if snap and snap.state == "paused" else "Pause")
        self.job_label.setToolTip(snap.detail if snap else "")
        if pending:
            status = "Waiting for Jetson confirmation"
        elif snap is None:
            status = (self.status_warning or "Waiting for current Jetson status") if self.radio else "Connect the radio to run a mission."
        elif snap.state == "running":
            status = phase if fresh_sim else "Waiting for simulation telemetry"
        elif snap.state == "failed" and snap.detail == "Unknown app":
            status = "Update the Jetson supervisor to run seeded missions."
        elif snap.state in ("failed", "stopped", "stopping"):
            status = f"{snap.state.capitalize()} · {snap.detail}"
        else:
            status = "Jetson ready" if snap.state == "idle" else snap.state.capitalize()
        self.job_label.setText(status)


    def show_review(self):
        if self.simulation or self.open_review.isHidden():
            return
        root = self.centralWidget()
        self.scrim.setGeometry(root.rect())
        self.scrim.show()
        self.scrim.raise_()
        dialog = self.review_page
        dialog.resize(max(300, int(self.width() * 0.82)), max(250, int(self.height() * 0.79)))
        dialog.move(self.mapToGlobal(self.rect().center()) - dialog.rect().center())
        dialog.show()
        dialog.raise_()
        self.later.setFocus()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "scrim"):
            self.scrim.setGeometry(self.centralWidget().rect())

    def inspect_images(self):
        st = self.station
        if not st or not any(not p.original.isNull() for p in self.pictures):
            return
        dialog = QDialog(self.review_page)
        dialog.setWindowTitle("Original images · " + self.runway.target_name(self.presentation[-1]))
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
            f"Link: {self.link_state}",
            f"Log folder: {self.log_folder or 'no connection yet'}",
        ]
        if self.radio:
            count = self.radio.engine.topic_messages_rx.get(24, 0)
            lines.append(f"Jetson status frames received: {count}")
            lines.append("Jetson status: " + (self.status_warning or
                         ("current" if self.app_fresh() else "waiting for fresh telemetry")))
        QApplication.clipboard().setText("\n".join(lines))
        self.feedback.setText("Diagnostics copied.")

    def update_timeline(self):
        item = self.tasks.item(self.tasks.currentRow(), 0)
        name = item.data(Qt.ItemDataRole.UserRole) if item else ""
        location = self.runway.targets.get(name)
        coords = (
            f"{location[0]:.6f}°, {location[1]:.6f}°" if location else "Coordinates unavailable"
        )
        detail = self.station.targets.get(name, "") if self.station else ""
        self.selection_info.setText(coords if name else "")
        self.selection_info.setVisible(bool(name))
        self.timeline.setVisible(bool(name))
        self.selection_info.setToolTip(f"{name}   {detail}")
        if self.runway.selected != name:
            self.runway.selected = name
            self.runway.update()
        entries = self.station.timeline.get(name, []) if self.station and item else []
        text = self.runway.target_name.in_text("\n".join(entries))
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
        name = self.tasks.item(item.row(), 0).data(Qt.ItemDataRole.UserRole)
        location = self.runway.targets.get(name)
        if location and abs(location[0]) <= 85.0511:
            self.follow.setChecked(False)
            self.tabs.setCurrentWidget(self.map_page)
            self.runway.initial_view = False
            self.runway.center = location
            self.runway.update()
        else:
            self.event_line(f"No mappable coordinates available for {name}.")

    def select_target(self, name):
        for row in range(self.tasks.rowCount()):
            if self.tasks.item(row, 0).data(Qt.ItemDataRole.UserRole) == name:
                self.tasks.selectRow(row)
                break

    def append_activity(self, entry):
        source, text, mission = entry
        if not self.source_filters[source].isChecked():
            return
        cursor = self.log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not self.log.document().isEmpty():
            cursor.insertBlock()
        style = QTextCharFormat()
        style.setForeground(QColor("#165887" if source == "GS" else "#146956"))
        cursor.insertText(text, style)
        if mission:
            data = QTextBlockUserData()
            data.mission = mission
            cursor.block().setUserData(data)
        self.log.setTextCursor(cursor)

    def filter_activity(self):
        self.log.clear()
        for entry in self.activity_entries:
            self.append_activity(entry)

    def event_line(self, text, mission="", source="GS"):
        if (source, text) != self.last_event:
            entry = (source, f"{eastern()}   {source}   {self.runway.target_name.in_text(text)}", mission)
            self.activity_entries.append(entry)
            self.append_activity(entry)
        self.last_event = (source, text)
        self.feedback.setText(self.runway.target_name.in_text(text))
        self.feedback.setToolTip(text)

    def command_action(self, name, fn):
        action = QAction(name, self)

        def invoke():
            try:
                fn()
            except OSError as err:
                self.log_failure(err)
            except (ValueError, RuntimeError) as err:
                self.event_line(str(err))
                if self.journal:
                    self.journal.log("operator_error", str(err))

        action.triggered.connect(invoke)
        return action

    def button(self, row, name, fn):
        button = CommandButton(self.command_action(name, fn))
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
        self.app_snapshot = None
        self.status_warning = ""
        self.app_stamp = self.app_received = self.pending_until = 0
        self.leave_job = None
        self.health_panel.reset_observations()
        self.sim_view.reset()
        self.track = LocationTrack()
        self.runway.last_known = None
        self.invalidate_draft()
        self.radio.start()
        self.radio_dialog.accept()
        self.last_rx = None
        self.connect_button.setText("Disconnect")
        self.tick()

    def disconnect(self):
        if self.tests_dialog and self.tests_dialog.timer.isActive():
            self.tests_dialog.cancel()
        if self.radio:
            self.radio.close()
            self.drain()
        if self.journal:
            self.journal.log("disconnect", "Operator closed connection")
            self.journal.close()
        self.radio = self.station = self.journal = None
        self.track = LocationTrack()
        self.app_snapshot = None
        self.pending_until = 0
        self.runway.reset_view()
        self.connect_button.setText("Connect")
        self.last_rx = None
        self.invalidate_draft()
        self.tick()

    def start_session(self):
        st = self.station
        if not self.start.isEnabled() or not st or not self.radio.ready:
            return
        if st.state == "awaiting acceptance":
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
            if self.station is not st or not self.radio.ready or not self.start.isEnabled():
                raise ValueError("Connection changed. Start canceled.")
            callsign, area, x, y, yaw = [e.text().strip() for e in edits]
            home = (coordinate_value(x, "home map X"), coordinate_value(y, "home map Y"),
                    math.radians(coordinate_value(yaw, "home heading")))
            self.station.start(callsign, area, home)
            self.invalidate_draft()
            self.runway.reset_view()
            self.event_line("Start requested · Awaiting UGV acceptance")

    def map(self):
        lat, lon = (coordinate_value(e.text(), name) for e, name in zip(self.coords, ("latitude", "longitude")))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("Enter valid latitude and longitude first.")
        self.follow.setChecked(False)
        self.runway.preview(lat, lon)
        self.draft = tuple(e.text() for e in self.coords)
        self.tick()

    def add(self):
        if (
            self.simulation or not self.station
            or not self.radio.ready
            or self.draft != tuple(e.text() for e in self.coords)
        ):
            raise ValueError("Preview the current coordinates before sending.")
        coords = tuple(coordinate_value(e.text(), name) for e, name in zip(self.coords, ("latitude", "longitude", "altitude")))
        mission = self.station.add(*coords)
        self.runway.targets[mission] = coords[:2]
        if abs(coords[0]) <= 85.0511:
            self.runway.preview(*coords[:2])
        self.event_line("Sent " + mission + "; awaiting UGV report")
        self.draft_dialog.accept()
        for e in self.coords:
            e.clear()
        self.tick()
        self.select_target(mission)

    def confirm(self, command, prompt):
        st = self.station
        if self.simulation or not st:
            return
        session = st.session
        if QMessageBox.question(self, "Mission control", prompt) == QMessageBox.StandardButton.Yes:
            if self.simulation or self.station is not st or st.session != session or not self.radio.ready:
                raise ValueError("Connection or session changed. Request canceled.")
            st.command(command)
            self.event_line(
                "Scanning complete requested"
                if command == "scan_complete"
                else "Halt requested · awaiting robot status" if command == "cancel"
                else "Return to base requested · awaiting robot status"
            )
            self.tick()

    def decide(self, decision):
        if self.simulation or not self.station:
            return
        self.station.decide(self.shown, decision)
        self.event_line("Sent " + decision + "; awaiting UGV report")
        self.tick()

    def show_demo(self):
        if self.radio is None:
            dialog = DemoReplay(self)
            dialog.exec()
            dialog.deleteLater()

    def locate_site(self):
        self.follow.setChecked(False)
        self.runway.initial_view = False
        self.runway.center = SITE
        self.runway.zoom = SITE_ZOOM
        self.runway.setCursor(Qt.CursorShape.OpenHandCursor)
        self.runway.failed.clear()
        self.runway.update()

    def locate_robot(self):
        if self.runway.robot:
            self.runway.initial_view = False
            self.runway.center = self.runway.robot[:2]
            self.runway.failed.clear()
            self.runway.update()

    def update_location(self, ready):
        pose = self.track.current() if ready else None
        mappable = pose if pose and abs(pose[0]) <= 85.0511 else None
        if self.track.position and abs(self.track.position[0]) <= 85.0511:
            self.runway.last_known = self.track.position[:2]
        if self.runway.robot != mappable or (mappable and self.runway.initial_view):
            self.runway.robot = mappable
            if mappable and (self.runway.initial_view or self.follow.isChecked()):
                self.runway.initial_view = False
                self.runway.center = mappable[:2]
            self.runway.update()
        self.locate.setEnabled(bool(mappable))
        if pose:
            lat, lon, bearing = pose
            course = "heading unavailable" if bearing is None else f"{bearing:.1f}° true"
            caption = f"UGV   {lat:.7f}°, {lon:.7f}°   ·   {course}"
        elif self.track.position:
            stamp = eastern(self.track.stamp / 1e9) + " ET"
            caption = f"UGV location stale · Last fix {stamp}"
        elif ready:
            caption = "Waiting for a fresh UGV location"
        else:
            caption = "UGV location unavailable"
        self.robot_caption.setText(caption)
        self.robot_caption.setToolTip(caption)

    def drain(self):
        if self.journal and self.journal.recording:
            self.journal.serial_data(*self.radio.transport.take_capture(True))
        for _ in range(2048):
            try:
                when, event, data = self.radio.events.get_nowait()
            except queue.Empty:
                break
            if event == "incoming":
                topic = data["topic"]
                arrived = data.get("arrival_mono", time.monotonic())
                try:
                    name = self.radio.engine.topics[topic].type
                    msg = self.codec.decode(data["payload"], name)
                    if topic == 24:
                        stamp = msg.stamp.sec * 10**9 + msg.stamp.nanosec
                        now_ns = time.time_ns()
                        timely = telemetry_time_ok(stamp, now_ns)
                        if self.radio.ready and not timely and not self.app_fresh():
                            warning = ("Jetson status is ahead of the GS clock. Sync both clocks."
                                       if stamp > now_ns else "Jetson status arrived stale. Check clocks and radio delay.")
                            if warning != self.status_warning:
                                self.status_warning = warning
                                self.event_line(warning)
                                self.journal.log("status_rejected", warning, received=when,
                                                 source_ns=stamp, age_ms=round((now_ns - stamp) / 1e6))
                        if (
                            self.radio.ready
                            and stamp >= self.track.connected
                            and stamp > self.app_stamp
                            and 0 <= msg.stamp.nanosec < 10**9 and timely
                        ):
                            self.status_warning = ""
                            msg.detail, separator, reply = msg.detail.partition("|d1:")
                            if separator and self.tests_dialog:
                                self.tests_dialog.reply(reply)
                            base, separator, metadata = msg.detail.partition("|h1:")
                            if separator:
                                msg.detail = base
                                if len(metadata) <= 256:
                                    try:
                                        old_wait = self.health_panel.waiting_for
                                        self.health_panel.diagnostics(json.loads(metadata), stamp)
                                        wait = self.health_panel.waiting_for
                                        if wait and wait != old_wait:
                                            self.event_line("Bring up · " + wait, source="UGV")
                                            self.journal.log("bringup_wait", wait, received=when)
                                    except (ValueError, RecursionError):
                                        pass
                            self.health_panel.observed["Radio"] = when / 1e9
                            previous = self.app_snapshot
                            if self.journal.recording:
                                self.journal.log("field_telemetry", self.fsm_panel.states.get(msg.fsm_state, ("Unknown", ""))[0],
                                                 received=when, source_ns=stamp, battery_percent=msg.battery_percent,
                                                 estop=msg.estop, health=msg.health, job_state=msg.state)
                            if msg.detail.startswith("Lynx E-stop") and (previous is None or previous.detail != msg.detail):
                                self.event_line(msg.detail, source="UGV")
                                self.journal.log("platform_stop", msg.detail, received=when)
                            if previous is None or previous.fsm_state != msg.fsm_state:
                                state = self.fsm_panel.states.get(msg.fsm_state)
                                self.event_line("FSM · " + (state[1] if state else "Unknown"), source="UGV")
                                self.journal.log(
                                    "fsm_sample",
                                    state[1] if state else "Unknown",
                                    received=when,
                                    code=msg.fsm_state,
                                    source_ns=stamp,
                                )
                            if previous is None or previous.health != msg.health:
                                for key, name, good, fault, _ in PROBES:
                                    shift = getattr(msg, key)
                                    level = (msg.health >> shift) & 3
                                    if previous is None or level != (previous.health >> shift) & 3:
                                        text = f"{name} · " + ("No report", good, "Degraded", fault)[level]
                                        self.event_line(text, source="UGV")
                                        self.journal.log("health_change", text, received=when)
                                self.journal.log(
                                    "subsystem_health",
                                    f"0x{msg.health:04x}",
                                    received=when,
                                    health=msg.health,
                                )
                            self.app_snapshot, self.app_stamp = msg, stamp
                            self.app_received = self.last_rx = arrived
                            if (
                                self.pending_until
                                and msg.job_id == self.pending_job
                                and (self.pending_op == "start" or msg.state in {
                                    "pause": ("pausing", "paused"), "continue": ("resuming", "running"),
                                    "stop": ("stopping", "stopped", "finished", "failed"),
                                }.get(self.pending_op, ()))
                            ):
                                self.pending_until = 0
                            if msg.job_id and self.sim_view.job != msg.job_id:
                                self.sim_view.reset()
                                self.sim_view.job = msg.job_id
                            if msg.state in SIM_ACTIVE[:-1] and msg.job_id:
                                if self.sim_view.accept_scene(msg.detail):
                                    self.journal.log("sim_layout", msg.detail, job_id=msg.job_id)
                                if self.sim_view.accept(msg, stamp):
                                    self.journal.log("sim_preview", msg.detail, job_id=msg.job_id)
                            if previous is None or (previous.job_id, previous.state) != (msg.job_id, msg.state):
                                self.event_line(f"Jetson  ·  {msg.app} {msg.state}", source="UGV")
                                self.journal.log(
                                    "app_status",
                                    msg.detail,
                                    state=msg.state,
                                    job_id=msg.job_id,
                                )
                        continue
                    if topic == 25:
                        stamp = msg.stamp.sec * 10**9 + msg.stamp.nanosec
                        if (
                            self.radio.ready
                            and 0 <= msg.stamp.nanosec < 10**9
                            and stamp >= self.track.connected
                            and telemetry_time_ok(stamp, time.time_ns())
                        ):
                            points = route_points(msg.coordinates_e7)
                            if self.runway.tracks.replace(stamp, points):
                                self.last_rx = arrived
                                self.journal.log(
                                    "plan",
                                    "Nav2 display waypoints",
                                    received=when,
                                    source_ns=stamp,
                                    points=points,
                                )
                                self.runway.update()
                        continue
                    if topic == 20:
                        if self.radio.ready and self.track.accept(msg):
                            self.runway.tracks.fix(self.track.stamp, self.track.position)
                            self.runway.update()
                            self.last_rx = arrived
                            self.journal.log(
                                "location",
                                "UGV position",
                                received=when,
                                source_ns=self.track.stamp,
                                pose=self.track.position,
                            )
                        continue
                    self.station.receive(topic, msg, when)
                    self.last_rx = arrived
                    parts = [
                        getattr(msg, field, "")
                        for field in ("mission_id", "state", "status", "notes", "kind")
                    ]
                    self.event_line(
                        "UGV  ·  " + "  ·  ".join(part for part in parts if part),
                        getattr(msg, "mission_id", "") or getattr(msg, "active_mission_id", ""), source="UGV",
                    )
                except OSError:
                    raise
                except Exception as err:  # noqa: BLE001 - reject malformed incoming data and keep the operator loop alive
                    self.journal.log("invalid_message", str(err), received=when, topic=topic)
                    self.event_line("Invalid message: " + str(err), source="UGV")
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
        self.track = LocationTrack()
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
        tracks = self.runway.tracks
        if not ready and tracks.plan:
            if self.journal:
                try:
                    self.journal.log("plan_lost", "Radio unavailable")
                except OSError as err:
                    self.log_failure(err)
                    return
            tracks.plan = ()
            self.runway.update()
        elif tracks.expire(time.time_ns()):
            self.runway.update()
        self.clear_track.setEnabled(bool(tracks.fixes))
        self.update_location(ready)
        if self.simulation and self.field_busy():
            self.set_workspace(False)
            return
        if self.leave_job and self.app_fresh():
            snap = self.app_snapshot
            if snap.job_id != self.leave_job:
                self.leave_job = None
            elif snap.state in ("stopped", "finished", "failed"):
                self.leave_job = None
                self.set_workspace(False)
                return
        self.update_cockpit(ready)
        st = self.station
        points = demo_obstacles(st.area, st.callsign) if st and st.session else ()
        if self.runway.obstacles != points:
            self.runway.obstacles = points
            self.runway.update()
        self.obstacle_key.setVisible(bool(points))
        self.sim_button.setEnabled(not self.field_busy())
        self.sim_button.setToolTip("Finish the field session first" if self.field_busy() else "Seeded Nav2 missions")
        self.telemetry_strip.setVisible(self.radio is not None and not self.simulation)
        self.fit_route.setEnabled(
            bool(self.runway.targets or tracks.fixes or tracks.ahead or points)
        )
        self.demo_button.setEnabled(self.radio is None)
        self.clock.setText(eastern() + " ET")
        callsign = (st.callsign or self.port.currentText()) if ready else ""
        self.identity.setText(
            self.identity.fontMetrics().elidedText(callsign, Qt.TextElideMode.ElideRight, 120)
        )
        self.identity.setToolTip(callsign)
        self.open_recording_action.setEnabled(self.radio is None)
        self.export_action.setEnabled(self.log_folder is not None and self.radio is None)
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
                state = "Waiting for Jetson"
        snap = self.app_snapshot if ready and self.app_fresh() else None
        health_key = (state, self.app_stamp if snap else None, int(time.monotonic()))
        if health_key != self.health_key:
            self.health_key = health_key
            if snap:
                phase = self.fsm_panel.states.get(snap.fsm_state, ("", "Unknown"))[1].replace("_", " ").capitalize()
                fix = self.track.current()
                self.health_panel.values["GNSS"] = f"{fix[0]:.6f}°, {fix[1]:.6f}°" if fix else "—"
                self.health_panel.values["Mission FSM"] = phase
                if snap.fsm_state == snap.FSM_BRING_UP_SYSTEM and self.health_panel.waiting_for:
                    self.health_panel.values["Mission FSM"] += " · " + self.health_panel.waiting_for
            watch = ("Battery", "E-stop", "Camera")
            before = {name: self.health_panel.rows[name].text() for name in watch}
            self.health_panel.display(state, snap)
            if snap and self.journal:
                for name in watch:
                    status = self.health_panel.rows[name].text()
                    if status != before[name]:
                        text = f"{name} · {status}"
                        self.event_line(text, source="UGV")
                        try:
                            self.journal.log("health_change", text, source_ns=self.app_stamp)
                        except OSError as err:
                            self.log_failure(err)
                            return
            self.health_summary.setText(self.health_panel.summary())
        if self.link_state != state:
            self.link_state = state
            bg, fg = {
                "Disconnected": ("#e9eff0", "#476069"),
                "Waiting for Jetson": ("#fff1cb", "#754b08"),
                "Link lost": ("#ffe4de", "#96382d"),
                "Port unavailable": ("#ffe4de", "#96382d"),
                "Contract mismatch": ("#ffe4de", "#96382d"),
                "Radio ready": ("#d6f1e8", "#125d4b"),
            }[state]
            self.link.setText("Connect radio…" if state == "Disconnected" else state)
            self.link.setStyleSheet(f"background: {bg}; color: {fg};")
        self.open_logs.setEnabled(self.log_folder is not None)
        self.start.setText(
            "Retry start" if st and st.state == "awaiting acceptance" else "Start session"
        )
        adapter_offline = bool(
            self.app_snapshot
            and (not self.app_fresh() or not self.app_snapshot.mission_adapter_online)
        )
        bringing_up = bool(snap and snap.fsm_state in (snap.FSM_TURN_ON, snap.FSM_BRING_UP_SYSTEM))
        self.start.setToolTip(
            "Field session input is unavailable. Check the Jetson bringup profile and health." if adapter_offline
            else self.health_panel.waiting_for or "Waiting for field bringup" if bringing_up else ""
        )
        self.start.setEnabled(
            ready and not self.simulation
            and not (self.pending_until or (self.app_fresh() and self.app_snapshot.state in SIM_ACTIVE))
            and not self.status_warning and not adapter_offline and not bringing_up
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
        self.start.setVisible(not (st and st.operating))
        closed = bool(st and st.scanned)
        self.scan_state.setVisible(closed)
        self.scan.setVisible(bool(st and st.operating and not closed))
        self.add_target.setVisible(bool(st and st.operating and not closed))
        self.stops.setVisible(self.radio is not None and not self.simulation)
        can_stop = bool(ready and not self.simulation and st.can_halt)
        can_estop = bool(ready and not self.simulation and snap and snap.state not in SIM_ACTIVE
                         and self.health_panel.can_trigger())
        self.stops.setEnabled(can_stop or can_estop)
        self.stops.setText("Stop controls" if can_estop and not can_stop else "Stop mission")
        self.stops.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup if can_estop and not can_stop
                                else QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.stops.setToolTip("Stop autonomous navigation and cleaning in this session" if can_stop else
                              "Open available stop controls" if can_estop else
                              "Requires a ready radio link and an accepted field mission")
        recording = bool(self.journal and self.journal.recording)
        self.record_button.setVisible(recording or bool(ready and not self.simulation and self.app_fresh()
                                     and self.app_snapshot.fsm_state == self.app_snapshot.FSM_BRING_UP_SYSTEM))
        self.record_button.setText("Stop recording" if recording else "Record")
        self.trigger_estop.setEnabled(can_estop)
        self.field_pause.setVisible(bool(not self.simulation and st and st.session and (st.operating or st.state == "paused")))
        self.field_pause.setEnabled(bool(ready and st and self.health_panel.can_pause() and (st.operating or st.state == "paused")))
        self.field_pause.setText("Continue mission" if st and st.state == "paused" else "Pause mission")
        self.halt.setEnabled(bool(ready and not self.simulation and st.can_halt))
        self.abort.setEnabled(bool(ready and not self.simulation and (st.operating or st.state == "paused") and st.state != "returning"))
        scanning = bool(ready and not self.simulation and st.operating and not st.scanned and not st.scanning_requested)
        self.scan.setText(
            "Scanning closed"
            if st and st.scanned
            else "Scan requested"
            if st and st.scanning_requested
            else "Scanning complete"
        )
        self.abort.setText("Return requested" if st and st.abort_requested else "Return to base")
        self.add_target.setEnabled(scanning)
        self.scan.setEnabled(scanning)
        self.submit.setEnabled(
            scanning and self.draft is not None and all(e.text().strip() for e in self.coords)
        )
        age = max(0.0, time.monotonic() - self.last_rx) if self.last_rx is not None else None
        age_text = "" if age is None else f"{int(age * 1000)} ms" if age < 1 else f"{age:.1f} s"
        self.last_update.setText(f"Last UGV message  {age_text} ago" if age_text else "")
        note = st.note if st else ""
        if note == "OK_TO_MOVE_FORWARD":
            note = "Waiting for operator review"
        elif note.isupper() and "_" in note:
            note = note.replace("_", " ").capitalize()
        self.status.setText(
            "   ·   ".join(
                x
                for x in (
                    st.area,
                    "Supervised session" if st.operating else st.state.capitalize(),
                )
                if x
            )
            if st and st.session
            else "Simulation running · field start unavailable"
            if self.app_fresh() and self.app_snapshot.state in SIM_ACTIVE
            else "Field standby · mission inputs disabled"
            if ready and adapter_offline and self.app_fresh() and self.app_snapshot.fsm_state
            else "Waiting for mission adapter"
            if ready and adapter_offline
            else "Connect the radio to start a session." if not self.radio
            else "No active session"
        )
        self.status.setToolTip(note or self.status.text())
        self.phase_readout.setVisible(self.radio is not None)
        self.phase_readout.setText(self.fsm_panel.current.text())
        active = st.active if st else ""
        # Selection reports and session snapshots arrive independently. The station
        # orders them by UGV stamp; review decisions still use the session's own ID.
        display_active = st.selected_fod if st and st.selected_fod not in st.final_reports else ""
        self.active_target.setVisible(bool(display_active))
        self.active_target.setToolTip(display_active)
        self.active_target.setText(
            ("Active target" if ready else "Last reported target")
            + "   "
            + self.runway.target_name(display_active)
        )
        if self.runway.active != display_active:
            self.runway.active = display_active
            self.runway.update()
        location = self.runway.center
        self.runway.setAccessibleDescription(
            f"WGS84   ·   Map center   {location[0]:.5f}°, {location[1]:.5f}°"
            if location
            else "WGS84   ·   No location selected"
        )
        self.update_targets(st, display_active)
        token = st.review_token() if st else None
        waiting = bool(
            st
            and st.operating
            and st.review
            and not token
            and st.latest.get((active, "review")) not in st.decisions
        )
        selected = self.runway.selected
        evidence_target = active if token or waiting else selected
        before = st.latest.get((evidence_target, "before")) if st else None
        review_item = st.latest.get((evidence_target, "review")) if st else None
        presentation = (token, before, review_item, waiting, evidence_target)
        if presentation != self.presentation:
            self.presentation = presentation
            if token and token != self.shown:
                self.select_target(active)
            self.shown = token
            kind = st.records[token][0] if token else ""
            self.approve.setText(
                "Approve removal"
                if kind == "after"
                else "Continue unverified"
                if kind == "cycle_complete"
                else "Continue"
            )
            self.retry.setText("Search again" if kind == "not_found" else "Clean again")
            display = {
                "cycle_complete": "Cleaning cycle finished · Removal unverified",
                "after": "Inspect the after image",
                "not_found": "FOD not found · Inspect the search area",
            }
            target = self.runway.target_name(evidence_target or "")
            self.review_heading.setText(
                "Operator review" if token or waiting else "Recorded evidence"
            )
            self.review_text.setText(
                f"{target}   ·   {display[kind]}"
                if token
                else f"{target}   ·   Waiting for evidence"
                if waiting
                else target
            )
            self.review_text.setToolTip(evidence_target or "")
            for title, label, item, fallback in zip(
                self.picture_titles,
                self.pictures,
                (before, review_item),
                ("Before", "Review"),
            ):
                name = st.records[item][0].replace("_", " ").capitalize() if item else fallback
                title.setText(name)
                label.set_image(QPixmap())
                label.setToolTip("")
                if item:
                    captured = st.image_times.get(item)
                    if captured:
                        title.setText(name + "  " + eastern(captured / 1e9) + " ET")
                        label.setToolTip(eastern(captured / 1e9) + " ET")
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
        has_image = any(not image.original.isNull() for image in self.pictures)
        self.cycle_summary.setVisible(bool(token and st.records[token][0] == "cycle_complete"))
        self.inspect.setVisible(has_image)
        self.inspect.setEnabled(has_image)
        self.approve.setVisible(bool(token or waiting))
        self.retry.setVisible(bool(token and st.records[token][0] != "cycle_complete"))
        self.approve.setEnabled(ready and image_ok)
        self.retry.setEnabled(
            bool(ready and token and st.records[token][0] != "cycle_complete" and image_ok)
        )

        pending = bool(ready and (token or waiting))
        available = bool(ready and (token or waiting or before or review_item))
        self.open_review.setVisible(available)
        self.open_review.setText("Open operator review" if pending else "View recorded evidence")
        self.later.setText("Review later" if pending else "Close")
        for picture, caption in zip(self.pictures, self.picture_titles):
            visible = not picture.original.isNull() or (
                caption is self.picture_titles[1]
                and token
                and st.records[token][0] != "cycle_complete"
            )
            picture.setVisible(bool(visible))
            caption.setVisible(bool(visible))
        request = (st.session, token) if ready and token else None
        if request != getattr(self, "announced_review", None):
            self.announced_review = request
            if request:
                self.show_review()
            elif self.review_page.isVisible():
                self.review_page.reject()
        if not available and self.review_page.isVisible():
            self.review_page.reject()

    def update_targets(self, st, active):
        queued = [n for n in st.queue if n != active and n not in st.final_reports] if st else []
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
        selected = previous.data(Qt.ItemDataRole.UserRole) if previous else active
        self.tasks.blockSignals(True)
        self.queue_rows = rows
        self.tasks.setVisible(bool(rows))
        self.empty_targets.setVisible(not rows)
        self.tasks.setMaximumHeight(34 + 30 * min(8, len(rows)))
        self.tasks.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, value in enumerate(row):
                title = self.runway.target_name(value) if j == 0 else value
                if j == 1 and value:
                    title = value.split(" · ", 1)[0]
                    if title == value and reports.get(row[0]):
                        title = (
                            "Awaiting UGV"
                            if value == "awaiting UGV"
                            else value.split(" ", 1)[0].replace("_", " ").capitalize()
                        )
                item = QTableWidgetItem(title)
                item.setData(Qt.ItemDataRole.UserRole, row[0])
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
        self.settings.setValue("splitter", self.splitter.saveState())
        self.disconnect()
        self.runway.stop_tiles()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setApplicationName("RavenStrike-GS")
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        try:
            logs = (
                Path(
                    QStandardPaths.writableLocation(
                        QStandardPaths.StandardLocation.AppLocalDataLocation
                    )
                )
                / "logs"
            )
            logs.mkdir(parents=True, exist_ok=True)
            crash_log = (logs / "desktop-crash.log").open("a", encoding="utf-8", buffering=1)
            if sys.stderr is None:
                sys.stderr = crash_log
            faulthandler.enable(crash_log)
        except OSError:
            pass
    window = Window()
    window.show()
    if "--self-test" in sys.argv:

        def check_package():
            # Exercise the packaged GUI, wire map and dynamically loaded CDR store offline.
            app.processEvents()
            if getattr(sys, "frozen", False) and window.build == "unknown":
                raise RuntimeError("Packaged build ID is missing")
            if any(window.tabs.tabIcon(i).pixmap(20, 20).isNull() for i in range(window.tabs.count())):
                raise RuntimeError("Packaged workspace icons unavailable")
            if window.runway.overview.isNull() or window.runway.layer != "Satellite":
                raise RuntimeError("Packaged satellite site overview is unavailable")
            if len(sys.argv) >= 3 and sys.argv[1] == "--self-test":
                from PySide6.QtCore import QPoint
                from PySide6.QtTest import QTest

                import runway_map

                # The build test serves separate JPEG imagery and PNG street tiles over loopback.
                if sys.argv[2] != "online":
                    runway_map.tile_url = lambda layer, z, x, y: QUrl(
                        f"{sys.argv[2]}/tile.{'jpg' if layer == 'Satellite' else 'png'}?tile={z}-{x}-{y}"
                    )
                view = window.runway
                buttons = {
                    btn.text(): btn for btn in window.findChildren(QPushButton, "layerButton")
                }
                center = view.rect().center()

                def wait_for_tile(layer, zoom):
                    until = time.monotonic() + 15
                    while time.monotonic() < until:
                        if any(key[:2] == (layer, zoom) for key in view.tiles):
                            return
                        QTest.qWait(30)
                    raise RuntimeError(f"{layer} tile failed at zoom {zoom}")

                # Start with the reported gesture. At this width a single zoom-out
                # exposes the edge of the bundled site image and requests online imagery.
                window.resize(1400, 800)
                app.processEvents()
                QTest.wheelEvent(
                    window.windowHandle(), view.mapTo(window, view.rect().center()), QPoint(0, -120)
                )
                assert view.zoom == SITE_ZOOM - 1
                app.processEvents()
                center = view.rect().center()
                QTest.mousePress(view, Qt.MouseButton.LeftButton, pos=center)
                QTest.mouseMove(view, center + QPoint(360, 65))
                QTest.qWait(80)
                QTest.mouseRelease(view, Qt.MouseButton.LeftButton, pos=center + QPoint(360, 65))
                assert view.center != SITE
                wait_for_tile("Satellite", view.zoom)
                QTest.wheelEvent(
                    window.windowHandle(), view.mapTo(window, view.rect().center()), QPoint(0, -120)
                )
                wait_for_tile("Satellite", SITE_ZOOM - 2)
                window.locate_site()
                app.processEvents()
                QTest.mousePress(view, Qt.MouseButton.LeftButton, pos=center)
                QTest.mouseMove(view, center + QPoint(80, 20))
                QTest.qWait(50)
                assert view.center != SITE
                QTest.mouseMove(view, center)
                QTest.qWait(50)
                QTest.mouseRelease(view, Qt.MouseButton.LeftButton, pos=center)
                assert not window.draft_dialog.isVisible()
                QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=center)
                assert window.draft_dialog.isVisible()
                window.draft_dialog.close()
                for _ in range(4):
                    QTest.mouseClick(buttons["Street"], Qt.MouseButton.LeftButton)
                    before = view.center
                    QTest.mousePress(view, Qt.MouseButton.LeftButton, pos=center)
                    offset = center + QPoint(70, 25)
                    QTest.mouseMove(view, offset)
                    QTest.qWait(50)
                    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, pos=offset)
                    assert view.center != before
                    QTest.mouseClick(buttons["Satellite"], Qt.MouseButton.LeftButton)
                QTest.mouseClick(buttons["Street"], Qt.MouseButton.LeftButton)
                wait_for_tile("Street", view.zoom)
            window.sim_button.click()
            if window.map_page.currentWidget() is not window.sim_view or window.launch_app.isEnabled():
                raise RuntimeError("Offline simulation workspace is unavailable or armed")
            window.field_button.click()
            if window.map_page.currentWidget() is not window.field_map:
                raise RuntimeError("Field map did not restore")
            replay = DemoReplay(window)
            replay.slider.setValue(replay.slider.maximum())
            if replay.phase.text() != "Mission complete at base":
                raise RuntimeError("Packaged demo recording is incomplete")
            replay.close()
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
            launch = window.codec.types["AppCommand"](
                window.codec.stamp(), "a" * 32, "start", "seeded-mission", "42", False
            )
            if (
                window.codec.decode(window.codec.encode(launch), radio.engine.topics[21].type)
                != launch
            ):
                raise RuntimeError("Packaged app command failed the CDR round trip")
            if window.launch_app.isEnabled() or window.submit.isEnabled() or radio.ready:
                raise RuntimeError("Disconnected station accepted mission input")
            window.close()

        try:
            check_package()
        except (AssertionError, RuntimeError, OSError, ValueError):
            import traceback

            traceback.print_exc(file=sys.stderr)
            sys.exit(1)
    else:
        sys.exit(app.exec())
