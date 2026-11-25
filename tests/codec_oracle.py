"""Generate fixtures using the field codec or the installed ROS implementation."""

import sys
from pathlib import Path

root = Path(sys.argv[2])
if sys.argv[1] == "field":
    from wire import Codec

    codec = Codec()
    for path in root.glob("*.cdr"):
        name = path.stem.replace("__", "/")
        value = codec.decode(path.read_bytes(), name)
        path.with_suffix(".roundtrip").write_bytes(codec.encode(value))
    for msg in [
        codec.stamp(),
        codec.control("s-23", "UGV-1", "test", (1.125, -3.0, 1.5708), "start"),
        codec.gps("s-23", "fod-1", "UGV-1", 40.0, -82.99997, 201.5),
        codec.types["OperatorDecision"](codec.stamp(), "s-23", "fod-1", "ev123", "approve"),
    ]:
        (root / (msg.__msgtype__.replace("/", "__") + ".field")).write_bytes(codec.encode(msg))
else:
    from ravenstrike_interfaces.msg import VerificationReport
    from rclpy.serialization import deserialize_message, serialize_message
    from rosidl_runtime_py.utilities import get_message
    from ugv_mission_interfaces.msg import Evidence, RobotLocation, SessionStatus

    if sys.argv[1] == "generate":
        records = [
            RobotLocation(latitude_e7=-123456789, longitude_e7=1799999999, heading_cdeg=65535),
            SessionStatus(
                session_id="odd-length", state="working", queued_mission_ids=["a", "FOD-3"]
            ),
            VerificationReport(
                mission_id="fod", notes="évidence", visited_checkpoint_ids=["one", "two"]
            ),
        ]
        image = Evidence(kind="after", session_id="s", evidence_id="xyz")
        image.image_stamp.sec = 123
        image.search_center.x = -17.0
        image.preview.data = bytes(range(251))
        image.preview.format = "jpeg"
        records.append(image)
        for value in records:
            name = value.__class__.__module__.split(".")[0] + "__msg__" + type(value).__name__
            (root / (name + ".cdr")).write_bytes(serialize_message(value))
    else:
        for path in root.glob("*.roundtrip"):
            cls = get_message(path.stem.replace("__", "/"))
            assert deserialize_message(path.read_bytes(), cls) == deserialize_message(
                path.with_suffix(".cdr").read_bytes(), cls
            )
        for path in root.glob("*.field"):
            cls = get_message(path.stem.replace("__", "/"))
            raw = path.read_bytes()
            value = deserialize_message(raw, cls)
            assert deserialize_message(serialize_message(value), cls) == value, cls
            if hasattr(value, "location"):
                assert value.location.altitude == 201.5
            if hasattr(value, "home"):
                assert value.home.pose.position.x == 1.125
            if hasattr(value, "decision"):
                assert value.evidence_id == "ev123"
