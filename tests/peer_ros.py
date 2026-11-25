"""Non-actuating radio peer for the isolated bench. No navigation or motor topics."""

import hashlib
import io
import os
import random
import socket
import sys

import rclpy
from geometry_msgs.msg import TransformStamped
from PIL import Image
from ravenstrike_interfaces.msg import VerificationReport
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from robot_localization.srv import ToLL
from sensor_msgs.msg import NavSatFix, NavSatStatus
from std_msgs.msg import Bool
from tf2_ros import TransformBroadcaster
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


# Rotated map: its +X points south even with identity base orientation.
fix_pub = node.create_publisher(NavSatFix, "/ravenstrike/gnss/fix", qos_profile_sensor_data)
health = node.create_publisher(Bool, "/ravenstrike/gnss/healthy", 10)
tf = TransformBroadcaster(node)


def to_ll(req, reply):
    reply.ll_point.latitude = 40 - req.map_point.x / 111035
    reply.ll_point.longitude = -83 + req.map_point.y / 85394
    return reply


link_healthy = False


def link_health(msg):
    global link_healthy
    link_healthy = msg.data


node.create_subscription(Bool, "/ravenstrike/radio/healthy", link_health, 10)


def sensors():
    # The bench client uses the first fix as its cue to start the review exercise.
    if (
        not link_healthy
        or not control_sub.get_publisher_count()
        or not pub.get_subscription_count()
    ):
        return
    healthy = area != "bench-cycle"
    health.publish(Bool(data=healthy))
    if healthy:
        fix = NavSatFix(latitude=40.0, longitude=-83.0)
        fix.status.status = NavSatStatus.STATUS_GBAS_FIX
        fix.header.stamp = when()
        fix_pub.publish(fix)
    if area != "bench-image":
        pose = TransformStamped()
        pose.header.stamp = when()
        pose.header.frame_id, pose.child_frame_id = "map", "lynx/base_link"
        pose.transform.rotation.w = 1.0
        tf.sendTransform(pose)


node.create_timer(0.1, sensors)
node.create_service(ToLL, "/lynx/toLL", to_ll)


def status(state, notes):
    pub.publish(
        SessionStatus(
            stamp=when(),
            session_id=session,
            state=state,
            active_mission_id=active,
            notes=notes,
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
            if kind == "after":
                # A field-sized JPEG exercises scheduling beside telemetry and heartbeats.
                pixels = random.Random(42).randbytes(136 * 136 * 3)
                Image.frombytes("RGB", (136, 136), pixels).save(buf, "JPEG", quality=72)
                assert 11000 <= buf.tell() <= 12288
            else:
                Image.new("RGB", (48, 32), "gray").save(buf, "JPEG")
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


control_sub = node.create_subscription(SessionControl, ns + "session_control", control, 10)
node.create_subscription(GpsTask, ns + "gps_tasks", task, 10)
node.create_subscription(OperatorDecision, ns + "operator_decision", decision, 10)
rclpy.spin(node)
