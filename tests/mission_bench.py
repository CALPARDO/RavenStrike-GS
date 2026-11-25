"""Run via bench.sh mission in the radio-only namespace. Only the Qt GS issues orders."""

import json
import math
import os
import signal
import socket
import statistics
import subprocess
import sys
from datetime import datetime
from itertools import pairwise
from pathlib import Path

from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import NavSatFix, NavSatStatus
from std_msgs.msg import Bool
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
config = prepare(directory)
# A single submitted target is sufficient to exercise the field console.
config["targets"] = {"east": config["targets"]["east"]}
preview = os.environ.get("GS_BENCH_PREVIEW") == "1"
sys.path.insert(0, gs)
from demo_site import OBSTACLES_XY, SITE

lat0, lon0 = SITE if preview else (40.0, -83.0)
if preview:
    centers = [
        (round((x1 + x2) / 2, 5), round((y1 + y2) / 2, 5)) for x1, y1, x2, y2 in config["obstacles"]
    ]
    assert tuple(centers) == OBSTACLES_XY, "GS obstacle points differ from the Nav2 demo world"


class SiteSimulation(NavigationSimulation):
    # Same synthetic projection as the mission fixture, translated to the selected site.
    def to_ll(self, request, response):
        response.ll_point.latitude = lat0 + request.map_point.y * 1e-5
        response.ll_point.longitude = lon0 + request.map_point.x * 1e-5
        response.ll_point.altitude = 200.0 + request.map_point.z
        return response

    def from_ll(self, request, response):
        response.map_point.x = (request.ll_point.longitude - lon0) / 1e-5
        response.map_point.y = (request.ll_point.latitude - lat0) / 1e-5
        response.map_point.z = request.ll_point.altitude - 200.0
        return response


rig = SiteSimulation(
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
fix_pub = rig.peer.create_publisher(NavSatFix, "/ravenstrike/gnss/fix", qos_profile_sensor_data)
health = rig.peer.create_publisher(Bool, "/ravenstrike/gnss/healthy", 10)


truth = {}


def simulated_fix():
    x, y, yaw = rig.robot
    fix = NavSatFix(latitude=lat0 + y * 1e-5, longitude=lon0 + x * 1e-5)
    fix.header.stamp = rig.peer.get_clock().now().to_msg()
    # Ground truth at the acquisition stamp; the GS never sees this table.
    stamp = fix.header.stamp.sec * 10**9 + fix.header.stamp.nanosec
    true_heading = (
        math.degrees(math.atan2(math.cos(math.radians(lat0)) * math.cos(yaw), math.sin(yaw))) % 360
    )
    truth[stamp] = (fix.latitude, fix.longitude, true_heading)
    fix.status.status = NavSatStatus.STATUS_GBAS_FIX
    health.publish(Bool(data=True))
    fix_pub.publish(fix)


rig.peer.create_timer(0.1, simulated_fix, callback_group=rig.group)
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
            journal_path = directory / "gs-journal.txt"
            env["GS_BENCH_JOURNAL"] = str(journal_path)
            subprocess.run(
                [
                    f"{gs}/.venv/bin/python",
                    f"{gs}/tests/bench_client.py",
                    callsign,
                    str(lat0 + y * 1e-5),
                    str(lon0 + x * 1e-5),
                ],
                env=env,
                check=True,
                timeout=300,
            )
            journal = Path(journal_path.read_text())
            samples = [
                r
                for line in (journal / "events.jsonl").read_text().splitlines()
                if (r := json.loads(line))["event"] == "location"
            ]
            if preview:
                routes = [
                    r
                    for line in (journal / "events.jsonl").read_text().splitlines()
                    if (r := json.loads(line))["event"] == "plan"
                ]
                sent = [r["points"] for r in routes if r["points"]]
                projected = [
                    [
                        (round((lat0 + y * 1e-5) * 1e7), round((lon0 + x * 1e-5) * 1e7))
                        for x, y in p["points"]
                    ]
                    for p in rig.plans
                ]
                assert sent and any(not r["points"] for r in routes)
                for route in sent:
                    points = [(round(lat * 1e7), round(lon * 1e7)) for lat, lon in route]
                    assert 2 <= len(points) <= 12
                    assert any(
                        points[0] == plan[0]
                        and points[-1] == plan[-1]
                        and all(pt in plan for pt in points)
                        for plan in projected
                    )
                print(
                    "PASS: received Nav2 waypoint snapshots match source paths and projection; point counts",
                    [len(r) for r in sent],
                    flush=True,
                )
            stamps = [r["source_ns"] for r in samples]
            assert len(stamps) >= 10 and stamps == sorted(set(stamps))
            assert len({tuple(r["pose"][:2]) for r in samples}) >= 5
            errors, delays = [], []
            for row in samples:
                stamp = row["source_ns"]
                lat, lon, heading = row["pose"]
                tx_lat, tx_lon, tx_heading = truth[stamp]
                assert abs(lat - tx_lat) <= 5.01e-8 and abs(lon - tx_lon) <= 5.01e-8
                if heading is not None:
                    errors.append(abs((heading - tx_heading + 180) % 360 - 180))
                delays.append(datetime.fromisoformat(row["utc"]).timestamp() - stamp / 1e9)
            assert len(errors) >= 0.9 * len(samples) and max(errors) < 10
            assert max(delays) < 3.0
            arrivals = [datetime.fromisoformat(row["utc"]).timestamp() for row in samples]
            result = {
                "samples": len(samples),
                "headings": len(errors),
                "max_heading_error_deg": max(errors),
                "max_source_age_s": max(delays),
                "median_source_age_s": statistics.median(delays),
                "rate_hz": (len(samples) - 1) / (arrivals[-1] - arrivals[0]),
                "longest_gap_s": max(b - a for a, b in pairwise(arrivals)),
            }
            (journal / "location-check.json").write_text(json.dumps(result, indent=2) + "\n")
            print(
                "PASS: GS fixes compared with moving simulation truth:",
                result,
                flush=True,
            )
            assert result["rate_hz"] >= 1.6 and result["longest_gap_s"] < 3, result
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
