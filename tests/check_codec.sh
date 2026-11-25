#!/usr/bin/env bash
set -eo pipefail
gs=$(cd "$(dirname "$0")/.." && pwd)
cd "${1:?Built UGV checkout required for reference}"
for name in RobotLocation AppCommand AppStatus; do
  cmp "$gs/contract/$name.msg" "src/ugv_mission_interfaces/msg/$name.msg"
done
cmp "$gs/contract/apps.json" src/ugv_comms/config/apps.json
cmp "$gs/contract/gs.yaml" src/ugv_comms/config/gs.yaml
source scripts/rosenv.sh
dir=$(mktemp -d)
trap 'rm -rf "$dir"' EXIT
python3 "$gs/tests/codec_oracle.py" generate "$dir"
PYTHONPATH="$gs" "$gs/.venv/bin/python" "$gs/tests/codec_oracle.py" field "$dir"
python3 "$gs/tests/codec_oracle.py" check "$dir"
echo 'PASS: ten field radio message types agree with Jazzy CDR in their operational directions.'
