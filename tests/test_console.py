"""Exercise the desktop controls offscreen. No network or serial device is required."""

import json
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QMessageBox

import gs
from demo_site import DEMO_AREA, SITE, demo_obstacles
from station import Journal


@pytest.fixture
def window(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    app.styleHints().setColorScheme(Qt.ColorScheme.Dark)
    monkeypatch.setattr(gs, "Journal", lambda _: Journal(tmp_path))
    monkeypatch.setattr(gs.RunwayMap, "load", lambda *args: None)
    widget = gs.Window()
    widget.timer.stop()
    widget.show()
    app.processEvents()
    yield widget
    widget.disconnect()
    widget.close()


def test_light_map_preview_without_radio(window):
    w = window
    assert QApplication.palette().color(QPalette.ColorRole.Base).lightness() > 230
    assert not w.submit.isEnabled() and not w.approve.isEnabled()
    w.coords[0].setText("95")
    w.coords[1].setText("0")
    w.preview.click()
    assert w.runway.center == SITE and "valid latitude" in w.feedback.text()
    w.coords[0].setText("40.25")
    w.preview.click()
    w.tick()
    assert w.runway.center == (40.25, 0.0) and not w.runway.targets
    zoom = w.runway.zoom
    w.zoom_in.click()
    assert w.runway.zoom == zoom + 1
    QTest.mousePress(w.runway, Qt.MouseButton.LeftButton, pos=QPoint(28, 28))
    QTest.mouseMove(w.runway, QPoint(68, 28))
    QTest.mouseRelease(w.runway, Qt.MouseButton.LeftButton, pos=QPoint(68, 28))
    assert w.runway.center != w.runway.preview_location == (40.25, 0.0)
    assert w.radio is None


def test_missing_serial_port_is_actionable(window, tmp_path):
    w = window
    # Invalid device path runs the actual transport's open failure on all hosts.
    w.port.setCurrentText(str(tmp_path / "missing" / "serial"))
    w.connect_button.click()
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        w.tick()
        if w.link.text() == "Port unavailable":
            break
        QTest.qWait(20)
    assert w.link.text() == "Port unavailable"
    assert not w.start.isEnabled() and not w.submit.isEnabled()
    w.connect_button.click()
    assert w.link.text() == "Disconnected" and w.open_logs.isEnabled()
    records = [
        json.loads(line) for line in (w.log_folder / "events.jsonl").read_text().splitlines()
    ]
    assert "port_unavailable" in {row["event"] for row in records}


def test_session_controls_and_reported_queue(window, monkeypatch):
    w = window
    sent = []
    monkeypatch.setattr(gs.Radio, "start", lambda self: None)
    monkeypatch.setattr(gs.Radio, "close", lambda self: None)
    monkeypatch.setattr(gs.Radio, "send", lambda self, topic, msg: sent.append((topic, msg)) or 1)
    w.port.setCurrentText("fixture")
    w.connect_button.click()
    w.radio.ready = True
    w.tick()

    def fill_start():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, QDialog)
        for field, value in zip(dialog.findChildren(QLineEdit), ("UGV-1", "test", "1", "2", "90")):
            field.setText(value)
        dialog.accept()

    QTimer.singleShot(0, fill_start)
    w.start.click()
    w.tick()
    assert sent[-1][1].command == "start" and not w.submit.isEnabled()
    w.start.click()
    assert sent[-1][1].session_id == sent[0][1].session_id
    st = w.station

    def receive(active="", queued=(), scanned=False):
        msg = w.codec.types["SessionStatus"](
            w.codec.stamp(),
            st.session,
            "working",
            active,
            list(queued),
            scanned,
            0,
            0,
            "NAVIGATION_MODE",
            "",
        )
        w.radio.receive(8, w.codec.encode(msg))
        w.tick()

    receive()
    for field, value in zip(w.coords, ("40.2", "-83.0", "200")):
        field.setText(value)
    assert not w.submit.isEnabled()
    w.preview.click()
    w.submit.click()
    target = sent[-1][1].mission_id
    w.radio.event("ack", topic=5, wire_id=1)
    w.radio.event("nack", topic=7, wire_id=1)
    w.tick()
    assert "Radio ACK" not in w.timeline.toPlainText()
    w.radio.event("ack", topic=7, wire_id=1)
    w.radio.event("ack", topic=7, wire_id=1)
    receive(target, ["onboard-2"])
    assert w.timeline.toPlainText().count("Radio ACK") == 1
    assert "UGV accepted" in w.timeline.toPlainText() and "Active" in w.timeline.toPlainText()
    w.tasks.selectRow(1)
    assert (
        "UGV accepted" in w.timeline.toPlainText() and "Radio ACK" not in w.timeline.toPlainText()
    )
    receive("onboard-2", [target])
    assert w.tasks.item(w.tasks.currentRow(), 0).text() == "onboard-2"
    receive(target, ["onboard-2"])
    assert w.timeline.toPlainText().count("Active") == 1
    assert w.runway.targets == {target: (40.2, -83.0)} and w.runway.active == target
    assert w.tasks.item(1, 0).text() == "onboard-2" and w.tasks.item(1, 1).text() == "Queue 1"
    w.locate_row(w.tasks.item(1, 0))
    assert "No mappable coordinates" in w.feedback.text()
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    w.scan.click()
    assert not st.scanning_requested
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    w.scan.click()
    assert sent[-1][1].command == "scan_complete" and not w.submit.isEnabled()
    receive(scanned=True)
    assert w.scan.text() == "Scanning closed"
    w.abort.click()
    assert sent[-1][1].command == "abort" and not w.abort.isEnabled()
    w.disconnect()
    assert w.tasks.rowCount() == 0 and not w.runway.targets and w.runway.center == SITE


