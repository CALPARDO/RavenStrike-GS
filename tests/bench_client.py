"""Uses the actual field UI handlers and serial worker in a hardware-isolated bench."""

import json
import os
import signal
import socket
import sys
import time
from itertools import pairwise
from pathlib import Path

from PySide6.QtCore import QPoint, QTimer
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QApplication

from gs import Window

assert {name for _, name in socket.if_nameindex()} == {"lo"}
assert os.environ.get("QT_QPA_PLATFORM") == "offscreen"
app = QApplication([])
preview = os.environ.get("GS_BENCH_PREVIEW") == "1"
if preview:
    app.setApplicationName("RavenStrike-GS")
w = Window()
if preview:
    w.resize(1366, 768)
w.show()
w.port.setCurrentText("/dev/ttyUSB0")
w.connect_radio()


def console_frame():
    frame = w.grab()
    # Qt renders a native modal in a separate window, including on the offscreen platform.
    if w.review_page.isVisible():
        painter = QPainter(frame)
        painter.drawPixmap(
            w.mapFromGlobal(w.review_page.mapToGlobal(QPoint())), w.review_page.grab()
        )
        painter.end()
    return frame


if preview:
    frames = w.journal.folder / "ui-frames"
    frames.mkdir()

    def capture():
        path = frames / f"{time.monotonic_ns()}.jpg"
        console_frame().save(str(path), "JPG", 85)

    timer = QTimer(w)
    timer.setInterval(250)
    timer.timeout.connect(capture)
    timer.start()


def wait(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.025)
    raise AssertionError(w.log.toPlainText())


