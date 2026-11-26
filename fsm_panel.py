"""Read-only view of the most recent onboard FSM sample."""

from pathlib import Path
from xml.etree import ElementTree as ET

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtSvgWidgets import QGraphicsSvgItem
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from display import eastern

# Group fast decision states by activity. The exact controller state is always shown.
PHASES = {
    "Startup": ("TURN_ON", "BRING_UP_SYSTEM"),
    "Standby": (
        "STANDBY_MODE",
        "CLEANING_TASKS_AVAILABLE",
        "FOD_ID_PROCESS_COMPLETED_MESSAGE_RECEIVED",
    ),
    "Target selection": ("SELECT_FOD_TARGET", "NEW_CLOSER_FOD_DETECTED", "FOD_TARGET_DEFINITION"),
    "Navigation": ("NAVIGATION_MODE", "REACHED_FOD_SEARCH_AREA"),
    "Detection": (
        "DETECTION_MODE",
        "FOD_DETECTED_IN_SEARCH_AREA",
        "SEND_BEFORE_PICTURE_TO_GS",
        "SEND_FALSE_POSITIVE_PICTURE_TO_GS",
    ),
    "Cleaning": ("CLEANING_MODE", "SEND_AFTER_PICTURE_TO_GS"),
    "Operator review": ("OK_TO_MOVE_FORWARD",),
    "Repositioning": ("REPOSITIONING_MODE",),
    "Queue update": ("QUEUE_REMOVAL", "CAN_DETECT_MORE_FOD"),
    "Return to base": ("RETURN_TO_BASE_MODE",),
    "Completed": ("MISSION_COMPLETED",),
    "Halted": ("HALTED",),
}


class Flowchart(QGraphicsView):
    def __init__(self):
        super().__init__()
        self.setAccessibleName("Mission state flowchart")
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setScene(QGraphicsScene(self))
        self.renderer = QSvgRenderer(self)
        self.item = QGraphicsSvgItem()
        self.root = ET.parse(Path(__file__).resolve().parent / "contract/fsm.svg").getroot()
        self.nodes = {g.get("id"): g for g in self.root.findall(".//{*}g[@class='node']")}
        self.active = None
        self.follow = True
        self.inferred = False
        self.manual_zoom = False
        # fitInView during resize must not toggle scrollbars and re-enter resizeEvent.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.load_svg()
        self.item.setSharedRenderer(self.renderer)
        self.scene().addItem(self.item)
        self.setSceneRect(self.item.boundingRect())

    def load_svg(self):
        ET.register_namespace("", "http://www.w3.org/2000/svg")
        if not self.renderer.load(QByteArray(ET.tostring(self.root))):
            raise ValueError("Unable to load the mission flowchart")

    def highlight(self, state, inferred=False):
        if self.active == state and self.inferred == inferred:
            return
        self.inferred = inferred
        for name in (self.active, state):
            if name is None:
                continue
            selected = name == state
            color = "#a03832" if name == "HALTED" else "#09685f"
            if selected and inferred:
                color = "#506773"
            for elem in self.nodes[name]:
                tag = elem.tag.split("}")[-1]
                if tag in ("ellipse", "polygon"):
                    elem.set("fill", color if selected else "#f5f8f8")
                    elem.set("stroke", color if selected else "#9aadb1")
                elif tag == "text":
                    elem.set("fill", "white" if selected else "#203a40")
        self.active = state
        self.load_svg()
        self.item.update()
        if self.follow or state is None:
            self.frame_chart()

    def set_follow(self, enabled):
        self.follow = enabled
        self.manual_zoom = False
        self.frame_chart()

    def zoom_by(self, factor):
        scale = self.transform().m11()
        target = min(6.0, max(0.04, scale * factor))
        self.manual_zoom = True
        self.scale(target / scale, target / scale)

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta:
            self.zoom_by(1.25 ** max(-4, min(4, delta / 120)))
            event.accept()
        else:
            super().wheelEvent(event)

    def frame_chart(self):
        if self.follow and self.active:
            if not self.manual_zoom:
                self.resetTransform()
            bounds = self.renderer.boundsOnElement(self.active)
            center = self.renderer.transformForElement(self.active).map(bounds.center())
            box = self.renderer.viewBoxF()
            rect = self.item.boundingRect()
            self.centerOn(
                (center.x() - box.x()) * rect.width() / box.width(),
                (center.y() - box.y()) * rect.height() / box.height(),
            )
        elif not self.manual_zoom:
            self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.frame_chart()


class FsmPanel(QWidget):
    def __init__(self, status_type):
        super().__init__()
        self.states = {
            getattr(status_type, "FSM_" + state): (phase, state)
            for phase, states in PHASES.items()
            for state in states
        }
        layout = QVBoxLayout(self)
        self.current = QLabel()
        self.current.setObjectName("heading")
        self.detail = QLabel()
        self.detail.setObjectName("muted")
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        self.source = QComboBox()
        self.source.addItems(["Field robot", "Jetson simulation"])
        self.source.setAccessibleName("State machine source")
        self.source.hide()
        layout.addWidget(self.source, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.current)
        layout.addWidget(self.detail)
        bar = QHBoxLayout()
        layout.addLayout(bar)
        self.follow = QCheckBox("Follow state")
        self.follow.setChecked(True)
        self.follow.setToolTip("Center the reported state. Clear to see the whole flowchart.")
        bar.addWidget(self.follow)
        bar.addStretch()
        bar.addWidget(QLabel("Dashed arrows · interrupts"))
        self.chart = Flowchart()
        self.fit = QPushButton("Fit diagram")
        self.fit.clicked.connect(self.fit_diagram)
        bar.addWidget(self.fit)
        self.follow.toggled.connect(self.chart.set_follow)
        layout.addWidget(self.chart, 1)
        self.sample = None
        self.display(None, False)

    def fit_diagram(self):
        self.follow.setChecked(False)
        self.chart.manual_zoom = False
        self.chart.frame_chart()

    def display_simulation(self, code, stamp_ms):
        sample = ("simulation", code, stamp_ms)
        if self.sample == sample:
            return
        self.sample = sample
        phase, name = self.states.get(code, ("Awaiting current sample", None))
        self.current.setText(f"Simulation · {phase}")
        self.detail.setText(f"{name}  ·  {eastern(stamp_ms / 1000)} ET" if name else "")
        self.chart.highlight(name)

    def display(self, snap, connected, idle=False):
        code = snap.fsm_state if snap else 0
        standby = connected and idle and code == 0
        sample = (connected, standby, code, (snap.stamp.sec, snap.stamp.nanosec) if snap else None)
        if sample == self.sample:
            return
        self.sample = sample
        phase, name = self.states.get(code, (None, None))
        if standby:
            self.current.setText("Radio standby")
            self.detail.setText("Link ready · awaiting onboard mission state")
            self.chart.highlight("STANDBY_MODE", inferred=True)
            return
        self.current.setText(phase or "FSM unavailable")
        if snap:
            stamp = eastern(snap.stamp.sec) + " ET"
            self.detail.setText(
                f"{name or (f'Unknown state {code}' if code else 'No fresh FSM sample')}  ·  {stamp}"
            )
        else:
            self.detail.setText("Awaiting field status" if connected else "Radio disconnected")
        self.chart.highlight(name)