def test_review_follows_current_evidence_and_keeps_original_resolution(
    window, monkeypatch, tmp_path
):
    import hashlib

    import numpy as np
    from PySide6.QtGui import QImage

    w = window
    monkeypatch.setattr(gs.Radio, "start", lambda self: None)
    monkeypatch.setattr(gs.Radio, "close", lambda self: None)
    monkeypatch.setattr(gs.Radio, "send", lambda *args: 17)
    w.port.setCurrentText("fixture")
    w.connect_button.click()
    w.radio.ready = True
    w.station.start("UGV-1", "bench", (0, 0, 0))
    c = w.codec
    session = w.station.session
    status = c.types["SessionStatus"](
        c.stamp(),
        session,
        "working",
        "fod-1",
        [],
        False,
        0,
        0,
        "OK_TO_MOVE_FORWARD",
        "",
    )
    w.radio.receive(8, c.encode(status))
    image = QImage(320, 240, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.white)
    path = tmp_path / "test.jpg"
    assert image.save(str(path))
    raw = path.read_bytes()

    def receive(token, kind, data):
        stamp = c.stamp()
        preview = c.store.types["sensor_msgs/msg/CompressedImage"](
            c.types["Header"](stamp, "optical"),
            "jpeg",
            np.frombuffer(data, dtype=np.uint8),
        )
        msg = c.types["Evidence"](
            stamp,
            session,
            "fod-1",
            token,
            kind,
            stamp,
            "optical",
            "",
            "",
            preview,
            hashlib.sha256(data).hexdigest(),
            "",
            c.types["GeoPoint"](0, 0, 0),
            c.types["Point"](0, 0, 0),
            0.23,
        )
        w.radio.receive(19, c.encode(msg))
        w.tick()

    receive("before", "before", raw)
    assert not w.approve.isEnabled()
    w.resize(1366, 768)
    receive("after", "after", raw)
    QApplication.processEvents()
    assert w.size().toTuple() == (1366, 768)
    assert w.approve.mapTo(w, w.approve.rect().bottomRight()).y() < w.height()
    assert (
        w.review_page.isVisible() and w.tabs.currentWidget() == w.map_page and w.approve.isEnabled()
    )
    assert w.inspect.isEnabled() and w.pictures[1].original.size() == image.size()
    assert w.scrim.isVisible() and QApplication.activeModalWidget() is w.review_page
    QTest.keyClick(w.review_page, Qt.Key.Key_Escape)
    assert not w.scrim.isVisible() and not w.station.decisions
    for _ in range(3):
        w.tick()
    assert w.open_review.isVisible() and not w.review_page.isVisible()
    w.open_review.click()
    assert w.review_page.isVisible() and w.open_review.text() == "Open operator review"
    w.later.click()
    w.tick()
    assert not w.scrim.isVisible() and not w.review_page.isVisible()
    w.open_review.click()
    QTimer.singleShot(0, lambda: QApplication.activeModalWidget().reject())
    w.inspect.click()
    receive("new-frame", "after", b"unreadable image")
    assert not w.approve.isEnabled() and w.pictures[1].pixmap().isNull()
    receive("cycle", "cycle_complete", b"")
    assert w.approve.isEnabled() and not w.retry.isEnabled()
    w.approve.click()
    assert not w.approve.isEnabled() and w.station.decisions == {"cycle"}
    assert not w.review_page.isVisible() and not w.scrim.isVisible()


