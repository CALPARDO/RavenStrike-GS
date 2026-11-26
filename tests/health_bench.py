"""Isolated field-status HIL fixture. Step selection is local; readings traverse the radio."""

import os
import socket
import sys
import time
from pathlib import Path

assert os.environ["ROS_DOMAIN_ID"] == "187" and {s for _, s in socket.if_nameindex()} == {"lo"}
step_file = Path(sys.argv[2]) / "health-step"
if sys.argv[1] == "source":
    import rclpy
    from sensor_msgs.msg import BatteryState
    from std_msgs.msg import Bool

    rclpy.init()
    node = rclpy.create_node("bench_health_inputs")
    gnss = node.create_publisher(Bool, "/ravenstrike/gnss/healthy", 1)
    battery = node.create_publisher(BatteryState, "/lynx/battery/battery_status", 1)
    stop = node.create_publisher(Bool, "/lynx/hardware/e_stop", 1)
    try:
        while rclpy.ok():
            rclpy.spin_once(node, timeout_sec=0.1)
            if not step_file.exists():
                continue
            step = step_file.read_text().strip()
            if step in ("healthy", "fault"):
                bad = step == "fault"
                gnss.publish(Bool(data=not bad))
                stop.publish(Bool(data=bad))
                msg = BatteryState(percentage=0.15 if bad else 0.9)
                msg.header.stamp = node.get_clock().now().to_msg()
                battery.publish(msg)
    finally:
        node.destroy_node()
        rclpy.try_shutdown()
else:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from gs import Window

    app = QApplication([])
    w = Window()
    w.resize(1320, 844)
    w.show()
    w.port.setCurrentText("/dev/ttyUSB0")
    w.connect_radio()

    def check(gnss, battery, estop):
        until = time.monotonic() + 15
        while time.monotonic() < until:
            app.processEvents()
            actual = tuple(
                w.health_panel.rows[name].data(Qt.ItemDataRole.UserRole)
                for name in ("GNSS", "Battery", "E-stop")
            )
            if w.app_fresh() and actual == (gnss, battery, estop):
                return
            time.sleep(0.05)
        raise AssertionError((actual, w.job_label.text()))

    try:
        check(0, 0, 0)
        step_file.write_text("healthy")
        check(1, 1, 1)
        step_file.write_text("fault")
        check(3, 2, 3)
        assert all(name in w.health_summary.text() for name in ("E-stop", "GNSS", "Battery"))
        w.health_details.click()
        assert w.health_panel.isVisible()
        assert w.health_panel.window().grab().save(str(w.journal.folder / "bench-faults.png"))
        step_file.write_text("silent")
        check(2, 0, 0)
        step_file.write_text("healthy")
        check(1, 1, 1)
        print(
            "PASS unknown → healthy → GNSS fault / low battery / E-stop active → stale → recovered over serial",
            w.journal.folder,
        )
    finally:
        w.disconnect()
        w.close()
