"""Run the field ground station with python gs.py. Nothing transmits on startup."""

import faulthandler
import json
import math
import queue
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

# The frozen executable also hosts the map worker. It opens no window or radio.
if __name__ == "__main__" and sys.argv[1:2] == ["--fetch-map-tiles"]:
    from map_tiles import main as fetch_map_tiles

    sys.exit(fetch_map_tiles(sys.argv[2]))

from PySide6.QtCore import QSettings, QStandardPaths, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QDesktopServices,
    QImageReader,
    QPixmap,
    QTextBlockUserData,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
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
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from serial.tools import list_ports

from appearance import field_theme
from demo_replay import DemoReplay
from demo_site import SITE, SITE_ZOOM, demo_obstacles
from display import eastern
from fsm_panel import FsmPanel
from health_panel import HealthPanel
from location import LocationTrack
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

    def __init__(self, text, parent):
        super().__init__(text)
        self.action = QAction(text, parent)
        self.clicked.connect(self.action.trigger)

    def setEnabled(self, enabled):
        self.action.setEnabled(bool(enabled))
        super().setEnabled(bool(enabled))

    def setText(self, text):
        self.action.setText(text)
        super().setText(text)


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        field_theme(QApplication.instance())
        self.build = build_id()
        self.codec = Codec()
        self.app_snapshot = None
        self.app_stamp = self.app_received = self.pending_until = 0
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
        bar.addWidget(label("Ground station", "headerText"))
        bar.addStretch()
        self.identity = label("Field console", "headerText")
        bar.addWidget(self.identity)
        self.link = self.button(bar, "Disconnected", lambda: self.radio_dialog.show())
        self.link.setAccessibleName("Radio connection status and settings")
        self.link.setObjectName("link")
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
        self.mission_panel(workspace)
        telemetry = QFrame()
        self.telemetry_strip = telemetry
        telemetry.setObjectName("telemetry")
        self.telemetry_bar = QHBoxLayout(telemetry)
        self.telemetry_bar.setContentsMargins(12, 3, 8, 3)
        workspace.addWidget(telemetry)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        workspace.addWidget(self.splitter, 1)
        self.tabs = QTabWidget()
        self.splitter.addWidget(self.tabs)
        self.map_page = QWidget()
        self.tabs.addTab(self.map_page, "Mission map")
        self.map_panel(QVBoxLayout(self.map_page))
        inspector = QFrame()
        inspector.setObjectName("card")
        inspector.setMinimumWidth(310)
        self.inspector_card = inspector
        self.splitter.addWidget(inspector)
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

    def map_panel(self, column):
        pane = column
        bar = QHBoxLayout()
        pane.addLayout(bar)
        self.map_heading = label("Demo site", "heading")
        bar.addWidget(self.map_heading)
        bar.addStretch()
        self.runway = RunwayMap()
        bar.addWidget(self.runway.layer_switch())
        self.runway.pointPicked.connect(self.pick_location)
        self.runway.targetPicked.connect(self.select_target)
        self.locate = self.button(bar, "Locate UGV", self.locate_robot)
        self.fit_route = self.button(bar, "Fit route", self.fit_targets)
        for control in (self.locate, self.fit_route):
            control.setObjectName("mapTool")
        self.zoom_out = self.button(bar, "−", lambda: self.runway.change_zoom(-1))
        self.zoom_in = self.button(bar, "+", lambda: self.runway.change_zoom(1))
        for button, name in [(self.zoom_out, "Zoom out"), (self.zoom_in, "Zoom in")]:
            button.setFixedWidth(35)
            button.setAccessibleName(name)
            button.setToolTip(name)
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

        meters = self.telemetry_bar
        meters.setSpacing(8)
        self.instruments = []
        for name in ("Battery", "E-stop", "Heading"):
            meters.addWidget(label(name, "field"))
            value = label("—", "meter")
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            value.setMinimumWidth(34)
            meters.addWidget(value)
            separator = QFrame()
            separator.setFrameShape(QFrame.Shape.VLine)
            separator.setObjectName("rule")
            meters.addWidget(separator)
            self.instruments.append(value)
        meters.addStretch()
        self.health_panel = HealthPanel(self.codec.types["FieldStatus"])
        health_bar = QHBoxLayout()
        self.telemetry_bar.addLayout(health_bar)
        self.health_summary = label("", "muted")
        self.health_summary.setWordWrap(True)
        health_bar.addWidget(self.health_summary, 1)
        self.health_details = QPushButton("System details")
        self.health_details.setObjectName("mapTool")
        self.health_details.setCheckable(True)
        health_bar.addWidget(self.health_details)
        self.health_details.toggled.connect(self.health_panel.setVisible)
        self.health_details.toggled.connect(
            lambda checked: self.tabs.setCurrentWidget(self.map_page) if checked else None
        )
        pane.addWidget(self.health_panel)
        self.health_panel.hide()
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
        row = QHBoxLayout()
        column.addLayout(row)
        self.status = label("No active session", "heading")
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row.addWidget(self.status, 2)
        self.active_target = label("", "muted")
        self.active_target.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row.addWidget(self.active_target, 1)
        self.idle_connect = self.button(row, "Connect radio…", self.radio_dialog.show)
        self.idle_connect.setObjectName("primary")
        self.start = self.button(row, "Start session", self.start_session)
        self.start.setObjectName("primary")
        self.add_target = self.button(row, "Add FOD…", self.open_draft)
        self.add_target.setObjectName("targetAction")
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
        self.scan_state = label("Scanning closed", "closed")
        row.insertWidget(row.indexOf(self.abort), self.scan_state)

    def activity_panel(self, column):
        pane = column
        pane.setContentsMargins(14, 12, 14, 12)
        self.inspector = label("Mission inspector", "heading")
        pane.addWidget(self.inspector)
        phase_bar = QHBoxLayout()
        pane.addLayout(phase_bar)
        self.phase_readout = label("", "phase")
        phase_bar.addWidget(self.phase_readout, 1)
        self.button(
            phase_bar,
            "Inspect FSM  ›",
            lambda: self.tabs.setCurrentWidget(self.fsm_panel),
        ).setObjectName("mapTool")
        self.queue_caption = label("Target register", "section")
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
        self.tasks.setFrameShape(QFrame.Shape.NoFrame)
        self.tasks.setAlternatingRowColors(True)
        self.tasks.itemActivated.connect(self.locate_row)
        self.tasks.verticalHeader().setDefaultSectionSize(30)
        self.tasks.setMinimumHeight(65)
        pane.addWidget(self.tasks, 1)
        self.empty_targets = label("Submitted and UGV-reported targets appear here.", "muted")
        self.empty_targets.setWordWrap(True)
        self.empty_targets.setAlignment(Qt.AlignmentFlag.AlignCenter)
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
        pane.addStretch(1)
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
        self.tabs.addTab(self.fsm_panel, "State machine")
        activity = self.activity = QWidget()
        self.tabs.addTab(activity, "Activity")
        pane = QVBoxLayout(activity)
        bar = QHBoxLayout()
        pane.addLayout(bar)
        bar.addWidget(label("Event log", "heading"))
        bar.addStretch()
        self.open_logs = self.button(bar, "Open folder", self.open_log_folder)
        self.log = ActivityLog()
        self.log.setReadOnly(True)
        self.log.targetPicked.connect(self.select_target)
        self.log.setMaximumBlockCount(400)
        self.log.setPlaceholderText("Radio and operator events appear here.")
        pane.addWidget(self.log)

        apps = self.apps_dialog = QDialog(self)
        apps.setWindowTitle("Jetson simulations")
        layout = QVBoxLayout(apps)
        layout.addWidget(label("Simulation launcher", "heading"))
        layout.addWidget(label("Isolated software jobs · field telemetry stays live.", "muted"))
        self.catalog = json.loads((CONTRACT / "apps.json").read_text())
        self.app_choice = QComboBox()
        for key, spec in self.catalog.items():
            self.app_choice.addItem(spec["label"], key)
        self.scenario = QComboBox()
        self.scenario.addItems(self.catalog["scenario"]["scenarios"])
        self.scenario.setCurrentText(self.catalog["scenario"]["defaults"]["scenario"])
        self.wait_timeout = QCheckBox("Exercise operator timeout")
        self.wait_timeout.setChecked(self.catalog["scenario"]["defaults"]["wait_timeout"])
        layout.addWidget(self.app_choice)
        layout.addWidget(self.scenario)
        layout.addWidget(self.wait_timeout)
        actions = QHBoxLayout()
        layout.addLayout(actions)
        self.launch_app = self.button(actions, "Run simulation", lambda: self.app_command("start"))
        self.stop_app = self.button(actions, "Stop simulation", lambda: self.app_command("stop"))
        self.job_label = label("", "muted")
        self.job_label.setWordWrap(True)
        layout.addWidget(self.job_label)
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
        self.demo_button = QAction("Demo replay", self)
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

        mission = self.menuBar().addMenu("&Mission")
        for button in (self.start, self.add_target, self.scan, self.abort):
            mission.addAction(button.action)
        self.add_target.action.setShortcut("Ctrl+N")

        tools = self.menuBar().addMenu("&Tools")
        action(tools, "Radio connection…", self.radio_dialog.show)
        action(tools, "Subsystem health", self.show_health)
        action(tools, "Jetson simulations…", self.apps_dialog.show)
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

    def show_guide(self):
        QMessageBox.information(
            self,
            "Operator guide",
            "Connect the USB radio from the header. Wait for Radio ready, then start a supervised session.\n\n"
            "Use Add FOD or click the map. Enter ellipsoid altitude, preview the location and send. Select a marker, target row or target event to inspect its history.\n\n"
            "Finish scanning closes submissions. Review opens automatically. Use Open operator review in the inspector to return to a deferred decision. Abort to base requests a return.\n\n"
            "Open recordings and export evidence from File. Jetson simulation jobs are under Tools.",
        )

    def open_recording(self):
        if self.radio:
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open session recording",
            str(self.log_folder or ""),
            "GS journal (events.jsonl)",
        )
        if path:
            try:
                dialog = JournalReplay(Path(path), self)
            except (ValueError, OSError) as err:
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

    def restore_layout(self):
        self.tabs.setCurrentWidget(self.map_page)
        self.splitter.setSizes([900, 350])
        self.follow.setChecked(self.settings.value("follow", False, type=bool))
        self.health_details.setChecked(False)

    def show_health(self):
        self.tabs.setCurrentWidget(self.map_page)
        self.health_details.setChecked(True)

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
        key = self.app_choice.currentData()
        scenario = self.scenario.currentText() if key == "scenario" else ""
        timeout = bool(scenario and self.wait_timeout.isChecked())
        if operation == "stop":
            if snap.state != "running":
                raise ValueError("No running simulation.")
            request, key, scenario, timeout = snap.job_id, snap.app, "", False
        else:
            if snap.state in ("running", "stopping"):
                raise ValueError("A simulation is already active.")
            request = uuid4().hex
        msg = self.codec.types["AppCommand"](
            self.codec.stamp(), request, operation, key, scenario, timeout
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
            and (-0.2 <= (time.time_ns() - self.app_stamp) / 1e9 < 3)
        )

    def update_cockpit(self, ready):
        snap = self.app_snapshot if ready and self.app_fresh() else None
        idle = not (self.station and self.station.session) and (
            self.app_snapshot is None
            or (snap is not None and snap.state == "idle" and not snap.mission_adapter_online)
        )
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
        running = snap and snap.state in ("running", "stopping")
        available = bool(snap and not running and not pending)
        self.app_choice.setEnabled(available)
        scenario = self.app_choice.currentData() == "scenario"
        self.scenario.setVisible(scenario)
        self.wait_timeout.setVisible(scenario)
        self.scenario.setEnabled(available)
        self.wait_timeout.setEnabled(available)
        self.launch_app.setEnabled(available)
        self.stop_app.setEnabled(bool(snap and snap.state == "running" and not pending))
        self.job_label.setText(
            "Waiting for Jetson confirmation"
            if pending
            else f"{snap.state.capitalize()}  {snap.detail}"
            if snap
            else "Waiting for current Jetson status"
        )

    def show_review(self):
        if self.open_review.isHidden():
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
            f"Link: {self.link.text()}",
            f"Log folder: {self.log_folder or 'no connection yet'}",
        ]
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

    def event_line(self, text, mission=""):
        if text != self.last_event:
            self.log.appendPlainText(eastern() + "   " + self.runway.target_name.in_text(text))
            if mission:
                data = QTextBlockUserData()
                data.mission = mission
                self.log.document().lastBlock().setUserData(data)
        self.last_event = text
        self.feedback.setText(self.runway.target_name.in_text(text))
        self.feedback.setToolTip(text)

    def button(self, row, name, fn):
        button = CommandButton(name, self)

        def invoke():
            try:
                fn()
            except OSError as err:
                self.log_failure(err)
            except (ValueError, RuntimeError) as err:
                self.event_line(str(err))
                if self.journal:
                    self.journal.log("operator_error", str(err))

        button.action.triggered.connect(invoke)
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
        self.app_stamp = self.app_received = self.pending_until = 0
        self.track = LocationTrack()
        self.runway.last_known = None
        self.invalidate_draft()
        self.radio.start()
        self.radio_dialog.accept()
        self.last_rx = None
        self.connect_button.setText("Disconnect")
        self.tick()

    def disconnect(self):
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
        if not st or not self.radio.ready:
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
            if self.station is not st or not self.radio.ready:
                raise ValueError("Connection changed. Start canceled.")
            callsign, area, x, y, yaw = [e.text().strip() for e in edits]
            self.station.start(callsign, area, (float(x), float(y), math.radians(float(yaw))))
            self.invalidate_draft()
            self.runway.reset_view()
            self.event_line("Start requested · Awaiting UGV acceptance")

    def map(self):
        lat, lon = [float(e.text()) for e in self.coords[:2]]
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("Enter valid latitude and longitude first.")
        self.follow.setChecked(False)
        self.runway.preview(lat, lon)
        self.draft = tuple(e.text() for e in self.coords)
        self.tick()

    def add(self):
        if (
            not self.station
            or not self.radio.ready
            or self.draft != tuple(e.text() for e in self.coords)
        ):
            raise ValueError("Preview the current coordinates before sending.")
        coords = tuple(float(e.text()) for e in self.coords)
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
        if not st:
            return
        session = st.session
        if QMessageBox.question(self, "Mission control", prompt) == QMessageBox.StandardButton.Yes:
            if self.station is not st or st.session != session or not self.radio.ready:
                raise ValueError("Connection or session changed. Request canceled.")
            st.command(command)
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
        for _ in range(2048):
            try:
                when, event, data = self.radio.events.get_nowait()
            except queue.Empty:
                break
            if event == "incoming":
                topic = data["topic"]
                arrived = data.get("arrival_mono", time.monotonic())
                try:
                    msg = self.codec.decode(data["payload"], self.radio.engine.topics[topic].type)
                    if topic == 24:
                        stamp = msg.stamp.sec * 10**9 + msg.stamp.nanosec
                        if (
                            self.radio.ready
                            and stamp >= self.track.connected
                            and stamp > self.app_stamp
                            and -0.2 <= (time.time_ns() - stamp) / 1e9 < 3
                        ):
                            previous = self.app_snapshot
                            if previous is None or previous.fsm_state != msg.fsm_state:
                                state = self.fsm_panel.states.get(msg.fsm_state)
                                self.journal.log(
                                    "fsm_sample",
                                    state[1] if state else "Unknown",
                                    received=when,
                                    code=msg.fsm_state,
                                    source_ns=stamp,
                                )
                            if previous is None or previous.health != msg.health:
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
                                and (self.pending_op == "start" or msg.state != "running")
                            ):
                                self.pending_until = 0
                            if previous is None or (
                                previous.job_id,
                                previous.state,
                                previous.detail,
                            ) != (msg.job_id, msg.state, msg.detail):
                                self.event_line(f"Jetson  ·  {msg.state}  {msg.detail}")
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
                            and -200_000_000 <= time.time_ns() - stamp <= 3_000_000_000
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
                        getattr(msg, "mission_id", "") or getattr(msg, "active_mission_id", ""),
                    )
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
        self.update_cockpit(ready)
        st = self.station
        points = demo_obstacles(st.area, st.callsign) if st and st.session else ()
        if self.runway.obstacles != points:
            self.runway.obstacles = points
            self.runway.update()
        self.obstacle_key.setVisible(bool(points))
        self.map_heading.setText("Demo site" if self.runway.center == SITE else "Route map")
        self.telemetry_strip.setVisible(self.radio is not None)
        self.inspector_card.setVisible(self.radio is not None)
        self.fit_route.setEnabled(
            bool(self.runway.targets or tracks.fixes or tracks.ahead or points)
        )
        self.idle_connect.setVisible(self.radio is None)
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
        self.health_panel.display(state, self.app_snapshot if ready and self.app_fresh() else None)
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
            self.link.setText(state)
            self.link.setStyleSheet(f"background: {bg}; color: {fg};")
        self.open_logs.setEnabled(self.log_folder is not None)
        self.start.setText(
            "Retry start" if st and st.state == "awaiting acceptance" else "Start session"
        )
        adapter_offline = bool(
            self.app_snapshot
            and (not self.app_fresh() or not self.app_snapshot.mission_adapter_online)
        )
        self.start.setToolTip(
            "Start the mission adapter on the Jetson first." if adapter_offline else ""
        )
        self.start.setEnabled(
            ready
            and not adapter_offline
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
        self.start.setVisible(self.radio is not None and not (st and st.operating))
        closed = bool(st and st.scanned)
        self.scan_state.setVisible(closed)
        self.scan.setVisible(bool(st and st.operating and not closed))
        self.add_target.setVisible(bool(st and st.operating and not closed))
        self.abort.setEnabled(bool(ready and st.operating))
        self.abort.setVisible(bool(st and (st.operating or st.abort_requested)))
        scanning = bool(ready and st.operating and not st.scanned and not st.scanning_requested)
        self.scan.setText(
            "Scanning closed"
            if st and st.scanned
            else "Scan requested"
            if st and st.scanning_requested
            else "Scanning complete"
        )
        self.abort.setText("Abort requested" if st and st.abort_requested else "Abort to base")
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
            else "Jetson idle · mission adapter offline"
            if ready and adapter_offline
            else "No active session"
        )
        self.status.setToolTip(note or self.status.text())
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
        self.zoom_out.setEnabled(bool(location and self.runway.zoom > 3))
        self.zoom_in.setEnabled(bool(location and self.runway.zoom < 24))
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
                QTest.mouseClick(window.zoom_out, Qt.MouseButton.LeftButton)
                assert view.zoom == SITE_ZOOM - 1
                app.processEvents()
                center = view.rect().center()
                QTest.mousePress(view, Qt.MouseButton.LeftButton, pos=center)
                QTest.mouseMove(view, center + QPoint(360, 65))
                QTest.qWait(80)
                QTest.mouseRelease(view, Qt.MouseButton.LeftButton, pos=center + QPoint(360, 65))
                assert view.center != SITE
                wait_for_tile("Satellite", view.zoom)
                QTest.mouseClick(window.zoom_out, Qt.MouseButton.LeftButton)
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
                window.codec.stamp(), "a" * 32, "start", "scenario", "retry", False
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