def test_location_marker_and_freshness(window, monkeypatch):
    from types import SimpleNamespace

    import location

    now = time.time_ns()
    clock = [now, time.monotonic_ns()]
    monkeypatch.setattr(
        location,
        "time",
        SimpleNamespace(time_ns=lambda: clock[0], monotonic_ns=lambda: clock[1]),
    )
    w = window
    monkeypatch.setattr(gs.Radio, "start", lambda self: None)
    monkeypatch.setattr(gs.Radio, "close", lambda self: None)
    w.port.setCurrentText("fixture")
    w.connect_button.click()
    w.radio.ready = True

    def emit(ts, lat=0, lon=0, hdg=0):
        utc = w.codec.types["Time"](*divmod(ts, 10**9))
        msg = w.codec.types["RobotLocation"](utc, lat, lon, hdg)
        w.radio.receive(20, w.codec.encode(msg))
        w.tick()

    for ts in (now - 10**9, now + 201_000_000):
        emit(ts)
        assert w.runway.robot is None
    emit(now)
    assert w.runway.robot == (0, 0, 0) and w.runway.center == (0, 0)
    assert w.locate.isEnabled() and w.runway.preview_location is None
    w.runway.preview(40, -83)
    clock[0] += 10**9
    for delta, lat, lon, h in [
        (0, 1, 3, 10),
        (10**9, 900000001, 0, 0),
        (10**9, 0, 1800000001, 0),
        (10**9, 0, 0, 36000),
    ]:
        emit(now + delta, lat, lon, h)
        assert w.runway.robot == (0, 0, 0)
    emit(clock[0], -10000000, 1799999999, 65535)
    assert w.runway.robot == (-1, 179.9999999, None)
    assert w.runway.center == (40, -83) and "unavailable" in w.robot_caption.text()
    w.locate.click()
    assert w.runway.center == w.runway.robot[:2]
    clock[1] += 3_100_000_000
    w.tick()
    assert w.runway.robot is None and not w.locate.isEnabled() and "stale" in w.robot_caption.text()
    clock[0] += 4_000_000_000
    emit(clock[0], 899999999, 0, 35999)
    assert w.runway.robot is None and "89.9999999" in w.robot_caption.text()
    w.disconnect()
    assert w.runway.robot is None


def test_copy_diagnostics_without_radio(window):
    window.port.setCurrentText("COM14")
    window.copy_info.click()
    text = QApplication.clipboard().text()
    for value in (
        window.build,
        f"Wire map v{gs.WIRE_MAP_VERSION}",
        "Port: COM14",
        "Disconnected",
        "no connection yet",
    ):
        assert value in text
    assert window.radio is None


def test_jetson_instruments_and_stale_app_controls(window, monkeypatch):
    w = window
    monkeypatch.setattr(gs.Radio, "start", lambda self: None)
    monkeypatch.setattr(gs.Radio, "close", lambda self: None)
    sent = []
    monkeypatch.setattr(gs.Radio, "send", lambda self, topic, msg: sent.append(msg) or 1)
    w.port.setCurrentText("fixture")
    w.connect_radio()
    w.radio.ready = True
    msg = w.codec.types["FieldStatus"](w.codec.stamp(), "", "", "idle", "", 75, 1, False, 0, 0)
    w.radio.receive(24, w.codec.encode(msg))
    w.tick()
    assert not w.start.isEnabled() and w.launch_app.isEnabled()
    assert [widget.text() for widget in w.instruments] == ["75%", "Active", "—"]
    w.app_choice.setCurrentIndex(w.app_choice.findData("scenario"))
    w.scenario.setCurrentText("retry")
    w.wait_timeout.setChecked(True)
    w.launch_app.click()
    assert len(sent) == 1 and sent[0].scenario == "retry" and sent[0].wait_timeout
    assert not w.launch_app.isEnabled() and not w.stop_app.isEnabled()
    msg.job_id, msg.state, msg.stamp = sent[0].request_id, "running", w.codec.stamp()
    w.radio.receive(24, w.codec.encode(msg))
    w.tick()
    assert w.stop_app.isEnabled() and not w.launch_app.isEnabled()
    w.app_received -= 4
    w.tick()
    assert not w.launch_app.isEnabled() and not w.stop_app.isEnabled()
    assert [widget.text() for widget in w.instruments] == ["—"] * 3
    w.disconnect()