try:
    wait(lambda: w.start.isEnabled())
    wait(lambda: w.runway.robot is not None and w.runway.robot[2] is not None)
    assert w.locate.isEnabled() and not w.station.session
    if len(sys.argv) != 4:
        assert w.runway.robot == (40.0, -83.0, 180.0)
    print(
        "PASS: live position and true heading over radio before session",
        w.runway.robot,
        flush=True,
    )
    if len(sys.argv) == 4:
        if preview:
            wait(
                lambda: (
                    w.app_fresh()
                    and w.app_snapshot.mission_adapter_online
                    and [v.text() for v in w.instruments[:2]] == ["90%", "Released"]
                )
            )
            assert w.runway.layer == "Satellite" and not w.runway.overview.isNull()
        w.station.start(
            sys.argv[1],
            "Demo site · simulated mission" if preview else "field-console-bench",
            (0.0, 0.0, 0.0),
        )
        wait(lambda: w.add_target.isEnabled())
        for edit, value in zip(w.coords, [sys.argv[2], sys.argv[3], "200"]):
            edit.setText(value)
        w.preview.click()
        assert w.submit.isEnabled()
        w.submit.click()
        (mission,) = w.station.targets
        wait(lambda: mission in w.station.report_times)
        w.station.command("scan_complete")
        if preview:
            wait(
                lambda: (
                    w.station.selected_fod == mission
                    and w.track.current() is not None
                    and w.fsm_panel.current.text() == "Navigation"
                )
            )
            wait(
                lambda: (
                    bool(w.runway.tracks.plan) and len({p for _, p in w.runway.tracks.fixes}) >= 18
                )
            )
            assert w.runway.active == mission and w.queue_caption.text().endswith("0 queued")
            assert len(w.runway.obstacles) == 3 and w.obstacle_key.isVisible()
            w.fit_route.click()
            app.processEvents()
            assert w.grab().save(str(w.journal.folder / "navigation.png"))
            w.tabs.setCurrentWidget(w.fsm_panel)
            app.processEvents()
            assert w.grab().save(str(w.journal.folder / "state-machine.png"))
            QTimer.singleShot(4500, lambda: w.tabs.setCurrentWidget(w.map_page))
        wait(lambda: w.approve.isEnabled(), 120)
        assert w.review_page.isVisible() and w.station.active == mission
        assert w.approve.text() == "Approve removal" and not w.pictures[1].pixmap().isNull()
        if preview:
            wait(lambda: w.fsm_panel.current.text() == "Operator review")
            assert w.width() == 1366 and w.height() == 768
        assert console_frame().save(str(w.journal.folder / "operator-review.png"))
        w.review_page.close()
        app.processEvents()
        assert not w.scrim.isVisible() and w.open_review.isVisible() and not w.station.decisions
        w.open_review.click()
        assert w.scrim.isVisible() and w.review_page.isVisible()
        if preview:
            # Leave both images visible for the recorded operator review.
            deadline = time.monotonic() + 4
            wait(lambda: time.monotonic() >= deadline)
        w.approve.click()
        wait(
            lambda: w.station.state == "completed" and mission in w.station.final_reports,
            120,
        )
        assert w.station.targets[mission].startswith("completed")
        if preview:
            wait(lambda: w.station.state == "completed" and bool(w.track.current()))
            wait(lambda: w.fsm_panel.current.text() == "Completed")
            w.tabs.setCurrentWidget(w.fsm_panel)
            capture()
        timeline = w.timeline.toPlainText()
        for event in (
            "Sent to radio queue",
            "Radio ACK",
            "UGV accepted",
            "Active",
            "Awaiting operator review",
            "Operator requested approve removal",
            "Completed",
        ):
            assert event in timeline, timeline
        print("PASS: selected FOD timeline\n" + timeline, flush=True)
        print(
            "PASS: field client received final task and session completion.",
            w.journal.folder,
            flush=True,
        )
        Path(os.environ["GS_BENCH_JOURNAL"]).write_text(str(w.journal.folder))
        sys.exit(0)
    for area, button in [
        ("bench-cycle", "Continue unverified"),
        ("bench-image", "Approve removal"),
        ("bench-search", "Continue"),
    ]:
        w.station.start(sys.argv[1], area, (0.0, 0.0, 0.0))
        wait(lambda: w.add_target.isEnabled())
        if area == "bench-cycle":
            wait(lambda: w.robot_caption.text().startswith("UGV location stale"))
            assert w.runway.robot is None and not w.locate.isEnabled()
        elif area == "bench-image":
            wait(lambda: w.runway.robot == (40.0, -83.0, None))
        else:
            wait(lambda: w.runway.robot == (40.0, -83.0, 180.0))
        for edit, value in zip(w.coords, ["40", "-82.99997", "200"]):
            edit.setText(value)
        start = time.monotonic() - w.journal.start
        w.preview.click()
        assert w.submit.isEnabled()
        w.submit.click()
        start_stamp = w.track.stamp
        wait(lambda: w.approve.isEnabled(), 35)
        assert w.approve.text() == button and w.retry.isEnabled() == (area != "bench-cycle")
        if area == "bench-image":
            wait(lambda: not w.pictures[0].pixmap().isNull())
            assert w.track.stamp > start_stamp and w.track.current() is not None
            end = time.monotonic() - w.journal.start
            rows = (w.journal.folder / "events.jsonl").read_text().splitlines()
            arrivals = [
                row["elapsed_s"]
                for line in rows
                if (row := json.loads(line))["event"] == "location"
                and start <= row["elapsed_s"] <= end
            ]
            boundaries = [start, *arrivals, end]
            result = {
                "submit_to_review_s": end - start,
                "fixes_during_review": len(arrivals),
                "longest_ui_gap_s": max(b - a for a, b in pairwise(boundaries)),
            }
            (w.journal.folder / "image-timing.json").write_text(json.dumps(result, indent=2))
            print("Location timing during JPEG review:", result, flush=True)
            assert result["longest_ui_gap_s"] < 3.0, result
        (w.retry if area == "bench-search" else w.approve).click()
        wait(
            lambda: (
                w.station.state == "completed"
                and any("unverified" in x for x in w.station.targets.values())
            )
        )
    gateway = int(os.environ["GS_BENCH_GATEWAY_PID"])
    os.killpg(gateway, signal.SIGSTOP)
    try:
        wait(lambda: w.radio.engine.lost and not w.start.isEnabled())
    finally:
        os.killpg(gateway, signal.SIGCONT)
    time.sleep(1.5)
    app.processEvents()
    assert not w.radio.ready and not w.start.isEnabled()
    previous = w.journal.folder
    assert w.link.text() == "Link lost" and w.runway.robot is None
    w.connect_button.click()
    assert w.link.text() == "Disconnected"
    w.connect_button.click()
    wait(lambda: w.start.isEnabled())
    wait(lambda: w.runway.robot == (40.0, -83.0, 180.0))
    assert w.journal.folder != previous and not w.station.session
    assert not any(button.isEnabled() for button in (w.submit, w.scan, w.approve, w.abort))
    print(
        "PASS: radio reviews, link loss and explicit reconnect with no active mission. Logs:",
        previous,
        w.journal.folder,
    )
finally:
    w.disconnect()
