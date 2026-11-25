"""Generate the desktop flowchart when the onboard FSM changes. Requires Graphviz to render."""

import argparse
import json
import subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

import yaml

ASSET = Path(__file__).resolve().parents[1] / "contract/fsm.svg"
LABELS = {
    "TURN_ON": "Power on",
    "BRING_UP_SYSTEM": "Bring up system",
    "STANDBY_MODE": "Standby",
    "CLEANING_TASKS_AVAILABLE": "Tasks queued?",
    "FOD_ID_PROCESS_COMPLETED_MESSAGE_RECEIVED": "Scan complete?",
    "SELECT_FOD_TARGET": "Select target",
    "NAVIGATION_MODE": "Navigate to target",
    "NEW_CLOSER_FOD_DETECTED": "Closer FOD?",
    "FOD_TARGET_DEFINITION": "Set new target",
    "REACHED_FOD_SEARCH_AREA": "At search area?",
    "DETECTION_MODE": "Search for FOD",
    "FOD_DETECTED_IN_SEARCH_AREA": "FOD found?",
    "SEND_BEFORE_PICTURE_TO_GS": "Send before image",
    "CLEANING_MODE": "Clean",
    "SEND_AFTER_PICTURE_TO_GS": "Send after image",
    "SEND_FALSE_POSITIVE_PICTURE_TO_GS": "Report no FOD",
    "OK_TO_MOVE_FORWARD": "Continue?",
    "REPOSITIONING_MODE": "Reposition",
    "QUEUE_REMOVAL": "Remove from queue",
    "CAN_DETECT_MORE_FOD": "More FOD?",
    "RETURN_TO_BASE_MODE": "Return to base",
    "MISSION_COMPLETED": "Mission complete",
    "HALTED": "Halted",
}


def graph(definition):
    lines = [
        "digraph mission {",
        'graph [bgcolor="white", nodesep=0.33, ranksep=0.36, pad=0.16];',
        (
            'node [fontname="sans-serif", fontsize=13, color="#9aadb1", fontcolor="#203a40",'
            ' style=filled, fillcolor="#f5f8f8", margin="0.12,0.07"];'
        ),
        'edge [fontname="sans-serif", fontsize=10, color="#758c91", fontcolor="#36545a", arrowsize=0.72];',
    ]
    labels = {}
    for name, state in definition["states"].items():
        transitions = state["transitions"]
        shape = "diamond" if len(transitions) > 1 else "box"
        if not transitions or name == definition["initial"]:
            shape = "oval"
        lines.append(f'{name} [id="{name}", label={json.dumps(LABELS[name])}, shape={shape}];')
        labels[name] = LABELS[name]
        for event, edge in transitions.items():
            target = edge["target"]
            text = [] if event == "next" else [event.capitalize()]
            text.extend(c.replace("_", " ") for c in edge["conditions"])
            key = f"{name}-{event}-{target}"
            lines.append(f'{name} -> {target} [id="{key}", label={json.dumps(", ".join(text))}];')
            labels[key] = ", ".join(text)
    for event, target in definition["interrupts"].items():
        source = f"interrupt_{event}"
        key = f"{source}-{target}"
        label = event.replace("_", " ").capitalize()
        lines.append(f'{source} [label="{label}", shape=plaintext, style="", fontsize=11];')
        lines.append(f'{source} -> {target} [id="{key}", style=dashed];')
        labels[key] = ""
    return "\n".join([*lines, "}"]), labels


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("yaml", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    source, expected = graph(yaml.safe_load(args.yaml.read_text()))
    if args.check:
        groups = ET.parse(ASSET).getroot().findall(".//{*}g")
        actual = {
            g.get("id"): (g.findtext("{*}text") or "")
            for g in groups
            if g.get("class") == "edge" or g.get("id") in LABELS
        }
        if actual != expected:
            raise SystemExit(
                "Flowchart differs from onboard transitions; regenerate contract/fsm.svg"
            )
    else:
        result = subprocess.run(
            ["dot", "-Tsvg"], input=source.encode(), capture_output=True, check=True
        )
        ASSET.write_bytes(result.stdout)