def test_demo_site_only_changes_map_view(window):
    window.coords[0].setText("40")
    window.site_action.trigger()
    assert window.runway.center == (27.517888, -82.084153)
    assert window.runway.robot is None and window.runway.preview_location is None
    assert not window.runway.targets and window.coords[0].text() == "40"
    assert window.station is None and window.radio is None


def test_offline_replay_and_controls(window, monkeypatch):
    monkeypatch.setattr(gs, "Radio", lambda *args: pytest.fail("Replay opened a serial connection"))
    checked = []

    def inspect():
        dialog = QApplication.activeModalWidget()
        try:
            assert isinstance(dialog, gs.DemoReplay)
            dialog.play.click()
            assert not dialog.timer.isActive()
            dialog.slider.setValue(dialog.slider.maximum())
            assert dialog.phase.text() == "Mission complete at base"
            assert dialog.play.text() == "Replay" and not dialog.grab().isNull()
            dialog.play.click()
            assert dialog.slider.value() == 0 and dialog.timer.isActive()
            checked.append(True)
        finally:
            dialog.accept()

    QTimer.singleShot(0, inspect)
    window.demo_button.trigger()
    assert checked and window.radio is None and window.station is None


def test_health_colors_and_stale_reset(window):
    panel = window.health_panel
    assert panel.isHidden() and window.health_summary.text() == "Awaiting system status"
    window.health_details.click()
    assert not panel.isHidden()
    window.health_details.click()
    assert panel.isHidden()
    kind = window.codec.types["FieldStatus"]
    msg = kind(
        window.codec.stamp(),
        "",
        "",
        "idle",
        "",
        15,
        1,
        True,
        0,
        (1 << kind.NAV2) | (2 << kind.ODOMETRY) | (3 << kind.GNSS),
    )
    panel.display("Radio ready", msg)
    assert [panel.rows[n].property("level") for n in ("Nav2", "Odometry", "GNSS", "Cleaning")] == [
        1,
        2,
        3,
        0,
    ]
    assert panel.summary() == "Check E-stop, GNSS, Battery, Odometry"
    panel.display("Link lost", None)
    assert panel.rows["Radio"].property("level") == 3
    assert all(w.property("level") == 0 for n, w in panel.rows.items() if n != "Radio")


def test_fsm_tab_follows_radio_and_clears_on_loss(window, monkeypatch):
    w = window
    monkeypatch.setattr(gs.Radio, "start", lambda self: None)
    monkeypatch.setattr(gs.Radio, "close", lambda self: None)
    w.port.setCurrentText("fixture")
    w.connect_radio()
    w.radio.ready = True
    panel = w.fsm_panel
    w.tabs.setCurrentWidget(panel)
    kind = w.codec.types["FieldStatus"]
    codes = {getattr(kind, k) for k in dir(kind) if k.startswith("FSM_") and k != "FSM_UNKNOWN"}
    assert set(panel.states) == codes

    def sample(code):
        msg = kind(w.codec.stamp(), "", "", "idle", "", -1, -1, True, code, 0)
        w.radio.receive(24, w.codec.encode(msg))
        w.tick()
        return msg

    w.tick()
    assert panel.current.text() == "Radio standby" and panel.chart.active == "STANDBY_MODE"
    assert panel.chart.inferred and "awaiting onboard" in panel.detail.text()
    sample(kind.FSM_STANDBY_MODE)
    assert panel.current.text() == "Standby" and not panel.chart.inferred
    scale = panel.chart.transform().m11()
    panel.zoom_in.click()
    assert panel.chart.transform().m11() == pytest.approx(scale * 1.25)
    old = sample(kind.FSM_CLEANING_MODE)
    assert panel.chart.transform().m11() == pytest.approx(scale * 1.25)
    panel.zoom_out.click()
    assert panel.chart.transform().m11() == pytest.approx(scale)
    panel.fit.click()
    assert not panel.follow.isChecked()
    panel.follow.click()
    sample(kind.FSM_OK_TO_MOVE_FORWARD)
    w.radio.receive(24, w.codec.encode(old))
    w.tick()
    assert panel.current.text() == "Operator review" and "OK_TO_MOVE_FORWARD" in panel.detail.text()
    assert panel.chart.active == "OK_TO_MOVE_FORWARD"
    assert panel.chart.renderer.elementExists(panel.chart.active)
    assert all(panel.chart.renderer.elementExists(state) for _, state in panel.states.values())
    panel.follow.click()
    assert not panel.chart.follow and panel.chart.active == "OK_TO_MOVE_FORWARD"
    panel.follow.click()
    sample(250)
    assert "Unknown state 250" in panel.detail.text()
    sample(kind.FSM_HALTED)
    assert panel.current.text() == "Halted"
    w.app_received -= 4
    w.tick()
    assert panel.current.text() == "FSM unavailable"
    assert panel.chart.active is None
    sample(kind.FSM_REPOSITIONING_MODE)
    w.radio.ready = False
    w.tick()
    assert panel.current.text() == "FSM unavailable"


