"""Geographic map overlays. Timestamps are source UTC nanoseconds."""

import math
from collections import deque

PLAN_TTL_NS = 10_000_000_000
GPS_GAP_NS = 3_000_000_000
MAX_FIXES = 4096


def route_points(values):
    if len(values) > 24 or len(values) % 2 or len(values) == 2:
        raise ValueError("Invalid waypoint count")
    points = tuple((int(a) / 1e7, int(b) / 1e7) for a, b in zip(values[::2], values[1::2]))
    if any(not (-90 <= a <= 90 and -180 <= b <= 180) for a, b in points):
        raise ValueError("Invalid waypoint coordinates")
    return points


class MapTracks:
    def __init__(self):
        self.fixes = deque(maxlen=MAX_FIXES)
        self.plan = ()
        self.plan_stamp = -1
        self.segment = 0
        self.fraction = 0.0

    @property
    def ahead(self):
        """Untraveled portion, starting at a point on the plan, not a GPS fix."""
        if self.segment + 1 >= len(self.plan):
            return ()
        a, b = self.plan[self.segment : self.segment + 2]
        lon_delta = (b[1] - a[1] + 180) % 360 - 180
        start = (
            a[0] + self.fraction * (b[0] - a[0]),
            (a[1] + self.fraction * lon_delta + 180) % 360 - 180,
        )
        return (start,) + self.plan[self.segment + 1 :]

    def advance(self, position, previous=None):
        # Only match the current leg. A nearby return leg on a hairpin
        # must not consume the outbound leg or skip a turn.
        while self.segment + 1 < len(self.plan):
            a, b = self.plan[self.segment : self.segment + 2]
            east = 111195 * math.cos(math.radians(a[0]))
            dx = ((b[1] - a[1] + 180) % 360 - 180) * east
            dy = (b[0] - a[0]) * 111195
            x = ((position[1] - a[1] + 180) % 360 - 180) * east
            y = (position[0] - a[0]) * 111195
            length = math.hypot(dx, dy)
            if length > 0.005:
                t = (x * dx + y * dy) / length**2
                # Endpoint capture accounts for modest corner cutting by RPP.
                near_end = math.hypot(x - dx, y - dy) <= min(0.35, length * 0.35)
                if not near_end:
                    if t < 0 or abs(x * dy - y * dx) / length > 0.75:
                        return
                    if t < 1:
                        self.fraction = max(self.fraction, t)
                        return
                    if (t - 1) * length > 0.75:
                        # Two consecutive fixes can straddle several short waypoints.
                        # Require a crossing near this vertex, in route order.
                        if previous is None:
                            return
                        px = ((previous[1] - a[1] + 180) % 360 - 180) * east
                        py = (previous[0] - a[0]) * 111195
                        before = (px * dx + py * dy) / length**2
                        if before >= 1:
                            return
                        cross = (1 - before) / (t - before)
                        if (
                            math.hypot(px + cross * (x - px) - dx, py + cross * (y - py) - dy)
                            > 0.75
                        ):
                            return
            self.segment += 1
            self.fraction = 0.0

    def fix(self, stamp, position):
        if self.fixes and stamp <= self.fixes[-1][0]:
            return
        previous = None
        if self.fixes and stamp - self.fixes[-1][0] <= GPS_GAP_NS:
            previous = self.fixes[-1][1]
        self.fixes.append((stamp, tuple(position[:2])))
        if stamp >= self.plan_stamp and stamp - self.plan_stamp <= PLAN_TTL_NS:
            self.advance(position, previous)

    def replace(self, stamp, points):
        if stamp <= self.plan_stamp:
            return False
        points = tuple(points)
        if points != self.plan:
            self.plan = points
            self.segment = 0
            self.fraction = 0.0
            if self.fixes and abs(stamp - self.fixes[-1][0]) <= GPS_GAP_NS:
                self.advance(self.fixes[-1][1])
        self.plan_stamp = stamp
        return True

    def expire(self, now):
        if self.plan and now - self.plan_stamp > PLAN_TTL_NS:
            self.plan = ()
            return True
        return False
