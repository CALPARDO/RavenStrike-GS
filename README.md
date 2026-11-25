# RavenStrike field ground station

A Python and Qt USB-radio console for one supervised Lynx session. The field app runs locally without ROS. The terminal demo remains in the UGV workspace.

## Run on Linux

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
3. Enter WGS84 latitude, longitude and ellipsoid altitude. **Preview on map** loads OpenStreetMap tiles using the laptop's internet. **Send FOD** sends one location. The current form uses hazard level 1 and map yaw 0 radians. The queue and active ID come from UGV status. The map labels the GS-submitted coordinates. A map outage leaves radio controls available.
4. Choose **Scanning complete** after sending the last task. For each review, match the active ID to the preview. Choose **Approve removal** or **Clean again** for an after-image. For a not-found image choose **Continue** or **Search again**. A cycle-complete token offers **Continue** and records removal as unverified. The UGV decides the outcome and any applicable timeouts.
5. Wait for the UGV's terminal session status. **Abort to base** requests a mission abort through the FSM. Physical emergency stop remains a platform function.

## Logs and recovery

Each connection creates a folder under `logs/`. `console.log` records radio and mission events in UTC. `events.jsonl` includes elapsed time, typed message fields, evidence IDs, operator requests, local send results, radio ACKs and retry counts. JPEGs are checked against the received SHA-256 and saved as `evidence/<preview_sha256>.jpg`. A transport ACK means the remote bridge received the bytes. Mission acceptance comes from a UGV report.

Loss of an established radio link locks the session and stops the GS heartbeat. Reconnecting requires an explicit disconnect/connect and creates a fresh transport. Reconcile the unfinished mission at the robot before starting another session. An unresponsive operator window also stops the heartbeat. A delayed or duplicate review message must not authorize a new decision.

## Integration

`radio.py` uses the upstream Python transport and reliability engine pinned in `requirements.txt`. The app reads wire-map v3 settings and registers the checked-in ROS message definitions with the rosbags Jazzy CDR codec. The production UGV gateway and FSM own task acceptance, movement and cleaning. The app adds no radio topics. Compatibility depends on both the topic ID/type map and the exact message definitions. Update the contract and rerun the codec and bench checks when either changes.

The online map uses the OpenStreetMap standard tile service. Only visible tiles are fetched. Application cache storage is bounded. Internet is required for uncached tiles. [Tile usage policy](https://operations.osmfoundation.org/policies/tiles/)

## Verify

From the GS checkout:

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q tests/test_station.py
bash tests/check_codec.sh ../RavenStrike-UGV
bash tests/bench.sh ../RavenStrike-UGV
```

The codec check uses the built UGV ROS environment as a reference. It compares decoded values across implementations because CDR alignment padding can differ. The radio bench requires both lab radios on the Jetson, passwordless access to the existing UGV software sandbox, and exclusive access to the ports. Close the GS before running the bench. It starts the production UGV gateway and a non-actuating ROS test peer in a loopback-only namespace, exercises three review branches using the field GUI handlers, and suspends the gateway to check loss and recovery. Images and mission responses are fixtures. No nav, camera, or cleaning hardware starts.

Before field use, run the field client against the production FSM through a full simulated mission, then commission actual receiver settings, site coordinates, reach, physical link loss and manual stop on Lynx. Test installation and USB operation on each additional OS. Local logs and site evidence are gitignored.