def test_workspace_map_selection_and_shared_actions(window, monkeypatch):
    w = window
    w.site_action.trigger()
    w.runway.targets["fod-west"] = w.runway.center
    w.update_targets(
        SimpleNamespace(
            queue=["fod-west"], targets={}, timeline={}, final_reports=set(), last_status=0
        ),
        "",
    )
    w.tasks.clearSelection()
    QTest.mouseClick(w.runway, Qt.MouseButton.LeftButton, pos=w.runway.rect().center())
    assert w.tasks.selectedItems()[0].text() == "fod-west"
    QTest.mouseClick(w.runway, Qt.MouseButton.LeftButton, pos=QPoint(20, 40))
    assert w.draft_dialog.isVisible()
    assert all(field.text() for field in w.coords[:2])
    assert w.draft is not None and not w.submit.action.isEnabled()
    w.preview.click()
    assert w.draft is not None
    w.coords[2].setText("2")
    assert w.draft is None
    w.draft_dialog.close()
    w.runway.targets = {"a": (15, 179.999), "b": (15.001, -179.999)}
    w.fit_targets()
    assert abs(w.runway.center[1]) > 179.9
    for point in w.runway.targets.values():
        assert w.runway.rect().contains(w.runway.screen_point(point).toPoint())
    w.tabs.setCurrentWidget(w.fsm_panel)
    w.restore_layout()
    assert w.tabs.currentWidget() is w.map_page
    mission = next(m.menu() for m in w.menuBar().actions() if m.text() == "&Mission")
    assert mission.actions() == [b.action for b in (w.start, w.add_target, w.scan, w.abort)]
    for action in mission.actions():
        assert not action.isEnabled()
    assert w.station is None


def test_recording_and_evidence_export(window, tmp_path):
    from zipfile import ZipFile

    from session_files import JournalReplay, export_evidence

    log = Journal(tmp_path)
    log.log("connect", "bench")
    log.start -= 1
    log.log("location", "UGV position", pose=[27.5, -82, 45])
    log.log("plan", "Nav2 display waypoints", points=[[27.5, -82], [27.51, -82.01]])
    log.log(
        "sent",
        "FOD",
        topic=7,
        message={
            "mission_id": "fod-1",
            "location": {"latitude": 27.51, "longitude": -82.01},
        },
    )
    log.start -= 4
    log.log("track_clear", "Operator cleared displayed GPS track")
    log.log("fsm_sample", "NAVIGATION_MODE")
    log.close()
    dialog = JournalReplay(log.folder / "events.jsonl", window)
    dialog.slider.setValue(1100)
    assert len(dialog.map.tracks.plan) == 2 and len(dialog.map.tracks.fixes) == 1
    assert dialog.map.robot == (27.5, -82, 45) and "fod-1" in dialog.map.targets
    dialog.slider.setValue(dialog.slider.maximum())
    assert not dialog.map.tracks.fixes and dialog.map.tracks.plan
    assert dialog.map.robot is None and "NAVIGATION_MODE" in dialog.log.toPlainText()
    dialog.slider.setValue(0)
    assert not dialog.map.targets and dialog.map.robot is None
    dialog.close()
    assert window.radio is None
    (log.folder / "ignore.txt").write_text("local note")
    (log.folder / "evidence" / "test.jpg").write_bytes(b"preview")
    dest = tmp_path / "session.zip"
    export_evidence(log.folder, dest)
    with ZipFile(dest) as bundle:
        assert set(bundle.namelist()) == {
            "events.jsonl",
            "console.log",
            "evidence/test.jpg",
        }
        assert bundle.read("events.jsonl") == (log.folder / "events.jsonl").read_bytes()
    bad = tmp_path / "broken.jsonl"
    bad.write_text("{}\n")
    with pytest.raises(ValueError, match="line 1"):
        JournalReplay(bad, window)


