#!/usr/bin/env bash
# Actual GS UI, serial transports, UGV gateway and job runner over paired Linux PTYs.
set -eo pipefail
gs=$(cd "$(dirname "$0")/.." && pwd)
cd "${1:?Built UGV checkout required}"
source scripts/rosenv.sh
export ROS_DOMAIN_ID=182 ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
unset ROS_DISCOVERY_SERVER ROS_STATIC_PEERS
dir=$(mktemp -d)
export ROS_LOG_DIR=$dir/logs
pids=()
cleanup() {
  for pid in "${pids[@]}"; do kill -INT -- "-$pid" 2>/dev/null || true; done
  for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
}
trap cleanup EXIT
setsid socat "pty,raw,echo=0,link=$dir/ugv" "pty,raw,echo=0,link=$dir/gs" >"$dir/pty.log" 2>&1 &
pids+=($!)
for ((i=0; i<50; i++)); do [[ -e $dir/gs ]] && break; sleep .1; done
sed 's/rtscts: true/rtscts: false/' src/ugv_comms/config/ugv.yaml > "$dir/config.yaml"
setsid ros2 launch ugv_comms radio.launch.py role:=ugv field_workspace:="$PWD" serial_confirmed:=true \
  serial_port:="$(readlink -f "$dir/ugv")" config:="$dir/config.yaml" >"$dir/gateway.log" 2>&1 &
pids+=($!)
echo "Bench logs: $dir"
QT_QPA_PLATFORM=offscreen PYTHONPATH="$gs" "$gs/.venv/bin/python" "$gs/tests/app_peer.py" "$(readlink -f "$dir/gs")"
