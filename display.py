"""Operator-facing labels. Wire IDs and UTC journal records stay intact."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
GENERATED_ID = re.compile(r"\b(?:fod|onboard)-[0-9a-fA-F]{8,}\b")


def eastern(seconds=None):
    when = datetime.now(EASTERN) if seconds is None else datetime.fromtimestamp(seconds, EASTERN)
    return when.strftime("%H:%M:%S")


class TargetNames:
    def __init__(self):
        self.names = {}

    def __call__(self, raw):
        if len(raw) <= 16 and not GENERATED_ID.fullmatch(raw):
            return raw
        if raw not in self.names:
            self.names[raw] = f"FOD {len(self.names) + 1:02d}"
        return self.names[raw]

    def in_text(self, text):
        return GENERATED_ID.sub(lambda match: self(match.group()), text)
