"""Exercise the field launcher using a USB radio or the paired Linux PTY fixture."""

import sys
import time
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QMessageBox

import gs
import radio

load = radio.load_config


def pty_config(*args, **kwargs):
    cfg = load(*args, **kwargs)
    cfg.serial.rtscts = False
    return cfg


if sys.argv[1].startswith("/dev/pts/"):
    radio.load_config = pty_config
app = QApplication([])
w = gs.Window()
w.port.setCurrentText(sys.argv[1])


def wait(check, seconds=15):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        app.processEvents()
        if check():
            return
        time.sleep(0.025)
    raise AssertionError((w.link_state, w.job_label.text(), w.log.toPlainText()[-900:]))


try:
    w.connect_radio()
    w.sim_button.click()
    wait(w.launch_app.isEnabled)
    assert w.app_snapshot.state in ("idle", "stopped", "finished", "failed")
    assert not w.app_snapshot.mission_adapter_online
    assert [v.text() for v in w.instruments] == ["—"] * 3
    print("Journal:", w.journal.folder, flush=True)
    print("PASS matched serial peers, fresh idle status, absent sensors unknown", flush=True)
    w.seed.setText("952741458")
    w.launch_app.click()
    wait(w.stop_app.isEnabled)
    wait(lambda: w.sim_view.seed == 952741458 and w.sim_view.stamp > 0, 45)
    assert w.sim_view.plot.targets and w.sim_view.plot.barriers and w.runway.robot is None
    wait(lambda: any(x*x + y*y > 9 for _, x, y, _ in w.sim_view.plot.trace["fixes"]), 120)
    w.pause_app.click()
    wait(lambda: w.app_snapshot.state == "paused", 45)
    start = w.sim_view.plot.trace["fixes"][-1]
    wait(lambda: w.sim_view.stamp >= start[0] + 2000)
    current = w.sim_view.plot.trace["fixes"][-1]
    assert abs(start[1] - current[1]) + abs(start[2] - current[2]) <= .04 and w.radio.ready
    w.pause_app.click()
    wait(lambda: w.app_snapshot.state == "running")
    wait(lambda: any((x-current[1])**2 + (y-current[2])**2 > .09 for t,x,y,_ in
                     w.sim_view.plot.trace["fixes"] if t > current[0]), 45)
    print("PASS measured stationary pause, radio lease and resumed Nav2 motion", flush=True)
    w.show()
    if not w.grab().save(str(w.journal.folder / "simulation-workspace.png")):
        raise AssertionError("Could not save the simulation workspace capture")
    wait(lambda: w.app_snapshot.state in ("finished", "failed"), 600)
    assert w.app_snapshot.state == "finished", w.app_snapshot.detail
    print("PASS seeded Nav2 mission completed", w.app_snapshot.detail, flush=True)
    w.launch_app.click()
    wait(w.stop_app.isEnabled)
    wait(lambda: len(w.sim_view.plot.trace["fixes"]) >= 3 and any(x or y for _, x, y, _ in w.sim_view.plot.trace["fixes"]), 60)
    print("PASS received changing Nav2 simulation position and FSM over serial", flush=True)
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
        w.field_button.click()
    wait(lambda: w.app_snapshot.state == "stopped" and not w.simulation)
    assert w.radio.ready
    w.sim_button.click()
    assert w.launch_app.isEnabled()
    print("PASS simulation stop confirmed; radio still ready", flush=True)
    w.launch_app.click()
    wait(w.stop_app.isEnabled)
    running = w.app_snapshot.job_id
    w.disconnect()
    time.sleep(5)
    w.connect_radio()
    wait(w.launch_app.isEnabled)
    assert w.app_snapshot.job_id == running and w.app_snapshot.state == "stopped"
    print("PASS disconnect stops job; reconnect returns stopped", flush=True)
finally:
    if w.radio:
        w.disconnect()
    w.close()
