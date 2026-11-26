"""Read-only playback of a recorded radio bench mission. No network or transport."""

import json
import math
import re
from pathlib import Path

from PySide6.QtCore import QElapsedTimer, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from demo_site import OBSTACLES_XY
from runway_map import obstacle_point


class GroundTrack(QWidget):
    def __init__(self, trace):
        super().__init__()
        self.trace = trace
        self.elapsed = 0
        self.barriers = []
        self.targets = []
        self.obstacles = OBSTACLES_XY
        self.show_robot = True
        self.historical = False
        self.setMinimumSize(360, 200)
        points = [row[1:3] for row in trace["fixes"]] + [trace["target"], *OBSTACLES_XY]
        self.bounds = [
            min(x for x, y in points),
            max(x for x, y in points),
            min(y for x, y in points),
            max(y for x, y in points),
        ]

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#f1f6f5"))
        west, east, south, north = self.bounds
        scale = min(
            (self.width() - 110) / max(1, east - west), (self.height() - 80) / max(1, north - south)
        )

        center_x, center_y = (west + east) / 2, (south + north) / 2

        def project(x, y):
            return QPointF(
                self.width() / 2 + (x - center_x) * scale,
                self.height() / 2 - (y - center_y) * scale,
            )

        origin = project(0, 0)
        p.setPen(QPen(QColor("#dbe7e4"), 1))
        for x in range(math.floor(west) - 4, math.ceil(east) + 5):
            px = round(project(x, 0).x())
            p.drawLine(px, 0, px, self.height())
        for y in range(math.floor(south) - 4, math.ceil(north) + 5):
            py = round(project(0, y).y())
            p.drawLine(0, py, self.width(), py)
        p.setPen(QColor("#436369"))
        p.drawText(14, 22, "N ↑    ·    1 m grid")
        if self.obstacles:
            obstacle_point(p, QPointF(self.width() - 110, 17))
            p.setPen(QColor("#563d15"))
            p.drawText(self.width() - 97, 22, "Sim obstacle")
        p.setPen(QPen(QColor("#b5986b"), 1))
        p.setBrush(QColor("#ded0b8"))
        for xmin, ymin, xmax, ymax in self.barriers:
            p.drawRect(QRectF(project(xmin, ymax), project(xmax, ymin)))
        for i, xy in enumerate(self.targets, 1):
            point = project(*xy)
            p.setPen(QPen(QColor("#576b74"), 1))
            p.setBrush(QColor("#ffffff"))
            p.drawEllipse(point, 4, 4)
            p.drawText(point + QPointF(7, 13), f"F{i}")
        for xy in self.obstacles:
            obstacle_point(p, project(*xy))
        p.setBrush(Qt.BrushStyle.NoBrush)
        fixes = [f for f in self.trace["fixes"] if f[0] <= self.elapsed]
        p.setPen(QPen(QColor("#148779"), 3))
        p.drawPolyline(QPolygonF([project(x, y) for _, x, y, _ in fixes]))
        p.setBrush(QColor("white"))
        p.drawEllipse(origin, 5, 5)
        p.setPen(QColor("#25484e"))
        p.drawText(origin + QPointF(-10, 23), "Base")
        if self.elapsed >= self.trace["target_time"]:
            target = project(*self.trace["target"])
            p.setPen(QPen(QColor("#667982" if self.historical else "#ad4c19"), 2))
            p.setBrush(QColor("#e3e9eb" if self.historical else "#ffddbb"))
            p.drawEllipse(target, 6, 6)
            p.drawText(target + QPointF(9, -8), "Last FOD" if self.historical else "FOD")
        if fixes and self.show_robot:
            _, x, y, heading = fixes[-1]
            p.translate(project(x, y))
            p.setPen(QPen(QColor("white"), 2))
            p.setBrush(QColor("#667982" if self.historical else "#194f61"))
            if heading is None:
                p.drawEllipse(QPointF(), 7, 7)
            else:
                p.rotate(heading)
                p.drawPolygon(
                    QPolygonF([QPointF(0, -12), QPointF(8, 9), QPointF(0, 5), QPointF(-8, 9)])
                )