def test_route_overlay_replaces_expires_and_clear_is_local(window, monkeypatch):
    import numpy as np

    from map_tracks import PLAN_TTL_NS

    w = window
    monkeypatch.setattr(gs.Radio, "start", lambda self: None)
    monkeypatch.setattr(gs.Radio, "close", lambda self: None)
    monkeypatch.setattr(
        gs.Radio, "send", lambda *args: pytest.fail("Map tools transmitted a command")
    )
    w.port.setCurrentText("fixture")
    w.connect_radio()
    w.radio.ready = True
    data = np.array([400000000, -830000000, 400000050, -830000100], dtype=np.int32)

    start_ns = time.time_ns() + 1_000_000

    def receive(offset_ms, values):
        monkeypatch.setattr(time, "time_ns", lambda: start_ns + offset_ms * 1_000_000)
        stamp = w.codec.stamp()
        msg = w.codec.types["PlanPreview"](stamp, values)
        w.radio.receive(25, w.codec.encode(msg))
        w.tick()

    receive(0, data)
    tracks = w.runway.tracks
    assert tracks.plan == ((40.0, -83.0), (40.000005, -83.00001))
    receive(0, data[::-1].copy())
    assert tracks.plan[0] == (40.0, -83.0)
    tracks.fix(time.time_ns(), (40.0, -83.0))
    w.tick()
    w.clear_track.click()
    assert not tracks.fixes and tracks.plan and not w.clear_track.isEnabled()
    receive(100, np.array([1, 2], dtype=np.int32))
    assert tracks.plan[0] == (40.0, -83.0)
    receive(200, np.array([], dtype=np.int32))
    assert not tracks.plan
    receive(300, data)
    assert tracks.plan
    expiry = start_ns + 300_000_000 + PLAN_TTL_NS
    assert not tracks.expire(expiry)
    assert tracks.expire(expiry + 1) and not tracks.plan
    receive(400, data)
    assert tracks.plan
    w.radio.ready = False
    w.tick()
    assert not tracks.plan


def test_map_advances_sequentially_and_preserves_progress_on_refresh():
    from map_tracks import MapTracks

    route = ((0, 0), (0, 0.00005), (0.000007, 0.00005), (0.000007, 0))
    tracks = MapTracks()
    tracks.replace(1, route)
    tracks.fix(2, (0.000007, 0.000025))  # Close parallel leg, outside outbound corridor.
    assert tracks.segment == 0 and tracks.fraction == 0
    tracks.fix(3, (0, 0.000025))
    assert tracks.segment == 0 and tracks.fraction == pytest.approx(0.5)
    tracks.fix(4, (0, 0.000020))  # A noisy step backwards cannot re-grow the route.
    tracks.replace(5, route)
    assert tracks.fraction == pytest.approx(0.5)
    tracks.fix(6, route[1])
    assert tracks.segment == 1 and tracks.ahead[1:] == route[2:]
    tracks.fixes.clear()
    tracks.fix(7, route[2])
    tracks.fix(8, route[3])
    assert not tracks.ahead and tracks.plan == route
    tracks.replace(9, route)
    assert not tracks.ahead
    tracks.replace(10, tuple(reversed(route)))
    assert tracks.ahead and tracks.segment == 0

    wrapped = MapTracks()
    wrapped.replace(1, ((0, 179.99999), (0, -179.99999)))
    wrapped.fix(2, (0, 180))
    assert wrapped.fraction == pytest.approx(0.5)
    assert abs(wrapped.ahead[0][1]) == pytest.approx(180)
    wrapped.replace(3, ())
    assert not wrapped.ahead


def test_eastern_clock_and_receive_age(window):
    from datetime import datetime

    from display import eastern

    for date, expected in (("2026-01-01", "07:00:00"), ("2026-07-01", "08:00:00")):
        assert eastern(datetime.fromisoformat(date + "T12:00:00+00:00").timestamp()) == expected
    for elapsed, unit in ((0.42, " ms ago"), (1.25, " s ago")):
        window.last_rx = time.monotonic() - elapsed
        window.tick()
        assert window.last_update.text().endswith(unit)
    assert window.clock.text().endswith(" ET")


