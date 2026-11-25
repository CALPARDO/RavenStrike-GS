"""Read-only playback of a recorded radio bench mission. No network or transport."""

import json
import math
from pathlib import Path

from PySide6.QtCore import QElapsedTimer, QPointF, Qt, QTimer
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

        def project(x, y):
            return QPointF(
                self.width() / 2 + (x - (west + east) / 2) * scale,
                self.height() / 2 - (y - (south + north) / 2) * scale,
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
        obstacle_point(p, QPointF(self.width() - 110, 17))
        p.setPen(QColor("#563d15"))
        p.drawText(self.width() - 97, 22, "Sim obstacle")
        for xy in OBSTACLES_XY:
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
            p.setPen(QPen(QColor("#ad4c19"), 2))
            p.setBrush(QColor("#ffddbb"))
            p.drawEllipse(target, 6, 6)
            p.drawText(target + QPointF(9, -8), "FOD")
        if fixes:
            _, x, y, heading = fixes[-1]
            p.translate(project(x, y))
            p.setPen(QPen(QColor("white"), 2))
            p.setBrush(QColor("#194f61"))
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
