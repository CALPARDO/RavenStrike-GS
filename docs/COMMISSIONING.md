# Commissioning checks

Open **Tools → Run Tests…**. Run a single check, a category or **Run all safe tests**. Expand a test to read the observation. The window shows elapsed duration and the Eastern time of execution. **Cancel** or closing the window stops the batch. The GS writes results to the connected session’s `events.jsonl` and `console.log`.

Remote checks always observe the Jetson’s field domain, including while the Simulation tab is open. Keep the robot immobilized during initial commissioning. These checks read data and query services. They issue no base motion, manipulation, E-stop release, session-start or radio configuration commands.

| Category | Evidence for a pass |
| --- | --- |
| Ground station | The installed CDR codec round-trips a command; a compressed test journal reopens with the right timestamped record; the field supervisor answers a unique radio request. |
| Jetson and network | Available RAM and workspace free space each exceed or equal the 256 MiB diagnostic floor. The Jetson receives an ICMP reply from the configured router address. |
| Lynx and localization | New stamped battery and odometry are valid, the E-stop input reports its actual state, GNSS health is true, the scan contains in-range returns and the live map-to-base transform is finite and fresh. The RTK map alignment check takes a screened fix from the onboard receiver monitor, checks the FromLL→ToLL horizontal round trip (≤0.05 m) and compares the projected XY with the antenna TF at the same capture stamp (≤0.25 m). Use a stationary antenna for initial commissioning. Absolute accuracy still needs a surveyed reference. An engaged E-stop passes the **input** check and still inhibits driving. |
| Camera and perception | The onboard camera produces a synchronized rectified BGR8 image, XYZ image with a finite forward sample, and valid intrinsics. The FSM independently receives a fresh completed inference. An empty inference is valid. |
| Mission and manipulation | The FSM reports a recognized state and unique queue IDs, the guard publishes a fresh planar command, Nav2 answers the lifecycle activity query, all six UR joints report finite positions and the cleaning action returns the expected unknown status for an unissued goal ID. |

**Hardware Unavailable** means no fresh field sample or endpoint arrived, or the radio could not reach the Jetson. Check power, the driver, namespace and network. **Failed** means data were invalid, a health input reported a fault, the link dropped mid-check, or the paired supervisor failed to return a result. **Skipped** covers cancellation and an unconfigured optional router check. A software publisher on the field domain can satisfy a telemetry check. Check the actual publishing driver during hardware commissioning.

Set `FIELD_ROUTER_IP` to the deployed RUTX50 LAN IPv4 address in the Jetson service environment. If the UR joint state broadcaster is namespaced, set `FIELD_ARM_JOINT_TOPIC` to its topic. Face the ZED toward textured material in operating range for the depth check. Use a known stationary target for the navigation scan. Select the production field profile for Lynx E-stop verification.

The supervisor allows one outstanding check. Each sensor check has four seconds to obtain a fresh sample. The GS allows ten seconds for the request and reply. The camera check inspects samples onboard and retains only frame identifiers and dimensions. The reply includes a short result in the existing status frame; images and joint streams stay onboard. An older supervisor that ignores the request times out.

## Software regression

On Linux with `socat`, a built Jazzy UGV checkout and the GS virtual environment:

```bash
bash tests/bench.sh /path/to/RavenStrike-UGV diagnostics
```

This bench runs the actual GS, serial bridge protocol, ROS gateway and field supervisor in a loopback namespace. Its paired virtual serial ports have hardware flow control disabled. It checks valid desktop and Jetson results alongside explicit unavailability on an empty field domain. A second phase injects healthy and faulty ROS telemetry and exercises the same path. Physical RF, router, ZED, UR and Lynx commissioning require their respective devices.

For interpretation of the source interfaces, see the [Stereolabs depth API](https://docs.stereolabs.com/docs/development/zed-sdk/modules/depth-sensing/using-the-api), [robot_localization GNSS integration](https://github.com/cra-ros-pkg/robot_localization/blob/rolling-devel/doc/integrating_gps.rst) and the [Universal Robots ROS 2 driver](https://github.com/UniversalRobots/Universal_Robots_ROS2_Driver/tree/3.9.0).
