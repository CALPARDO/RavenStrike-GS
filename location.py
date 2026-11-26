"""One live position per radio connection. Times are UTC on both endpoints."""

import time


# The UGV admits heartbeats up to one second old. Allow the same clock
# difference on received telemetry. Commands are validated separately onboard.
def telemetry_time_ok(stamp_ns, now_ns):
    return -1_000_000_000 <= now_ns - stamp_ns < 3_000_000_000


class LocationTrack:
    def __init__(self):
        self.connected = time.time_ns()
        self.stamp = self.expires = 0
        self.position = None

    def accept(self, msg):
        stamp = msg.stamp.sec * 10**9 + msg.stamp.nanosec
        now = time.time_ns()
        age = now - stamp
        if (
            not 0 <= msg.stamp.nanosec < 10**9
            or stamp < self.connected
            or stamp <= self.stamp
            or not telemetry_time_ok(stamp, now)
            or not -900_000_000 <= msg.latitude_e7 <= 900_000_000
            or not -1_800_000_000 <= msg.longitude_e7 <= 1_800_000_000
            or not (0 <= msg.heading_cdeg < 36000 or msg.heading_cdeg == 65535)
        ):
            return False
        self.stamp = stamp
        self.expires = time.monotonic_ns() + 3_000_000_000 - max(0, age)
        self.position = (
            msg.latitude_e7 / 1e7,
            msg.longitude_e7 / 1e7,
            None if msg.heading_cdeg == 65535 else msg.heading_cdeg / 100,
        )
        return True

    def current(self):
        if (
            time.monotonic_ns() < self.expires
            and telemetry_time_ok(self.stamp, time.time_ns())
        ):
            return self.position
        return None
