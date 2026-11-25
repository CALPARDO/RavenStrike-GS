#!/usr/bin/env bash
set -eo pipefail
gs=$(cd "$(dirname "$0")/.." && pwd)
cd "${1:?Built UGV checkout required for reference}"
for name in RobotLocation AppCommand FieldStatus PlanPreview; do
  cmp "$gs/contract/$name.msg" "src/ugv_mission_interfaces/msg/$name.msg"
done
cmp "$gs/contract/apps.json" src/ugv_comms/config/apps.json
cmp "$gs/contract/gs.yaml" src/ugv_comms/config/gs.yaml
"$gs/.venv/bin/python" "$gs/tools/render_fsm.py" src/ugv_mission/config/fsm.yaml --check
source scripts/rosenv.sh
dir=$(mktemp -d)
trap 'rm -rf "$dir"' EXIT
python3 "$gs/tests/codec_oracle.py" generate "$dir"
PYTHONPATH="$gs" "$gs/.venv/bin/python" "$gs/tests/codec_oracle.py" field "$dir"
python3 "$gs/tests/codec_oracle.py" check "$dir"
echo 'PASS: eleven field radio message types agree with Jazzy CDR in their operational directions.'
