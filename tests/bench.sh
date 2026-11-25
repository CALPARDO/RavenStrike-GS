#!/usr/bin/env bash
set -euo pipefail
: "${1:?Pass the path of the built RavenStrike-UGV checkout}"
ugv=$(realpath "$1")
gs=$(cd "$(dirname "$0")/.." && pwd)
case ${2:-reviews} in reviews|mission) ;; *) echo "Usage: $0 UGV_CHECKOUT [reviews|mission]" >&2; exit 2 ;; esac
cd "$ugv"
exec bash scripts/software_sandbox.sh --radio-bench bash -c '
  set -eo pipefail
  source scripts/rosenv.sh
  export ROS_DOMAIN_ID=187
  gs=$1
  callsign=BENCH-$(cat /proc/sys/kernel/random/uuid)
  work=$(mktemp -d)
  export ROS_LOG_DIR="$work"
  echo "Gateway logs: $work"
  setsid ros2 launch ugv_comms radio.launch.py role:=ugv serial_port:=/dev/ttyTHS1 serial_confirmed:=true >"$work/gateway.log" 2>&1 &
  gateway=$!
  trap '\''kill -INT -- "-$gateway" 2>/dev/null || true; wait "$gateway" || true'\'' EXIT
  if [[ $2 == mission ]]; then
    python3 "$gs/tests/mission_bench.py" "$gs" "$callsign" "$work"
    exit
  fi
  python3 "$gs/tests/peer_ros.py" "$callsign" >"$work/peer.log" 2>&1 &
  peer=$!
  trap '\''kill -INT -- "-$gateway" 2>/dev/null || true; kill "$peer" 2>/dev/null || true; wait "$gateway" || true'\'' EXIT
  export GS_BENCH_GATEWAY_PID="$gateway"
  cd "$gs"
  env -u AMENT_PREFIX_PATH -u LD_LIBRARY_PATH PYTHONPATH="$gs" "$gs/.venv/bin/python" tests/bench_client.py "$callsign"
' bench "$gs" "${2:-reviews}"
