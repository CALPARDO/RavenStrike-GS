"""Read-only GS journal playback and evidence export."""

import gzip
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
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from demo_replay import SimulationView
from demo_site import demo_obstacles
from display import TargetNames, eastern
from map_tracks import MAX_FIXES, PLAN_TTL_NS, MapTracks
from runway_map import RunwayMap


def export_evidence(folder, destination):
    files = [folder / "events.jsonl", folder / "console.log"]
    files.extend(sorted(folder.glob("radio-*.jsonl.gz")))
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
        self.states = parent.fsm_panel.states if parent and hasattr(parent, "fsm_panel") else {}
        self.sim = SimulationView()
        self.mode_times, self.modes = [0], [""]
        self.simulations = {}
        end = 0
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as source:
            for i, line in enumerate(source, 1):
                try:
                    row = json.loads(line)
                    ms = round(float(row["elapsed_s"]) * 1000)
                    if not end <= ms < 2**31:
                        raise ValueError("invalid elapsed time")
                    end = ms
                    event = row["event"]
                    if event == "serial" and row.get("direction") != "tx_failed":
                        continue
                    if event in ("sim_preview", "sim_layout"):
                        job = row["job_id"]
                        timeline = self.simulations.setdefault(job, {"times": [], "rows": [], "fixes": [], "scene": None})
                        if job != self.modes[-1]:
                            self.mode_times.append(ms)
                            self.modes.append(job)
                        if event == "sim_preview":
                            sample = self.sim.parse_sample(row["detail"], None)
                            if sample is None:
                                raise ValueError("Invalid simulation sample")
                            timeline["times"].append(ms)
                            timeline["rows"].append(sample)
                            timeline["fixes"].append((ms, sample[2]/100, sample[3]/100, sample[4]))
                        else:
                            timeline["scene"] = (ms, row["detail"])
                    if self.modes[-1] and (event == "disconnect" or (event == "workspace" and row["detail"] == "Field")):
                        self.mode_times.append(ms)
                        self.modes.append("")
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
                    detail = names.in_text(row["detail"])
                    if event == "sim_preview":
                        phase = self.states.get(sample[1], (f"State {sample[1]}", ""))[0]
                        detail = f"{phase} · ({sample[2]/100:.2f}, {sample[3]/100:.2f}) m · {sample[4]}° · {sample[5]} queued"
                    elif event == "sim_layout":
                        detail = "Received the seeded map and assigned targets"
                    elif event == "app_status":
                        detail = f"Jetson job {row['state']}"
                    elif event in ("ack", "nack"):
                        fields = row.get("fields", {})
                        detail = f"Topic {fields.get('topic', '—')} · message {fields.get('wire_id', '—')}"
                    elif event == "location":
                        detail = f"{lat:.6f}°, {lon:.6f}°" + (f" · {heading:.0f}° true" if heading is not None else "")
                    self.events.append(f"{clock} ET   {event.replace('_', ' ').capitalize()}   {detail}")
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
        self.layers = self.map.layer_switch()
        toolbar.addWidget(self.layers)
        layout.addLayout(toolbar)
        self.map.target_name = names
        split = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(split, 1)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.views = QStackedWidget()
        self.views.addWidget(self.map)
        self.views.addWidget(self.sim)
        left_layout.addWidget(self.views, 1)
        split.addWidget(left)
        key = QHBoxLayout()
        for text, name in (("┅  Nav2 plan", "planKey"), ("━  GPS track", "trackKey")):
            caption = QLabel(text)
            caption.setObjectName(name)
            key.addWidget(caption)
        self.obstacle_key = QLabel("◆  Sim obstacle")
        self.obstacle_key.setObjectName("obstacleKey")
        key.addWidget(self.obstacle_key)
        key.addStretch()
        self.legend = QWidget()
        self.legend.setLayout(key)
        left_layout.addWidget(self.legend)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        right = QWidget()
        side = QVBoxLayout(right)
        self.current_event = QLabel()
        self.current_event.setWordWrap(True)
        self.current_event.setTextFormat(Qt.TextFormat.PlainText)
        side.addWidget(QLabel("Current event"))
        side.addWidget(self.current_event)
        side.addWidget(self.log, 1)
        split.addWidget(right)
        split.setSizes([580, 300])
        row = QHBoxLayout()
        layout.addLayout(row)
        self.play = QPushButton("Play")
        row.addWidget(self.play)
        for seconds in (-5, -1, 1, 5):
            button = QPushButton(f"{seconds:+d} s")
            button.clicked.connect(lambda _, delta=seconds: self.seek(delta))
            row.addWidget(button)
        self.speed = QComboBox()
        for rate in (.5, 1, 2, 5, 20):
            self.speed.addItem(f"{rate:g}×", rate)
        self.speed.setCurrentIndex(1)
        row.addWidget(self.speed)
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
        job = self.modes[bisect_right(self.mode_times, ms) - 1]
        self.views.setCurrentWidget(self.sim if job else self.map)
        self.layers.setVisible(not job)
        self.legend.setVisible(not job)
        if job:
            timeline = self.simulations[job]
            i = bisect_right(timeline["times"], ms)
            scene = timeline["scene"]
            if self.sim.job != job or (self.sim.seed is not None and scene and ms < scene[0]):
                self.sim.reset()
                self.sim.job = job
            if scene and ms >= scene[0] and self.sim.seed is None:
                self.sim.accept_scene(scene[1])
            plot = self.sim.plot
            plot.trace["fixes"] = timeline["fixes"][max(0, i-2048):i]
            plot.trace["target_time"] = math.inf
            plot.elapsed = ms
            plot.show_robot = bool(i and ms - timeline["times"][i-1] <= 3000)
            self.sim.detail.setText("Recorded simulation · local metres")
            if i:
                row = timeline["rows"][i-1]
                phase = self.states.get(row[1], (f"State {row[1]}", ""))[0]
                self.sim.detail.setText(f"{phase} · {row[5]} queued · local metres")
                if len(row) == 8:
                    plot.trace["target"] = (row[6]/100, row[7]/100)
                    plot.trace["target_time"] = timeline["times"][i-1]
            plot.update()
        i = bisect_right(self.event_times, ms)
        self.current_event.setText(self.events[i-1] if i else "Waiting for first event")
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

    def seek(self, seconds):
        self.pause()
        self.slider.setValue(self.slider.value() + seconds * 1000)

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
        self.slider.setValue(self.slider.value() + round(self.clock.restart() * self.speed.currentData()))
