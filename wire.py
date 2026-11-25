"""CDR payloads for the UGV v8 radio map."""

import math
import time
from pathlib import Path

from rosbags.typesys import Stores, get_types_from_msg, get_typestore

WIRE_MAP_VERSION = 8
CONTRACT = Path(__file__).resolve().parent / "contract"


class Codec:
    def __init__(self):
        self.store = get_typestore(Stores.ROS2_JAZZY)
        custom = {}
        for path in sorted(CONTRACT.glob("*.msg")):
            package = {
                "GeoPoint": "geographic_msgs",
                "VerificationReport": "ravenstrike_interfaces",
            }.get(path.stem, "ugv_mission_interfaces")
            custom.update(get_types_from_msg(path.read_text(), f"{package}/msg/{path.stem}"))
        self.store.register(custom)
        self.types = {
            name.split("/")[-1]: cls
            for name, cls in self.store.types.items()
            if name.startswith(
                (
                    "ugv_mission_interfaces/",
                    "geometry_msgs/",
                    "builtin_interfaces/",
                    "ravenstrike_interfaces/",
                    "std_msgs/",
                    "geographic_msgs/",
                )
            )
        }
        self.types["Time"] = self.store.types["builtin_interfaces/msg/Time"]

    def stamp(self):
        return self.types["Time"](*divmod(time.time_ns(), 10**9))

    def control(self, session, callsign, area, home, command):
        x, y, yaw = home
        if not all(map(math.isfinite, home)):
            raise ValueError("Enter finite map coordinates and heading for home.")
        make = self.types
        pose = make["Pose"](
            make["Point"](x, y, 0.0),
            make["Quaternion"](0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2)),
        )
        stamped = make["PoseStamped"](make["Header"](self.stamp(), "map"), pose)
        return make["SessionControl"](
            self.stamp(), session, callsign, area, command, "supervised", stamped
        )

    def gps(self, session, mission, callsign, lat, lon, alt):
        if not (-90 <= lat <= 90 and -180 <= lon <= 180 and math.isfinite(alt)):
            raise ValueError("Invalid WGS84 latitude, longitude, or ellipsoid altitude.")
        return self.types["GpsTask"](
            self.stamp(), session, mission, callsign, 1, self.types["GeoPoint"](lat, lon, alt), 0.0
        )

    def encode(self, msg):
        return bytes(self.store.serialize_cdr(msg, msg.__msgtype__))

    def decode(self, data, name):
        return self.store.deserialize_cdr(data, name)
