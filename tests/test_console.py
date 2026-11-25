"""Exercise the desktop controls offscreen. No network or serial device is required."""

import json
import time

import pytest
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QMessageBox

import gs
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
    assert w.runway.center is None and "valid latitude" in w.feedback.text()
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
    assert w.tasks.rowCount() == 0 and not w.runway.targets and w.runway.center is None


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
    receive("after", "after", raw)
    assert w.tabs.currentWidget() == w.review_page and w.approve.isEnabled()
    assert w.inspect.isEnabled() and w.pictures[1].original.size() == image.size()
    QTimer.singleShot(0, lambda: QApplication.activeModalWidget().reject())
    w.inspect.click()
    receive("new-frame", "after", b"unreadable image")
    assert not w.approve.isEnabled() and w.pictures[1].pixmap().isNull()
    receive("cycle", "cycle_complete", b"")
    assert w.approve.isEnabled() and not w.retry.isEnabled()
    w.approve.click()
    assert not w.approve.isEnabled() and w.station.decisions == {"cycle"}


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
        "Wire map v5",
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
    msg = w.codec.types["AppStatus"](w.codec.stamp(), "", "", "idle", "", 75, 1, False)
    w.radio.receive(22, w.codec.encode(msg))
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
    w.radio.receive(22, w.codec.encode(msg))
    w.tick()
    assert w.stop_app.isEnabled() and not w.launch_app.isEnabled()
    w.app_received -= 4
    w.tick()
    assert not w.launch_app.isEnabled() and not w.stop_app.isEnabled()
    assert [widget.text() for widget in w.instruments] == ["—"] * 3
    w.disconnect()


def test_demo_site_only_changes_map_view(window):
    window.coords[0].setText("40")
    window.site_button.click()
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
    window.demo_button.click()
    assert checked and window.radio is None and window.station is None
