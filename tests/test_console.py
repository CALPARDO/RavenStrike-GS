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
    receive(target, ["onboard-2"])
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
        c.stamp(), session, "working", "fod-1", [], False, 0, 0, "OK_TO_MOVE_FORWARD", ""
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
            c.types["Header"](stamp, "optical"), "jpeg", np.frombuffer(data, dtype=np.uint8)
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
