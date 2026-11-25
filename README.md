# RavenStrike field ground station

A Python and Qt USB-radio console for one supervised Lynx session. The field app runs locally without ROS. The terminal demo remains in the UGV workspace.

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

1. Plug in the GS USB radio. Select its port and click **Connect**. **Ready** requires a compatible peer and a heartbeat warm-up. One application owns each radio.
2. Choose **Start session**. Enter the robot callsign, area ID and surveyed home pose in the robot's map frame (metres and heading in degrees). Confirm the UGV acknowledges the session. Both endpoint clocks need to be synchronized.
3. Enter WGS84 latitude, longitude and ellipsoid altitude. **Preview on map** loads OpenStreetMap tiles using the laptop's internet. **Send FOD** sends one location. The current form uses hazard level 1 and map yaw 0 radians. The queue and active ID come from UGV status. The map labels GS-submitted coordinates in blue and highlights the reported active target in orange when its coordinates are known. The first fresh UGV fix centers an empty map. A dark arrow shows the robot’s true heading, and a dot means only position is available. **Locate UGV** recenters after panning. The marker expires after 3 s or on link loss. Nav2 paths stay onboard. A map outage leaves radio controls available.
4. Choose **Scanning complete** after sending the last task. For each review, match the active ID to the preview. Choose **Approve removal** or **Clean again** for an after-image. For a not-found image choose **Continue** or **Search again**. A cycle-complete token offers **Continue** and records removal as unverified. The UGV decides the outcome and any applicable timeouts.
5. Wait for the UGV's terminal session status. **Abort to base** requests a mission abort through the FSM. Physical emergency stop remains a platform function.

## Operator console

The console opens in light mode. The mission map supports drag to pan, scroll or **+/−** to zoom, and coordinate preview before connecting. Double-click a target in **Targets** to center its known coordinates. Queue positions and the active ID come from the UGV. A target discovered onboard may have only an ID on the radio link.

Select a row in **Targets** to see that FOD’s timeline in UTC. A radio ACK confirms receipt by the peer bridge. A UGV queue entry, active status or progress report confirms acceptance. Operator decisions and final UGV outcomes have separate entries. The register keeps onboard targets available for review after they leave the queue.

The footer shows the build ID and wire map version. **Copy diagnostics** copies those values, the selected port, link status and log folder for troubleshooting. Packaged builds use the Git commit from the build job.

**Review** opens when evidence for the active target is ready. **View original images** shows the received JPEG previews at their native resolution. **Activity** timestamps events in UTC and **Open folder** opens the connection journal. The radio indicator distinguishes connecting, a missing port, a contract mismatch, and loss of an established link.

## Logs and recovery

Each connection creates a folder under `logs/` beside the source or, for packaged apps, in the user’s application data directory. The window prints the full path on connection. `console.log` records radio and mission events in UTC. `events.jsonl` includes elapsed time, typed message fields, evidence IDs, operator requests, local send results, radio ACKs and retry counts. JPEGs are checked against the received SHA-256 and saved as `evidence/<preview_sha256>.jpg`. A transport ACK means the remote bridge received the bytes. Mission acceptance comes from a UGV report.

Loss of an established radio link locks the session and stops the GS heartbeat. Reconnecting requires an explicit disconnect/connect and creates a fresh transport. Reconcile the unfinished mission at the robot before starting another session. An unresponsive operator window also stops the heartbeat. A delayed or duplicate review message must not authorize a new decision.

## Integration

`radio.py` uses the upstream Python transport and reliability engine pinned in `requirements.txt`. The app reads wire-map v4 settings and registers the checked-in ROS message definitions with the rosbags Jazzy CDR codec. The production UGV gateway and FSM own task acceptance, movement and cleaning. The paired UGV sends one latest-only position per second at most, including before a mission. Incoming fixes and headings are saved in the connection journal. Compatibility depends on both the topic ID/type map and the exact message definitions. Update the contract and rerun the codec and bench checks when either changes.

The online map uses the OpenStreetMap standard tile service. Only visible tiles are fetched. Application cache storage is bounded. Internet is required for uncached tiles. [Tile usage policy](https://operations.osmfoundation.org/policies/tiles/)

## Verify

From the GS checkout:

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q tests
bash tests/check_codec.sh ../RavenStrike-UGV
bash tests/bench.sh ../RavenStrike-UGV
bash tests/bench.sh ../RavenStrike-UGV mission
```

The codec check uses the built UGV ROS environment as a reference. It compares decoded values across implementations because CDR alignment padding can differ. The radio bench requires both lab radios on the Jetson, passwordless access to the existing UGV software sandbox, and exclusive access to the ports. Close the GS before running the bench. It starts the production UGV gateway and a non-actuating ROS test peer in a loopback-only namespace, exercises three review branches using the field GUI handlers, and suspends the gateway to check loss and recovery. It verifies true south on a rotated map, RTK loss, position-only display during TF loss and restored heading. Images, RTK fixes and mission responses are fixtures. No nav, camera, or cleaning hardware starts.

The `mission` bench uses the same isolated radio pair with the actual UGV FSM, Nav2 and command guard. It submits one WGS84 target, waits for the UGV report, closes scanning, reviews synthetic imagery, approves via the field app and checks task completion and return within 0.1 m of the simulated home. It compares the received positions and headings against timestamp-matched simulation truth and saves `location-check.json` and the review screenshot beside the GS journal and prints the location of the Nav2 log and FSM journal. The simulator supplies movement, sensing and cleaning. Run the benches one at a time.

On the 25 November 2026 Jetson radio bench, the GS received 65 timestamped fixes
with heading during the simulated mission, at 0.91 Hz. Median fix age on receipt
was 187 ms and the maximum was 285 ms. All received coordinates matched the
simulated GNSS input to wire precision. Maximum bearing error against the
simulated body orientation was 0.49°. A separate review run continued receiving
fixes during an 11,409-byte JPEG transfer.

Before field use, commission actual receiver settings, site coordinates, reach, physical link loss and manual stop on Lynx. Test installation and USB operation on each additional OS. Local logs and site evidence are gitignored.
