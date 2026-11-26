"""State the GS has observed, never a local mission FSM."""

import base64
import gzip
import hashlib
import json
import time
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from display import eastern


def contents(value):
    if is_dataclass(value):
        return {
            f.name: contents(getattr(value, f.name))
            for f in fields(value)
            if f.name != "__msgtype__"
        }
    if hasattr(value, "tobytes"):
        return {"bytes": value.nbytes}
    return value


class Journal:
    def __init__(self, root):
        self.recording = None
        self.start = time.monotonic()
        self.folder = Path(root) / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8])
        (self.folder / "evidence").mkdir(parents=True)
        self.console = (self.folder / "console.log").open("x", buffering=1, encoding="utf-8")
        self.events = (self.folder / "events.jsonl").open("x", buffering=1, encoding="utf-8")

    def log(self, event, detail, *, received=None, **data):
        utc_ns = received or time.time_ns()
        when = datetime.fromtimestamp(utc_ns / 1e9, UTC).isoformat(
            timespec="milliseconds"
        )
        entry = dict(
            utc=when, utc_ns=utc_ns,
            elapsed_s=round(time.monotonic() - self.start, 3),
            event=event,
            detail=detail,
            **data,
        )
        self.capture(entry)
        self.events.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.console.write(f"{when} {event}: {detail}\n")

    def start_recording(self):
        if self.recording:
            raise ValueError("Recording already active")
        self.record_path = self.folder / ("radio-" + datetime.now(UTC).strftime("%H%M%S-%f") + ".jsonl.gz")
        self.recording = gzip.open(self.record_path, "xt", encoding="utf-8", compresslevel=1)  # noqa: SIM115 - spans Record and Stop
        self.record_start = time.monotonic_ns()
        self.log("record_start", "GS radio capture started")

    def capture(self, row):
        if self.recording:
            row = dict(row, elapsed_s=(time.monotonic_ns() - self.record_start) / 1e9)
            self.recording.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n")

    def serial_data(self, chunks, missed):
        for ns, direction, raw in chunks:
            self.capture({"event": "serial", "direction": direction, "utc_ns": ns,
                              "utc": datetime.fromtimestamp(ns / 1e9, UTC).isoformat(),
                              "detail": f"{direction} · {len(raw)} bytes",
                              "data": base64.b64encode(raw).decode("ascii")})
        if missed:
            self.log("capture_gap", f"{missed} serial chunks exceeded the recorder buffer")

    def stop_recording(self):
        if self.recording:
            try:
                self.log("record_stop", "GS radio capture stopped")
            finally:
                self.recording.close()
                self.recording = None

    def close(self):
        self.stop_recording()
        self.events.close()
        self.console.close()


