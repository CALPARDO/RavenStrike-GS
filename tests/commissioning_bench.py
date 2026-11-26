"""Run the actual GS, radio transport and ROS field supervisor on private PTYs."""
import os
import socket
import sys
import time
from pathlib import Path

from PySide6.QtWidgets import QApplication

import radio
from gs import Window

if (os.environ.get("ROS_DOMAIN_ID") != "187"
        or {name for _, name in socket.if_nameindex()} != {"lo"}
        or Path(sys.argv[1]).resolve().parent != Path("/dev/pts")):
    raise RuntimeError("Use tests/bench.sh UGV_CHECKOUT diagnostics in the private software sandbox")

radio.CONTRACT = Path(sys.argv[1]).parent  # PTY configuration written by bench.sh.
app = QApplication([])
w = Window()
w.port.setCurrentText(sys.argv[1])
w.connect_radio()


def wait_for(check, seconds, what):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if check():
            return
        time.sleep(.01)
    raise RuntimeError(f"Timeout waiting for {what}; link={w.link.text()}")


try:
    wait_for(lambda: w.radio.ready and w.app_fresh(), 30, "fresh supervisor telemetry")
    w.open_tests()
    d = w.tests_dialog
    d.run_all.click()
    wait_for(lambda: not d.timer.isActive(), 145, "all commissioning checks")
    # Private, empty ROS domain: the field supervisor is the only onboard application.
    expected = {"codec": "Passed", "record": "Passed", "radio": "Passed", "resources": "Passed", "router": "Skipped"}
    for key, (item, info, _) in d.rows.items():
        status = item.text(1)
        print(f"{key}: {status} ({item.text(2)}) · {info.text(0)}", flush=True)
        assert status == expected.get(key, "Hardware Unavailable")
    step = Path(sys.argv[1]).parent / "health-step"
    for stimulus, gnss_status in (("healthy", "Passed"), ("fault", "Failed")):
        step.write_text(stimulus)
        d.start(["battery", "estop", "gnss"])
        wait_for(lambda: not d.timer.isActive(), 35, stimulus + " sensor fixture")
        assert d.rows["battery"][0].text(1) == d.rows["estop"][0].text(1) == "Passed"
        assert ("engaged" if stimulus == "fault" else "released") in d.rows["estop"][1].text(0)
        assert d.rows["gnss"][0].text(1) == gnss_status
        for key in ("battery", "estop", "gnss"):
            item, info, _ = d.rows[key]
            print(f"Fixture {stimulus} · {key}: {item.text(1)} · {info.text(0)}", flush=True)
    print("PASS · GS ↔ virtual serial ↔ UGV gateway ↔ field supervisor; evidence:", w.journal.folder)
finally:
    w.disconnect()
    w.close()