class DemoReplay(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        trace = json.loads((Path(__file__).resolve().parent / "demo/mission.json").read_text())
        self.setWindowTitle("RavenStrike | Demo replay")
        self.resize(640, 430)
        layout = QVBoxLayout(self)
        title = QLabel("Recorded mission")
        title.setObjectName("heading")
        layout.addWidget(title)
        layout.addWidget(QLabel("Radio bench recording · simulated Lynx and debris"))
        self.plot = GroundTrack(trace)
        layout.addWidget(self.plot, 1)
        self.phase = QLabel()
        self.phase.setObjectName("heading")
        layout.addWidget(self.phase)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setAccessibleName("Replay time")
        self.slider.setRange(0, round(trace["duration"] * 1000))
        layout.addWidget(self.slider)
        row = QHBoxLayout()
        layout.addLayout(row)
        self.play = QPushButton("Pause")
        row.addWidget(self.play)
        self.time = QLabel()
        row.addStretch()
        row.addWidget(self.time)
        self.clock = QElapsedTimer()
        self.timer = QTimer(self)
        self.timer.setInterval(80)
        self.timer.timeout.connect(self.advance)
        self.slider.valueChanged.connect(self.show_frame)
        self.slider.sliderPressed.connect(self.pause)
        self.play.clicked.connect(self.toggle)
        self.show_frame(0)
        self.resume()

    def show_frame(self, ms):
        self.clock.start()
        self.plot.elapsed = ms / 1000
        self.plot.update()
        self.phase.setText(
            next(label for t, label in reversed(self.plot.trace["events"]) if t <= ms / 1000)
        )
        self.time.setText(f"{ms // 1000} s  /  {self.slider.maximum() // 1000} s")
        if ms == self.slider.maximum():
            self.pause()
            self.play.setText("Replay")

    def pause(self):
        self.timer.stop()
        self.play.setText("Play")

    def resume(self):
        if self.slider.value() == self.slider.maximum():
            self.slider.setValue(0)
        self.clock.start()
        self.timer.start()
        self.play.setText("Pause")

    def advance(self):
        self.slider.setValue(self.slider.value() + self.clock.elapsed())

    def toggle(self):
        if self.timer.isActive():
            self.pause()
        else:
            self.resume()


class SimulationView(QWidget):
    """Read-only samples from the currently supervised Jetson job, in local metres."""

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        self.phase = QLabel()
        self.phase.setObjectName("heading")
        self.detail = QLabel()
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.phase)
        layout.addWidget(self.detail)
        self.phase.setTextFormat(Qt.TextFormat.PlainText)
        self.plot = GroundTrack({"fixes": [], "target": (0, 0), "target_time": math.inf})
        layout.addWidget(self.plot, 1)
        legend = QHBoxLayout()
        for text, color in (("━  Traveled", "#14756a"), ("●  Active FOD", "#98521f"),
                            ("○  Assigned FOD", "#53636d"), ("■  Obstacle", "#896e46")):
            item = QLabel(text)
            item.setStyleSheet(f"color: {color}; font-weight: 600")
            legend.addWidget(item)
        legend.addStretch()
        layout.addLayout(legend)
        self.reset()

    def reset(self):
        self.job = ""
        self.seed = None
        self.plot.obstacles = ()
        self.plot.barriers = []
        self.plot.targets = []
        self.phase.setText("Simulation map")
        self.detail.setText("Local coordinates in metres")
        self.stamp = self.code = self.queued = self.queue_stamp = 0
        self.queue = None
        self.plot.trace["fixes"].clear()
        self.plot.trace["target_time"] = math.inf
        self.plot.bounds = [-8, 8, -8, 8]
        self.plot.show_robot = False
        self.plot.update()

    def accept_scene(self, detail):
        if self.seed is not None or len(detail) > 263 or not detail.startswith("scene1:"):
            return False
        try:
            seed, barriers, targets = json.loads(detail[7:])
            if type(seed) is not int or not 0 <= seed < 2**32:
                return False
            for rows, width in ((barriers, 4), (targets, 2)):
                if (not isinstance(rows, list) or not 3 <= len(rows) <= 5
                        or any(not isinstance(r, list) or len(r) != width
                               or any(type(v) is not int or abs(v) > 800 for v in r) for r in rows)):
                    return False
            if any(w >= e or s >= n for w, s, e, n in barriers):
                return False
        except (ValueError, TypeError, RecursionError):
            return False
        self.seed = seed
        self.plot.barriers = [[v / 100 for v in row] for row in barriers]
        self.plot.targets = [[v / 100 for v in row] for row in targets]
        self.plot.bounds = [-8, 8, -8, 8]
        self.plot.update()
        return True

    @staticmethod
    def parse_sample(detail, stamp, previous=0):
        if len(detail) > 325:
            return None
        detail = detail.partition("|q1:")[0]
        if not detail.startswith("sim1:") or len(detail) > 133:
            return None
        try:
            row = json.loads(detail[5:])
        except (ValueError, RecursionError):
            return None
        if (not isinstance(row, list) or len(row) not in (6, 8)
                or any(type(v) is not int for v in row)
                or not previous < row[0]
                or (stamp is not None and not 0 <= stamp // 1_000_000 - row[0] <= 2500) or not 0 <= row[1] <= 255
                or not 0 <= row[4] < 360 or not 0 <= row[5] <= 32
                or any(abs(v) > 10000 for v in row[2:4] + row[6:])):
            return None
        return row

    def accept(self, msg, stamp):
        row = self.parse_sample(msg.detail, stamp, self.stamp)
        if row is None:
            return False
        self.stamp, self.code, x, y, heading, self.queued, *target = row
        _, separator, metadata = msg.detail.partition("|q1:")
        if separator:
            self.queue = self.parse_queue(metadata, self.queued)
            self.queue_stamp = self.stamp if self.queue is not None else 0
        elif self.queue is not None and len(self.queue) != self.queued:
            self.queue = None
            self.queue_stamp = 0
        x, y = x / 100, y / 100
        self.plot.elapsed = self.stamp
        track = self.plot.trace
        track["fixes"].append((self.stamp, x, y, heading))
        del track["fixes"][:-2048]
        points = [(x, y)]
        track["target_time"] = math.inf
        if target:
            track["target"] = (target[0]/100, target[1]/100)
            track["target_time"] = self.stamp
            points.append(track["target"])
        for x, y in points:
            w, e, s, n = self.plot.bounds
            self.plot.bounds = [min(w, x-.5), max(e, x+.5), min(s, y-.5), max(n, y+.5)]
        self.plot.update()
        return True

    @staticmethod
    def parse_queue(metadata, count):
        if len(metadata) > 188:
            return None
        try:
            selected, ids = json.loads(metadata)
        except (ValueError, TypeError, RecursionError):
            return None
        if (not isinstance(ids, list) or len(ids) != count or count > 8
                or type(selected) is not int or not -1 <= selected < len(ids)
                or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", name)
                       for name in ids) or len(set(ids)) != count):
            return None
        return tuple((name, i == selected) for i, name in enumerate(ids))