class Station:
    def __init__(self, codec, radio, journal):
        self.codec, self.radio, self.journal = codec, radio, journal
        self.session = self.active = self.state = self.note = ""
        self.callsign = self.area = ""
        self.home = ()
        self.queue = []
        self.scanned = self.scanning_requested = self.abort_requested = self.review = False
        self.cancel_requested_at = None
        self.last_status = -1
        self.records = {}
        self.image_times = {}
        self.latest = {}
        self.decisions = set()
        self.targets = {}
        self.report_times = {}
        self.final_reports = set()
        self.timeline = {}
        self.pending_tasks = {}
        self.accepted_tasks = set()
        self.selected_fod = ""
        self.selection_stamp = -1

    def record(self, mission, text, when=None):
        clock = eastern((when or time.time_ns()) / 1e9)
        self.timeline.setdefault(mission, []).append(f"{clock}   {text}")

    def task_ack(self, topic, wire_id, when):
        if topic == 7 and (mission := self.pending_tasks.pop(wire_id, None)):
            self.record(mission, "Radio ACK", when)

    def accepted(self, mission, when):
        if mission not in self.accepted_tasks and mission not in self.final_reports:
            self.accepted_tasks.add(mission)
            self.record(mission, "UGV accepted", when)

    def observe_selection(self, mission, stamp, received):
        # Progress reports and session snapshots travel on separate radio topics.
        if stamp < self.selection_stamp:
            return
        self.selection_stamp = stamp
        if mission != self.selected_fod:
            self.selected_fod = mission
            if mission and mission not in self.final_reports:
                self.record(mission, "Active", received)

    @property
    def operating(self):
        return self.state in ("standby", "working", "returning") and not self.abort_requested and self.cancel_requested_at is None

    def send(self, topic, msg, detail):
        self.journal.log("operator_request", detail, topic=topic, message=contents(msg))
        wire_id = self.radio.send(topic, msg)
        self.journal.log("sent", detail, wire_id=wire_id, topic=topic, message=contents(msg))

        return wire_id

    def start(self, callsign, area, home):
        if self.session and self.state not in (
            "completed",
            "canceled",
            "halted",
            "rejected",
        ):
            raise ValueError("A session is already pending or active.")
        if not all(0 < len(s.encode()) <= 128 for s in (callsign, area)):
            raise ValueError("Enter a callsign and area (up to 128 bytes each).")
        sid = uuid4().hex
        msg = self.codec.control(sid, callsign, area, home, "start")
        self.send(5, msg, "Start supervised session " + sid)
        self.__init__(self.codec, self.radio, self.journal)
        self.session, self.area, self.callsign, self.home = sid, area, callsign, home
        self.state = "awaiting acceptance"

    def retry_start(self):
        if self.state != "awaiting acceptance":
            raise ValueError("Session is no longer pending.")
        self.send(
            5,
            self.codec.control(self.session, self.callsign, self.area, self.home, "start"),
            "Operator retried start for " + self.session,
        )

    @property
    def can_halt(self):
        return (bool(self.session) and self.state in ("working", "returning", "standby", "paused")
                and (self.cancel_requested_at is None or time.monotonic() - self.cancel_requested_at >= 3))

    def command(self, command):
        if command in ("cancel", "stop"):
            allowed = self.can_halt
        elif command == "continue":
            allowed = self.state == "paused" and self.cancel_requested_at is None
        elif command == "pause":
            allowed = self.operating
        else:
            allowed = (self.operating or self.state == "paused") and command in ("scan_complete", "abort")
        if not allowed:
            raise ValueError("Session cannot accept this command.")
        if command == "scan_complete" and (self.scanned or self.scanning_requested):
            raise ValueError("Scanning completion already sent.")
        self.send(
            5,
            self.codec.control(self.session, self.callsign, self.area, self.home, command),
            command,
        )
        if command == "scan_complete":
            self.scanning_requested = True
        elif command in ("cancel", "stop"):
            self.cancel_requested_at = time.monotonic()
        elif command == "abort":
            self.abort_requested = True

    def add(self, lat, lon, alt):
        if not self.operating or self.scanned or self.scanning_requested:
            raise ValueError("Start an accepted, open scanning session first.")
        mission = "fod-" + uuid4().hex[:12]
        msg = self.codec.gps(self.session, mission, self.callsign, lat, lon, alt)
        wire_id = self.send(7, msg, "Submitted " + mission)
        self.pending_tasks[wire_id] = mission
        self.record(mission, "Sent to radio queue")
        self.targets[mission] = "awaiting UGV"
        return mission

    def review_token(self):
        token = self.latest.get((self.active, "review"))
        if self.operating and self.review and token and token not in self.decisions:
            return token
        return None

    def decide(self, token, decision):
        if not token or token != self.review_token():
            raise ValueError("The displayed review is no longer current.")
        kind = self.records[token][0]
        if decision not in ("approve", "retry") or (
            kind == "cycle_complete" and decision == "retry"
        ):
            raise ValueError("This review does not allow that decision.")
        msg = self.codec.types["OperatorDecision"](
            self.codec.stamp(), self.session, self.active, token, decision
        )
        self.send(6, msg, decision + " " + self.active + " evidence " + token)
        self.decisions.add(token)
        action = (
            "retry" if decision == "retry" else "continue" if kind != "after" else "approve removal"
        )
        self.record(self.active, f"Operator requested {action}")

    def receive(self, topic, msg, received=None):
        detail = getattr(msg, "notes", "") or getattr(msg, "kind", "")
        summary = " ".join(
            str(x)
            for x in (
                getattr(msg, "mission_id", ""),
                getattr(msg, "state", ""),
                getattr(msg, "status", ""),
                detail,
            )
            if x
        )
        self.journal.log("received", summary, received=received, topic=topic, message=contents(msg))
        stamp = msg.stamp.sec * 10**9 + msg.stamp.nanosec
        if topic == 2:
            if msg.assigned_callsign == self.callsign and (
                msg.mission_id in self.targets
                or msg.mission_id in self.accepted_tasks
                or msg.mission_id == self.active
            ):
                if msg.mission_id in self.final_reports or stamp < self.report_times.get(
                    msg.mission_id, -1
                ):
                    return
                self.report_times[msg.mission_id] = stamp
                if msg.status == "in_progress":
                    self.accepted(msg.mission_id, received)
                    if msg.notes in ("nearest target selected", "closer onboard target selected"):
                        self.observe_selection(msg.mission_id, stamp, received)
                if msg.status in ("completed", "aborted", "blocked"):
                    self.record(
                        msg.mission_id,
                        f"{msg.status.capitalize()} · {msg.notes}".rstrip(" ·"),
                        received,
                    )
                    self.final_reports.add(msg.mission_id)
                self.targets[msg.mission_id] = (msg.status + " " + msg.notes).strip()
            return
        if not self.session or msg.session_id != self.session:
            return
        if topic == 8:
            if stamp < self.last_status or self.state in (
                "completed",
                "halted",
                "canceled",
            ):
                return
            previous_active, previous_review = self.active, self.review
            self.last_status = stamp
            self.state, self.note, self.active = (
                msg.state,
                msg.notes,
                msg.active_mission_id,
            )
            self.queue = list(msg.queued_mission_ids)
            self.scanned = msg.scanning_complete
            self.review = self.note == "OK_TO_MOVE_FORWARD"
            for mission in self.queue:
                self.accepted(mission, received)
            active_fod = self.active if self.state == "working" else ""
            if active_fod:
                self.accepted(active_fod, received)
            self.observe_selection(active_fod, stamp, received)
            if (
                active_fod
                and active_fod not in self.final_reports
                and self.review
                and (not previous_review or self.active != previous_active)
            ):
                self.record(active_fod, "Awaiting operator review", received)
        elif topic == 19:
            token, kind = msg.evidence_id, msg.kind
            if (
                not token
                or token in self.records
                or kind not in ("before", "after", "not_found", "cycle_complete")
            ):
                return
            data = bytes(msg.preview.data)
            path = None
            if kind == "cycle_complete":
                if data:
                    raise ValueError("Unexpected image on cycle_complete.")
            else:
                digest = hashlib.sha256(data).hexdigest()
                if (
                    not data
                    or len(data) > 12288
                    or digest != msg.preview_sha256
                    or msg.preview.format.lower() != "jpeg"
                ):
                    raise ValueError("Invalid JPEG preview or checksum.")
                path = self.journal.folder / "evidence" / (digest + ".jpg")
                path.write_bytes(data)
            self.image_times[token] = msg.image_stamp.sec * 10**9 + msg.image_stamp.nanosec
            self.records[token] = (kind, path, stamp)
            key = (msg.mission_id, "before" if kind == "before" else "review")
            prior = self.latest.get(key)
            if prior is None or self.records[prior][2] < stamp:
                self.latest[key] = token