def test_replay_scrub_restores_remaining_route(window, tmp_path):
    from session_files import JournalReplay

    route = [[30, -82], [30, -81.99995], [30.00005, -81.99995]]
    events = [
        ("plan", {"points": route}),
        ("location", {"pose": [30, -81.99996, 90]}),
        ("location", {"pose": [*route[1], 90]}),
        ("plan", {"points": route}),  # Same plan heartbeat.
        ("track_clear", {}),
        ("location", {"pose": [*route[2], 0]}),
    ]
    path = tmp_path / "events.jsonl"
    with path.open("w") as f:
        for i, (event, fields) in enumerate(events):
            row = dict(
                event=event,
                detail="",
                elapsed_s=i,
                source_ns=i * 10**9,
                utc="2026-11-25T22:00:00Z",
                **fields,
            )
            f.write(json.dumps(row) + "\n")
    replay = JournalReplay(path, window)
    replay.slider.setValue(4000)
    assert replay.map.tracks.segment == 1 and not replay.map.tracks.fixes
    assert replay.map.tracks.ahead[1:] == (tuple(route[2]),)
    replay.slider.setValue(5000)
    assert not replay.map.tracks.ahead
    replay.slider.setValue(1000)
    assert replay.map.tracks.segment == 0 and replay.map.tracks.fraction == pytest.approx(0.8)
    assert replay.map.tracks.ahead[1:] == tuple(map(tuple, route[1:]))
    replay.close()


def test_compact_target_labels_preserve_identity(window):
    w = window
    ids = ["fod-11223344aabb", "fod-11223344bbcc", "onboard-" + "a" * 32]
    st = SimpleNamespace(queue=ids, targets={}, timeline={}, final_reports=set(), last_status=0)
    w.update_targets(st, "")
    for i, raw in enumerate(ids):
        row = w.tasks.item(i, 0)
        assert row.text() == f"FOD {i + 1:02d}" == w.runway.target_name(raw)
        assert row.toolTip() == row.data(Qt.ItemDataRole.UserRole) == raw
        w.select_target(raw)
        assert w.runway.selected == raw
    st.queue = list(reversed(ids))
    w.update_targets(st, "")
    assert w.tasks.item(0, 0).text() == "FOD 03" and w.runway.selected == ids[-1]


def test_fix_pair_can_cross_short_waypoints_but_not_a_telemetry_gap():
    from map_tracks import GPS_GAP_NS, MapTracks

    route = ((0, 0), (0, 0.000005), (0, 0.000010), (0, 0.00005))
    for interval, segment in [(500_000_000, 2), (GPS_GAP_NS + 1, 0)]:
        tracks = MapTracks()
        tracks.replace(1, route)
        tracks.fix(2, route[0])
        tracks.fix(2 + interval, (0, 0.000025))
        assert tracks.segment == segment


def test_satellite_startup_and_layer_switch(window):
    from PySide6.QtCore import QUrlQuery
    from PySide6.QtWidgets import QPushButton

    from runway_map import tile_url

    view = window.runway
    assert view.layer == "Satellite" and view.center == SITE and view.initial_view
    assert view.overview.width() == 2048 and view.overview.height() == 1536
    assert view.robot is None and not view.obstacles and window.idle_connect.isVisible()
    center = view.grab().toImage()
    colors = {
        center.pixel(x, y)
        for x in range(50, view.width() - 50, 8)
        for y in range(50, view.height() - 50, 8)
    }
    assert len(colors) > 500
    assert view.zoom == 18
    controls = {b.text(): b for b in window.findChildren(QPushButton, "layerButton")}
    for name in ("Street", "Satellite"):
        controls[name].click()
        assert view.layer == name and controls[name].isChecked() and view.center == SITE
    assert tile_url("Street", 4, 5, 6).path() == "/4/5/6.png"
    assert tile_url("Satellite", 16, 7, 9).path().endswith("/tile/16/9/7")
    adjacent = [
        QUrlQuery(tile_url("Satellite", 18, x, 9)).queryItemValue("bbox").split(",") for x in (7, 8)
    ]
    assert adjacent[0][2] == adjacent[1][0] and adjacent[0][1] == adjacent[1][1]


