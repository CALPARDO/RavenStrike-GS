"""Read-only GS journal playback and evidence export."""

import json
import math
import os
import tempfile
from bisect import bisect_right
from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from PySide6.QtCore import QElapsedTimer, Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QVBoxLayout,
)

from demo_site import demo_obstacles
from display import TargetNames, eastern
from map_tracks import MAX_FIXES, PLAN_TTL_NS, MapTracks
from runway_map import RunwayMap


def export_evidence(folder, destination):
    files = [folder / "events.jsonl", folder / "console.log"]
    files.extend(sorted((folder / "evidence").glob("*.jpg")))
    if any(
        not f.is_file() or f.is_symlink() or folder.resolve() not in f.resolve().parents
        for f in files
    ):
        raise ValueError("Missing or invalid session evidence.")
    if destination.suffix.lower() != ".zip":
        raise ValueError("Choose a .zip file.")
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temp:
        name = temp.name
    try:
        with ZipFile(name, "w", ZIP_DEFLATED) as bundle:
            for path in files:
                bundle.write(path, path.relative_to(folder))
        os.replace(name, destination)
    finally:
        Path(name).unlink(missing_ok=True)


class JournalReplay(QDialog):
    """Reconstruct only information actually recorded on the ground station."""

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Session recording · " + path.parent.name)
        self.resize(880, 640)
        self.fixes, self.events, self.targets = [], [], []
        self.times, self.event_times, self.target_times = [], [], []
        self.plan_times, self.plans, self.fix_stamps = [], [], []
        self.clear_times, self.clear_indices = [0], [0]
        progress = MapTracks()
        names = TargetNames()
        self.target_resets, self.target_indices = [0], [0]
        self.obstacle_scenes = [()]
        self.progress_times, self.progress = [], []
        end = 0
        with path.open(encoding="utf-8") as source:
            for i, line in enumerate(source, 1):
                try:
                    row = json.loads(line)
                    ms = round(float(row["elapsed_s"]) * 1000)
                    if not end <= ms < 2**31:
                        raise ValueError("invalid elapsed time")
                    end = ms
                    event = row["event"]
                    if event == "location":
                        lat, lon, heading = row["pose"]
                        if not (
                            -90 <= lat <= 90
                            and -180 <= lon <= 180
                            and (heading is None or math.isfinite(heading))
                        ):
                            raise ValueError("invalid recorded fix")
                        self.times.append(ms)
                        self.fix_stamps.append(int(row.get("source_ns", ms * 1_000_000)))
                        self.fixes.append((lat, lon, heading))
                        progress.fix(self.fix_stamps[-1], (lat, lon))
                        self.progress_times.append(ms)
                        self.progress.append((progress.segment, progress.fraction))
                    else:
                        start = (
                            event == "sent"
                            and row.get("topic") == 5
                            and row["message"].get("command") == "start"
                        )
                        if event == "disconnect" or start:
                            msg = row["message"] if start else {}
                            self.obstacle_scenes.append(
                                demo_obstacles(
                                    msg.get("area_of_interest", ""),
                                    msg.get("assigned_callsign", ""),
                                )
                            )
                            self.target_resets.append(ms)
                            self.target_indices.append(len(self.targets))
                        if event in ("track_clear", "disconnect") or start:
                            self.clear_times.append(ms)
                            self.clear_indices.append(len(self.fixes))
                            progress.fixes.clear()
                        if event in ("plan", "plan_lost", "disconnect") or start:
                            points = row.get("points", [])
                            if (
                                len(points) == 1
                                or len(points) > 12
                                or any(
                                    not (-90 <= lat <= 90 and -180 <= lon <= 180)
                                    for lat, lon in points
                                )
                            ):
                                raise ValueError("Invalid planned waypoints")
                            if (
                                self.plan_times
                                and (ms - self.plan_times[-1]) * 1_000_000 > PLAN_TTL_NS
                            ):
                                progress.plan = ()
                            self.plan_times.append(ms)
                            if event != "plan":
                                progress = MapTracks()
                            progress.replace(
                                int(row.get("source_ns", ms * 1_000_000)),
                                tuple(tuple(p) for p in points),
                            )
                            self.plans.append(progress.plan)
                            self.progress_times.append(ms)
                            self.progress.append((progress.segment, progress.fraction))
                        if event == "disconnect":
                            self.fix_stamps.append(ms * 1_000_000)
                            self.times.append(ms)
                            self.fixes.append(None)
                        if event == "sent" and row.get("topic") == 7:
                            msg = row["message"]
                            point = msg["location"]
                            self.targets.append(
                                (
                                    msg["mission_id"],
                                    (
                                        float(point["latitude"]),
                                        float(point["longitude"]),
                                    ),
                                )
                            )
                            self.target_times.append(ms)
                        clock = eastern(datetime.fromisoformat(row["utc"]).timestamp())
                        self.events.append(f"{clock} ET   {event}   {names.in_text(row['detail'])}")
                        self.event_times.append(ms)
                except (TypeError, KeyError, ValueError, OverflowError) as err:
                    raise ValueError(f"Invalid GS recording at line {i}: {err}") from err
        if not self.fixes and not self.events:
            raise ValueError("Empty session recording.")
        layout = QVBoxLayout(self)
        title = QLabel("Recording   /   " + path.parent.name)
        title.setObjectName("heading")
        toolbar = QHBoxLayout()
        toolbar.addWidget(title, 1)
        self.map = RunwayMap()
        self.finished.connect(self.map.stop_tiles)
        toolbar.addWidget(self.map.layer_switch())
        layout.addLayout(toolbar)
        self.map.target_name = names
        layout.addWidget(self.map, 2)
        key = QHBoxLayout()
        for text, name in (("┅  Nav2 plan", "planKey"), ("━  GPS track", "trackKey")):
            caption = QLabel(text)
            caption.setObjectName(name)
            key.addWidget(caption)
        self.obstacle_key = QLabel("◆  Sim obstacle")
        self.obstacle_key.setObjectName("obstacleKey")
        key.addWidget(self.obstacle_key)
        key.addStretch()
        layout.addLayout(key)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log, 1)
        row = QHBoxLayout()
        layout.addLayout(row)
        self.play = QPushButton("Play")
        row.addWidget(self.play)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, end)
        row.addWidget(self.slider, 1)
        self.elapsed = QLabel()
        row.addWidget(self.elapsed)
        self.clock = QElapsedTimer()
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.advance)
        self.slider.valueChanged.connect(self.frame)
        self.slider.sliderPressed.connect(self.pause)
        self.play.clicked.connect(self.toggle)
        self.frame(0)

    def frame(self, ms):
        fix = bisect_right(self.times, ms) - 1
        pose = self.fixes[fix] if fix >= 0 else None
        self.map.robot = (
            pose if pose and abs(pose[0]) <= 85.0511 and ms - self.times[fix] <= 3000 else None
        )
        self.map.last_known = pose[:2] if pose and abs(pose[0]) <= 85.0511 else None
        route = bisect_right(self.plan_times, ms) - 1
        self.map.tracks.plan = (
            self.plans[route]
            if route >= 0 and (ms - self.plan_times[route]) * 1_000_000 <= PLAN_TTL_NS
            else ()
        )
        index = bisect_right(self.progress_times, ms) - 1
        self.map.tracks.segment, self.map.tracks.fraction = (
            self.progress[index] if index >= 0 else (0, 0.0)
        )
        first = max(
            self.clear_indices[bisect_right(self.clear_times, ms) - 1],
            fix + 1 - MAX_FIXES,
        )
        self.map.tracks.fixes.clear()
        self.map.tracks.fixes.extend(
            (self.fix_stamps[i], self.fixes[i][:2]) for i in range(first, fix + 1) if self.fixes[i]
        )
        scene = bisect_right(self.target_resets, ms) - 1
        first_target = self.target_indices[scene]
        self.map.obstacles = self.obstacle_scenes[scene]
        self.obstacle_key.setVisible(bool(self.map.obstacles))
        self.map.targets = dict(self.targets[first_target : bisect_right(self.target_times, ms)])
        if self.map.initial_view:
            self.map.fit_targets()
        self.map.update()
        i = bisect_right(self.event_times, ms)
        text = "\n".join(self.events[max(0, i - 35) : i])
        if self.log.toPlainText() != text:
            self.log.setPlainText(text)
            self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())
        self.elapsed.setText(f"{ms / 1000:.1f} / {self.slider.maximum() / 1000:.1f} s")
        if ms == self.slider.maximum():
            self.pause()

    def pause(self):
        self.timer.stop()
        self.play.setText("Play")

    def toggle(self):
        if self.timer.isActive():
            self.pause()
        else:
            if self.slider.value() == self.slider.maximum():
                self.slider.setValue(0)
            self.clock.start()
            self.timer.start()
            self.play.setText("Pause")

    def advance(self):
        self.slider.setValue(self.slider.value() + self.clock.restart())
