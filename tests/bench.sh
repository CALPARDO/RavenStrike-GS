#!/usr/bin/env bash
set -euo pipefail
: "${1:?Pass the path of the built RavenStrike-UGV checkout}"
ugv=$(realpath "$1")
gs=$(cd "$(dirname "$0")/.." && pwd)
case ${2:-reviews} in reviews|mission|preview|health|diagnostics) ;; *) echo "Usage: $0 UGV_CHECKOUT [reviews|mission|preview|health|diagnostics]" >&2; exit 2 ;; esac
cd "$ugv"
sandbox=(--radio-bench)
[[ ${2:-reviews} == diagnostics ]] && sandbox=()
exec bash scripts/software_sandbox.sh "${sandbox[@]}" bash -c '
  set -eo pipefail
  source scripts/rosenv.sh
  export ROS_DOMAIN_ID=187
  gs=$1
  callsign=BENCH-$(cat /proc/sys/kernel/random/uuid)
  work=$(mktemp -d)
  export ROS_LOG_DIR="$work"
  echo "Gateway logs: $work"
  extra=()
  ugv_port=/dev/ttyTHS1
  if [[ $2 == diagnostics ]]; then
    command -v socat >/dev/null
    socat pty,raw,echo=0,link="$work/ugv" pty,raw,echo=0,link="$work/gs" >"$work/pty.log" 2>&1 &
    relay=$!
    trap '\''kill "$relay" 2>/dev/null || true'\'' EXIT
    for i in {1..100}; do [[ -c $work/gs && -c $work/ugv ]] && break; sleep .05; done
    ugv_port=$(realpath -e "$work/ugv")
    # PTYs have no modem control wires. All framing and application paths are production.
    sed "s/^  rtscts: true$/  rtscts: false/" "$(ros2 pkg prefix --share ugv_comms)/config/ugv.yaml" >"$work/ugv.yaml"
    sed "s/^  rtscts: true$/  rtscts: false/" "$gs/contract/gs.yaml" >"$work/gs.yaml"
    extra+=("field_workspace:=$PWD" "config:=$work/ugv.yaml")
  fi
  if [[ $2 == preview || $2 == health ]]; then
    extra+=("field_workspace:=$PWD")
    export GS_BENCH_PREVIEW=1
  fi
  setsid ros2 launch ugv_comms radio.launch.py role:=ugv "serial_port:=$ugv_port" serial_confirmed:=true "${extra[@]}" >"$work/gateway.log" 2>&1 &
  gateway=$!
  trap '\''kill -INT -- "-$gateway" 2>/dev/null || true; wait "$gateway" || true'\'' EXIT
  if [[ $2 == diagnostics ]]; then
    python3 "$gs/tests/health_bench.py" source "$work" >"$work/fixture.log" 2>&1 &
    inputs=$!
    trap '\''kill -INT -- "-$gateway" 2>/dev/null || true; wait "$gateway" || true; kill "$inputs" "$relay" 2>/dev/null || true'\'' EXIT
    env -u AMENT_PREFIX_PATH -u LD_LIBRARY_PATH PYTHONPATH="$gs" "$gs/.venv/bin/python" "$gs/tests/commissioning_bench.py" "$work/gs"
    exit
  fi
  if [[ $2 == health ]]; then
    python3 "$gs/tests/health_bench.py" source "$work" > "$work/health-inputs.log" 2>&1 &
    inputs=$!
    trap '\''kill "$inputs" 2>/dev/null || true; kill -INT -- "-$gateway" 2>/dev/null || true; wait "$gateway" || true'\'' EXIT
    env -u AMENT_PREFIX_PATH -u LD_LIBRARY_PATH PYTHONPATH="$gs" "$gs/.venv/bin/python" "$gs/tests/health_bench.py" client "$work"
    exit
  fi
  if [[ $2 == mission || $2 == preview ]]; then
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