def test_obstacles_in_connected_preview_only(window, monkeypatch):
    w = window
    monkeypatch.setattr(gs.Radio, "start", lambda self: None)
    monkeypatch.setattr(gs.Radio, "close", lambda self: None)
    monkeypatch.setattr(gs.Radio, "send", lambda *args: 1)
    w.port.setCurrentText("fixture")
    w.connect_button.click()
    w.radio.ready = True
    w.tick()
    assert not w.runway.obstacles and not w.obstacle_key.isVisible()
    w.station.start("BENCH-test", DEMO_AREA, (0, 0, 0))
    w.tick()
    assert len(w.runway.obstacles) == 3 and w.obstacle_key.isVisible()
    w.fit_route.click()
    img = w.runway.grab().toImage()
    for coord in w.runway.obstacles:
        assert img.pixelColor(w.runway.screen_point(coord).toPoint()).name() == "#ffc15c"
    w.disconnect()
    assert not w.runway.obstacles and not w.obstacle_key.isVisible()


def test_obstacles_follow_demo_session_in_recording(window, tmp_path):
    from session_files import JournalReplay

    points = demo_obstacles(DEMO_AREA, "BENCH-test")
    assert len(points) == 3
    assert points[0] == (SITE[0] + 0.65e-5, SITE[1] + 2.6e-5)
    assert not demo_obstacles(DEMO_AREA, "UGV-1") and not demo_obstacles("field", "BENCH-test")
    log = Journal(tmp_path)
    log.log("connect", "connected")
    log.start -= 1
    log.log(
        "sent",
        "start",
        topic=5,
        message={
            "command": "start",
            "area_of_interest": DEMO_AREA,
            "assigned_callsign": "BENCH-test",
        },
    )
    log.start -= 2
    log.log("disconnect", "disconnected")
    log.close()
    dialog = JournalReplay(log.folder / "events.jsonl", window)
    for ms, expected in [(0, ()), (2000, points), (dialog.slider.maximum(), ()), (2000, points)]:
        dialog.slider.setValue(ms)
        assert dialog.map.obstacles == expected
        if expected:
            dialog.map.fit_targets()
            assert all(
                dialog.map.rect().contains(dialog.map.screen_point(pt).toPoint()) for pt in expected
            )
    dialog.close()


def test_disabled_menu_text_is_visibly_lighter(window):
    from PySide6.QtWidgets import QMenu

    menu = QMenu(window)
    enabled = menu.addAction("Mission command")
    disabled = menu.addAction("Mission command")
    disabled.setEnabled(False)
    menu.ensurePolished()
    menu.resize(menu.sizeHint())
    image = menu.grab().toImage()

    def darkest_text(action):
        area = menu.actionGeometry(action).adjusted(4, 2, -4, -2)
        return min(
            image.pixelColor(x, y).lightness()
            for x in range(area.left(), area.right())
            for y in range(area.top(), area.bottom())
        )

    assert darkest_text(disabled) >= darkest_text(enabled) + 45
    assert not window.fit_route.action.isEnabled()


def test_killed_tile_worker_preserves_map_and_radio(window, monkeypatch):
    from pathlib import Path

    from PySide6.QtCore import QUrl

    import runway_map

    view = window.runway
    monkeypatch.setattr(gs.Radio, "start", lambda self: None)
    monkeypatch.setattr(gs.Radio, "close", lambda self: None)
    monkeypatch.setattr(gs.Radio, "send", lambda *args: pytest.fail("Map sent a radio command"))
    window.port.setCurrentText("fixture")
    window.connect_radio()
    window.radio.ready = True
    window.tick()
    station = window.station
    overview = Path(runway_map.__file__).parent / "demo/site-overview.jpg"
    monkeypatch.setattr(runway_map, "tile_url", lambda *args: QUrl.fromLocalFile(str(overview)))
    window.zoom_out.click()
    key = ("Satellite", 17, 35646, 55108)
    view.wanted.add(key)
    view.fetch_visible()
    assert view.worker.waitForStarted(5000)
    view.worker.kill()
    assert view.worker.waitForFinished(5000)
    assert key in view.failed and not view.pending and view.batch is None
    window.tick()
    assert window.station is station and window.radio.ready
    assert not view.overview.isNull() and view.zoom == 17
    window.zoom_in.click()
    assert view.zoom == 18
