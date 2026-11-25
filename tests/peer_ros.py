"""Non-actuating radio peer for the isolated bench. No navigation or motor topics."""

import hashlib
import io
import os
import socket
import sys

import rclpy
from PIL import Image
from ravenstrike_interfaces.msg import VerificationReport
from rclpy.node import Node
from ugv_comms.gateway import REPORTS
from ugv_mission_interfaces.msg import (
    Evidence,
    GpsTask,
    OperatorDecision,
    SessionControl,
    SessionStatus,
)

assert {name for _, name in socket.if_nameindex()} == {"lo"} and os.environ.get(
    "ROS_DOMAIN_ID"
) == "187"
rclpy.init()
node = Node("gs_field_non_actuating_test")
ns = "/ravenstrike/ugv/"
pub = node.create_publisher(SessionStatus, ns + "session_status", 10)
proof = node.create_publisher(Evidence, ns + "evidence", 10)
reports = node.create_publisher(VerificationReport, REPORTS, 10)
session = active = area = ""
when = lambda: node.get_clock().now().to_msg()
callsign = sys.argv[1]


def status(state, notes):
    pub.publish(
        SessionStatus(
            stamp=when(), session_id=session, state=state, active_mission_id=active, notes=notes
        )
    )


def control(msg):
    global session, area
    if msg.assigned_callsign == callsign and msg.command == "start":
        area = msg.area_of_interest
        session = msg.session_id
        status("standby", "session accepted")


def task(msg):
    global active
    if msg.session_id != session or not session or msg.assigned_callsign != callsign:
        return
    active = msg.mission_id
    status("working", "OK_TO_MOVE_FORWARD")
    kinds = {
        "bench-cycle": ["cycle_complete"],
        "bench-image": ["before", "after"],
        "bench-search": ["not_found"],
    }[area]
    for kind in kinds:
        evidence = Evidence(
            stamp=when(),
            session_id=session,
            mission_id=active,
            evidence_id="bench-" + kind,
            kind=kind,
        )
        if kind != "cycle_complete":
            buf = io.BytesIO()
            Image.new("RGB", (48, 32), "white" if kind == "after" else "gray").save(buf, "JPEG")
            data = buf.getvalue()
            evidence.preview.format = "jpeg"
            evidence.preview.data = data
            evidence.preview_sha256 = hashlib.sha256(data).hexdigest()
        proof.publish(evidence)


def decision(msg):
    kind, decision = {
        "bench-cycle": ("cycle_complete", "approve"),
        "bench-image": ("after", "approve"),
        "bench-search": ("not_found", "retry"),
    }[area]
    if session and (msg.session_id, msg.mission_id, msg.evidence_id, msg.decision) == (
        session,
        active,
        "bench-" + kind,
        decision,
    ):
        reports.publish(
            VerificationReport(
                stamp=when(),
                assigned_callsign=callsign,
                mission_id=active,
                status="blocked",
                notes="processed location; removal unverified",
            )
        )
        status("completed", "bench review received")


node.create_subscription(SessionControl, ns + "session_control", control, 10)
node.create_subscription(GpsTask, ns + "gps_tasks", task, 10)
node.create_subscription(OperatorDecision, ns + "operator_decision", decision, 10)
rclpy.spin(node)
