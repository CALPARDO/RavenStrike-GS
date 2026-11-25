"""One live position per radio connection. Times are UTC on both endpoints."""

import time


class LocationTrack:
    def __init__(self):
        self.connected = time.time_ns()
        self.stamp = self.expires = 0
        self.position = None

    def accept(self, msg):
        stamp = msg.stamp.sec * 10**9 + msg.stamp.nanosec
        age = time.time_ns() - stamp
        if (
            not 0 <= msg.stamp.nanosec < 10**9
            or stamp < self.connected
            or stamp <= self.stamp
            or not -200_000_000 <= age <= 3_000_000_000
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
            and -200_000_000 <= time.time_ns() - self.stamp <= 3_000_000_000
        ):
            return self.position
        return None
