# RavenStrike ground station

A Qt console for supervised Lynx missions and seeded Jetson simulations. Select **Field** or **Simulation** in the header. The field app runs locally without ROS. The terminal demo remains in the UGV workspace.

## Download a desktop build

Open [Desktop builds](https://github.com/CALPARDO/RavenStrike-GS/actions/workflows/desktop.yml), select a successful run, and download the artifact for your machine. Unpack the artifact and the archive inside it. On Ubuntu 24.04 (x64 or ARM64), run `./RavenStrike-GS/RavenStrike-GS`. On Windows x64, open `RavenStrike-GS.exe`. On Apple silicon, open `RavenStrike-GS.app`. Keep the extracted folder together. Windows and macOS builds are unsigned.

Pushes to main and pull requests test and package all four platforms. Use **Run workflow** to rebuild a selected branch. Pushing a version tag such as `v0.1.0` publishes the tested archives and SHA-256 checksums on [Releases](https://github.com/CALPARDO/RavenStrike-GS/releases). Each runner tests the packaged window, loads the radio configuration and round-trips a GPS task and robot location without opening a serial port. USB radio checks run separately on the bench.

## Run from source on Linux

On the prepared Jetson, open a desktop terminal and run:

```bash
cd ~/ravenstrike/RavenStrike-GS
.venv/bin/python gs.py
```

On a fresh lab machine, install Python 3.12, its venv module, Git, and the USB adapter driver. Give the operator access to its serial port (usually the Linux dialout group). From this checkout run:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python gs.py
```

1. Plug in the GS USB radio. Click the link indicator in the header, select its port, and click **Connect**. **Ready** requires a compatible peer and a heartbeat warm-up. One application owns each radio.
2. Choose **Start session**. Enter the robot callsign, area ID and surveyed home pose in the robot's map frame (metres and heading in degrees). Confirm the UGV acknowledges the session. Both endpoint clocks need to be synchronized.
3. Choose **Mission → Add FOD** or click an empty map location. Enter WGS84 latitude, longitude and ellipsoid altitude. **Preview on map** centers the draft pin. **Send FOD** sends one location. The current form uses hazard level 1 and map yaw 0 radians. The queue and active ID come from UGV status. The map labels GS-submitted coordinates in blue and highlights the reported active target in orange when its coordinates are known. The first fresh UGV fix centers the map unless the operator already chose a view. A dark arrow shows the robot’s true heading, and a dot means only position is available. **Locate UGV** recenters after panning. After 3 s without a fresh fix, or on link loss, the arrow becomes a dashed last-fix marker. The map caption gives the fix time in US Eastern time. The blue dashed line shows the remaining Nav2 route. Fresh GPS fixes retire passed waypoints in order. A new plan replaces the old one, and clearing the teal GPS trail leaves route progress intact. A map outage leaves radio controls available.
4. Choose **Scanning complete** after sending the last task. For each review, match the active ID to the preview. Choose **Approve removal** or **Clean again** for an after-image. For a not-found image choose **Continue** or **Search again**. A cycle-complete token offers **Continue** and records removal as unverified. The UGV decides the outcome and any applicable timeouts.
5. **Pause mission** holds the queue. Navigation cancels its active goal and confirms the base has stopped. An in-progress cleaning stroke or reposition finishes before a pause. **Continue mission** resumes the same session. The main **Stop mission** button immediately withdraws autonomous motion permission, requests cancellation and ends the session once the controller confirms the stop. Unfinished targets are logged as aborted. On a peer without fresh controller capabilities, the primary button uses the existing halt-in-place command. The arrow offers **Return to base**, **Scanning complete**, and **Trigger Lynx base E-stop** if the service is available. Triggering the base E-stop is a separate latched operation. Commission the physical base and arm safety circuits before field use.

## Operator console

Use **View** or the workspace tabs to switch between the map, state machine and activity log. After connecting, use **Mission control** on the right to start the session, add targets, close scanning, review a result. The target register sits below those commands. **Follow vehicle** tracks fresh fixes. Panning turns follow off. **Fit route** frames known target coordinates and the last vehicle position. Resize the inspector by dragging its divider. **View → Restore default layout** resets the workspace.

**Fit route** zooms to the plan, traveled track and known targets, including metre-scale detail. Blue rings mark Nav2 waypoints. The solid teal line and dots show received GNSS antenna fixes. Both overlays use the same geographic projection. Gaps longer than three seconds break the trail. **Clear track** starts a fresh local trail while retaining the plan, target queue and session journal. The display holds the latest 4,096 fixes. Session recordings can replay both overlays.

Select a map marker, target row or target event in Activity to inspect its coordinates and Eastern-time history. The map and inspector give long target IDs short session labels such as **FOD 01**. Hover over a register ID to see its full wire value. Double-click a row to locate it. A radio ACK confirms receipt by the peer bridge. A queue entry, active status or progress report confirms UGV acceptance. Onboard targets may have only an ID.

A new operator decision opens a large review window over the dimmed console. **Review later**, Escape, or the close button defers the decision. Use **Open operator review** in the inspector to return to it. **View original images** opens the received previews at native resolution. Completed tasks retain their available evidence in the register. Previewing or editing a location never sends it. After any coordinate edit, preview again before sending.

During reported field **Bring up system**, click **Record**. Click **Stop recording** at any time, or disconnect to finish the clip. In **Activity**, the GS and UGV checkboxes filter the displayed events independently. The file includes events from both sources regardless of the filters.

After disconnecting, choose **File → Open recording** and select a `radio-*.jsonl.gz` clip or a connection’s `events.jsonl`. Replay shows the recorded field or simulation map on the left and the current event on the right. Drag the timeline, step ±1 or ±5 seconds, or play at 0.5×, 1×, 2×, 5× or 20×. **Export session evidence** includes clips, the journal, the console log and received JPEGs.

## Demo replay and field site

With the radio disconnected, choose **File → Open example recording** for a 1× playback of a recorded USB-to-header radio test. The track, FOD position and mission stages come from the GS journal of a simulated Nav2 mission with real radio traffic. Pause or drag the time slider to inspect operator review and return to base. The replay uses local metres and works offline. Amber diamonds mark the centers of the three fixed Nav2 obstacles. The Florida bench session and its journal replay show the same simulation landmarks on the geographic map. Field sessions require a separate source for sensed obstacles. The markers use no radio bandwidth.

The map opens in **Satellite** at the demo site. Choose **Street** for a road map or **View → Center on demo site** to return to **27.517888, −82.084153**. You can use it before connecting or during a session. It only changes the map view. The surveyed home pose for a real mission is entered separately in **Start session**.

## Run a simulation

Use the matching desktop and gateway wire versions. **Waiting for Jetson** means the GS is waiting for the peer handshake or heartbeat warm-up. **Contract mismatch** means both radios exchanged a handshake and the applications use different topic maps.

1. Connect the radio and select **Simulation**. Leave **Seed** blank for a new mission, or enter 0–4294967295 to replay a layout. Click **Run mission**.
2. Watch **Mission map** for the generated obstacles, assigned locations, selected target and traveled path. **State machine** follows the simulated adapter. The mission runs the field FSM, NavFn planner, RPP controller and guard in an isolated software world with simulated sensors and cleaning.
3. Use **Copy** beside **Seed used** to repeat the layout. **Pause** and **Continue** use the Jetson controller while telemetry and the radio lease stay live. Selecting **Field** during a running mission asks to end the simulation and waits for the matching job to finish. **Stop simulation** ends that job. Losing the radio lease also stops it. Evidence remains in `evidence-run/<job_id>/` on the Jetson.

Both workspaces use wire map v8. Install the matching Jetson supervisor for seeded missions. Changing workspace keeps the radio connected. Field session controls and operator review stay in Field. An active field session keeps the console in Field. A running simulation must finish or be stopped before starting a field session. Named regression cases remain available from the Jetson CLI.

The instruments show reported battery percentage, E-stop input and true heading. A dash means unavailable or stale. **Start session** uses the separately commissioned mission adapter. The idle service reports whether that adapter is online. Session controls and simulation controls have separate lifecycles.

## Field subsystem health

Choose **Subsystem health** in the telemetry bar or **Tools** menu to show or hide the checks on any tab. The compact popup closes with **Close**, Escape, or the window close button. The telemetry bar names any checks needing attention. The panel uses labeled green, yellow, red and gray indications. With a fresh Jetson snapshot, **No data** means a sensor did not supply a usable reading. **No report** means the Jetson has no observation for that check. **Unknown** means the GS lacks a fresh snapshot. A missing supervised core process is a fault after startup grace. Green describes the check listed in the table: the guard is listening, the cleaning server is available, or Nav2 reports active. Those indicators do not certify physical movement or removal. Fresh empty detections are valid perception input. Stale input degrades an observed stream; loss of the Jetson snapshot clears the onboard indications.

The matching gateway replaces the v5 app status (ID 22) with FieldStatus (ID 24). Eight two-bit onboard indications share one unsigned 16-bit field. Existing link, battery and E-stop data supply the remaining table rows. Field status updates arrive at up to 1 Hz. Every five seconds the Jetson includes a compact diagnostic sample with observation ages, forward odometry speed, nearest scan return, base E-stop service availability and the current bringup blocker if known. The table groups the signals by subsystem. Camera monitors the stamped calibration stream. Perception monitors inference observations. Last observed is approximate to one second for these samples. Position and heading share a latest-only update capped at 2 Hz. The session journal records matched command ACK turnaround including GS queueing and retries.

Open **State machine** to see the field adapter’s latest state. The flowchart highlights the reported state. Diamonds show decisions, labeled arrows show their outcomes, and dashed arrows show interrupts. Use the scroll wheel to zoom, drag to pan, and **Fit diagram** for the whole chart. **Follow state** centers incoming states. When the radio first becomes ready without a mission, a slate standby highlight is labeled **Radio standby**. The green highlight identifies a reported onboard state. The controller’s exact state name and the status timestamp appear above it. The status frame samples the FSM at up to 1 Hz, so brief transitions may pass between samples. A stale or missing report clears the highlight. The desktop replay and supervised simulations have their own mission logs. After editing the onboard FSM, regenerate the diagram with `python tools/render_fsm.py <path-to-fsm.yaml>` on a machine with Graphviz. The desktop app includes the rendered diagram.

With both radios on the Jetson, use `tests/bench.sh UGV_CHECKOUT health` for isolated fault indications, or `preview` for a recorded simulated mission at the demo site. Stop the boot-time gateway first, as shown below. UI frames, operator review and console logs are in the GS journal directory. Use a matching v8 checkout for these local tests.

## Logs and recovery

Each connection creates a folder under `logs/` beside the source or, for packaged apps, in the user’s application data directory. The window prints the full path on connection. `console.log` records radio and mission events in UTC. The console displays US Eastern time with automatic daylight-saving adjustments. `events.jsonl` includes elapsed time, typed message fields, evidence IDs, operator requests, local send results, radio ACKs and retry counts. JPEGs are checked against the received SHA-256 and saved as `evidence/<preview_sha256>.jpg`. A transport ACK means the remote bridge received the bytes. Mission acceptance comes from a UGV report.

Each recording is gzip-compressed JSON Lines. Read it with `gzip -dc radio-*.jsonl.gz`. Rows include UTC nanoseconds and elapsed time from recording start. Serial rows contain base64 bytes observed at the GS port, including received fragments, retries and control traffic. A successful write means the local serial port accepted the frame. Failed writes record the attempted bytes. The recorder omits outbound GS heartbeat frames and records a gap if its bounded buffer fills. Enabling recording adds no radio traffic.

Loss of an established radio link locks the session and stops the GS heartbeat. Reconnecting requires an explicit disconnect/connect and creates a fresh transport. Reconcile the unfinished mission at the robot before starting another session. An unresponsive operator window also stops the heartbeat. A delayed or duplicate review message must not authorize a new decision.

## Integration

`radio.py` uses the upstream Python transport and reliability engine pinned in `requirements.txt`. The app reads wire-map v8 settings and registers the checked-in ROS message definitions with the rosbags Jazzy CDR codec. The production UGV gateway and FSM own task acceptance, movement and cleaning. The paired UGV sends up to two latest-only position updates per second, including before a mission. Incoming fixes and headings are saved in the connection journal. Compatibility depends on both the topic ID/type map and the exact message definitions. Update the contract and rerun the codec and bench checks when either changes.

Satellite uses USGS imagery with detailed USDA NAIP aerial photography in the US. The app bundles one 2048 × 1536 site overview from the NAIP service, centered on the demo coordinates at Web Mercator zoom 18. It opens immediately offline. Other uncached imagery uses the laptop’s internet. Street uses OpenStreetMap and supports locations outside the US. Both share a bounded cache. [USGS imagery services](https://www.usgs.gov/faqs/what-are-urls-imagery-services-national-map-and-are-they-cached-or-dynamic) · [OpenStreetMap tile policy](https://operations.osmfoundation.org/policies/tiles/)

## Verify

From the GS checkout:

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q tests
bash tests/check_codec.sh ../RavenStrike-UGV
# On the Jetson with both radios and the idle field service running:
QT_QPA_PLATFORM=offscreen PYTHONPATH=. .venv/bin/python tests/app_peer.py /dev/ttyUSB0
# Give the isolated mission and review benches exclusive use of the header radio.
(
  sudo systemctl stop ravenstrike-field-radio
  trap 'sudo systemctl start ravenstrike-field-radio' EXIT
  bash tests/bench.sh ../RavenStrike-UGV mission && bash tests/bench.sh ../RavenStrike-UGV reviews
)
```

The codec check uses the built UGV ROS environment as a reference. It compares decoded values across implementations because CDR alignment padding can differ. The radio bench requires both lab radios on the Jetson, passwordless access to the existing UGV software sandbox, and exclusive access to the ports. Close the GS before running the bench. It starts the production UGV gateway and a non-actuating ROS test peer in a loopback-only namespace, exercises three review branches using the field GUI handlers, and suspends the gateway to check loss and recovery. It verifies true south on a rotated map, RTK loss, position-only display during TF loss and restored heading. Images, RTK fixes and mission responses are fixtures. No nav, camera, or cleaning hardware starts.

The `mission` bench uses the same isolated radio pair with the actual UGV FSM, Nav2 and command guard. It submits one WGS84 target, waits for the UGV report, closes scanning, reviews synthetic imagery, approves via the field app and checks task completion and return within 0.1 m of the simulated home. It compares the received positions and headings against timestamp-matched simulation truth and saves `location-check.json` and the review screenshot beside the GS journal and prints the location of the Nav2 log and FSM journal. The simulator supplies movement, sensing and cleaning. Run the benches one at a time.

In two local wire v6 bench missions, the two physical radios delivered simulated position and heading at 1.74 and 1.75 Hz. The longest arrival gap was 0.73 s. In a separate review with an 11,409-byte JPEG, the longest observed UI update gap was 1.21 s. All 17 commands across the three runs received matching ACKs. Field range and antenna placement still need an outdoor test.

The 25 November 2026 wire v5 radio mission supplies the desktop demo recording. It covered 10.57 simulated metres and returned to base. The GS received 72 fixes with heading (0.91 Hz). Median fix age was 177 ms, maximum age was 338 ms and maximum heading error against simulation truth was 0.64°. A separate run exercised all three review branches and continued receiving fixes during JPEG transfer. The field launcher test also confirmed stop on disconnect and no restart on reconnect.

Before field use, commission actual receiver settings, site coordinates, reach, physical link loss and manual stop on Lynx. Test installation and USB operation on each additional OS. Local logs and site evidence are gitignored.

If the Windows desktop exits unexpectedly, its native fault trace, when available, is in `%LOCALAPPDATA%/RavenStrike-GS/logs/desktop-crash.log`.

**Reading the live displays.** In **Tools → Subsystem health**, expand a category to see signal status, value and elapsed time since its last observation. A dash means the GS has no timestamp for that signal. The Simulation workspace shows the selected and waiting FOD IDs in the order reported by the Jetson mission FSM. The supervisor sends queue changes with the existing simulation preview and repeats the snapshot every six seconds. An older peer can still report position and queue length. The table reads “Awaiting a queue report” until it receives the IDs.

When simulation telemetry pauses or a job ends, the map and flowchart retain the last observed pose and state in gray. These are historical observations. Connecting to a new radio session or receiving a new job ID clears the previous run. **Stop mission** is available for an accepted field session in standby, working, returning, or paused. The GS permits another request after three seconds without confirmation. Simulation has its own **Stop simulation** control.

Open **Tools → Run Tests…** for individual commissioning checks or a read-only batch. Expand any result to see what was measured. [Commissioning checks](docs/COMMISSIONING.md)
