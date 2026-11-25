"""Uses the actual field UI handlers and serial worker in a hardware-isolated bench."""

import os
import signal
import socket
import sys
import time

from PySide6.QtWidgets import QApplication

from gs import Window

assert {name for _, name in socket.if_nameindex()} == {"lo"}
assert os.environ.get("QT_QPA_PLATFORM") == "offscreen"
app = QApplication([])
w = Window()
w.port.setCurrentText("/dev/ttyUSB0")
w.connect_radio()


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
    time.sleep(0.7)
    for area, button in [
        ("bench-cycle", "Continue"),
        ("bench-image", "Approve removal"),
        ("bench-search", "Continue"),
    ]:
        w.station.start(sys.argv[1], area, (0.0, 0.0, 0.0))
        wait(lambda: w.submit.isEnabled())
        for edit, value in zip(w.coords, ["40", "-82.99997", "200"]):
            edit.setText(value)
        w.submit.click()
        wait(lambda: w.approve.isEnabled())
        assert w.approve.text() == button and w.retry.isEnabled() == (area != "bench-cycle")
        if area == "bench-image":
            wait(lambda: not w.pictures[0].pixmap().isNull())
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
    print(
        "PASS: real field radio client and ROS gateway; simulated peer workflow. Log:",
        w.journal.folder,
    )
finally:
    w.disconnect()
