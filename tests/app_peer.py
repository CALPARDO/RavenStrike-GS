"""Exercise the field launcher using a USB radio or the paired Linux PTY fixture."""

import sys
import time

from PySide6.QtWidgets import QApplication

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
    wait(w.launch_app.isEnabled)
    assert w.app_snapshot.state in ("idle", "stopped", "finished", "failed")
    assert not w.app_snapshot.mission_adapter_online
    assert [v.text() for v in w.instruments] == ["—"] * 3
    print("Journal:", w.journal.folder, flush=True)
    print("PASS matched serial peers, fresh idle status, absent sensors unknown", flush=True)
    w.app_choice.setCurrentIndex(w.app_choice.findData("scenario"))
    w.scenario.setCurrentText("nav-rejected")
    w.launch_app.click()
    wait(w.stop_app.isEnabled)
    wait(lambda: w.sim_view.stamp > 0 and w.tabs.currentWidget() is w.sim_view, 45)
    assert w.sim_view.plot.trace["fixes"] and w.runway.robot is None
    wait(lambda: w.app_snapshot.state in ("finished", "failed"), 120)
    assert w.app_snapshot.state == "finished", w.app_snapshot.detail
    print("PASS actual nav-rejected scenario completed", w.app_snapshot.detail, flush=True)
    w.app_choice.setCurrentIndex(w.app_choice.findData("nav2-demo"))
    w.launch_app.click()
    wait(w.stop_app.isEnabled)
    wait(lambda: len(w.sim_view.plot.trace["fixes"]) >= 3 and any(x or y for _, x, y, _ in w.sim_view.plot.trace["fixes"]), 60)
    print("PASS received changing Nav2 simulation position and FSM over serial", flush=True)
    assert w.stop_app.action in w.stop_menu.actions()
    w.stop_app.action.trigger()
    wait(lambda: w.app_snapshot.state == "stopped")
    assert w.radio.ready and w.launch_app.isEnabled()
    print("PASS header simulation stop confirmed; radio still ready", flush=True)
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
