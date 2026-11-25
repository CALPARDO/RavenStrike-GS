"""Reference geometry for the recorded, fixed Florida Nav2 demonstration."""

SITE = (27.517888, -82.084153)
SITE_ZOOM = 18
DEMO_AREA = "Demo site · simulated mission"
# Centroids of the three rectangles in ugv_mission/config/nav2_demo.yaml, metres.
OBSTACLES_XY = ((2.60, 0.65), (4.00, 3.00), (0.40, 3.35))


def demo_obstacles(area, callsign):
    if area != DEMO_AREA or not callsign.startswith("BENCH-"):
        return ()
    # The bench's toLL service uses this synthetic degrees-per-metre projection.
    return tuple((SITE[0] + y * 1e-5, SITE[1] + x * 1e-5) for x, y in OBSTACLES_XY)
