"""Run via bench.sh mission in the radio-only namespace. Only the Qt GS issues orders."""

import math
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

from ugv_mission.nav2_demo import (
    NavigationSimulation,
    prepare,
    verify_shared_controller,
    wait_for_navigation,
)

if {name for _, name in socket.if_nameindex()} != {"lo"} or os.environ.get(
    "ROS_DOMAIN_ID"
) != "187":
    raise RuntimeError("Run through the isolated radio bench")

gs, callsign, work = sys.argv[1:]
directory = Path(work)
config = prepare(directory, seed=42, max_fod=2)
rig = NavigationSimulation(
    {
        "callsign": callsign,
        "require_radio_link": True,
        "fsm_config": str(directory / "fsm_used.yaml"),
        "evidence_directory": work,
        "heartbeat_timeout": 3.0,
        "state_timeout": 1.0,
        "capture_timeout": 5.0,
        "gps_timeout": 2.0,
        "image_topic": "/fod/observation_image",
    },
    config,
)
rig.beat_enabled = False
rig.scenario = "nominal"  # One synthetic cleaning cycle for this review.
try:
    with (directory / "nav2.log").open("w") as log:
        nav = subprocess.Popen(
            ["ros2", "launch", str(directory / "navigation.launch.py")],
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            wait_for_navigation(rig, nav)
            verify_shared_controller(rig, directory)
            x, y, _ = next(iter(config["targets"].values()))
            env = os.environ.copy()
            for key in ("AMENT_PREFIX_PATH", "LD_LIBRARY_PATH"):
                env.pop(key, None)
            env["PYTHONPATH"] = gs
            subprocess.run(
                [
                    f"{gs}/.venv/bin/python",
                    f"{gs}/tests/bench_client.py",
                    callsign,
                    str(40 + y * 1e-5),
                    str(-83 + x * 1e-5),
                ],
                env=env,
                check=True,
                timeout=300,
            )
            assert rig.adapter.session_state == "completed"
            assert rig.commands and rig.plans and len(rig.clean_goals) == 1
            assert rig.travel > 1.0 and math.hypot(*rig.robot[:2]) < 0.1 and not rig.collisions
            print(
                f"PASS: production FSM and Nav2 returned home; {rig.travel:.2f} simulated metres, "
                f"{len(rig.plans)} plans, {rig.commands} simulated drive commands.",
                flush=True,
            )
        finally:
            if nav.poll() is None:
                os.killpg(nav.pid, signal.SIGINT)
                try:
                    nav.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(nav.pid, signal.SIGKILL)
                    nav.wait()
finally:
    rig.close()
