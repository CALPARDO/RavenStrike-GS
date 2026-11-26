import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QApplication, QWidget

from commissioning import CommissioningDialog
from station import Journal
from wire import Codec


@pytest.fixture
def tool(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    owner.codec = Codec()
    owner.journal = Journal(tmp_path)
    owner.radio = SimpleNamespace(ready=True)
    owner.station = Mock()
    owner.app_fresh = lambda: True
    w = CommissioningDialog(owner)
    yield w
    w.reject()
    owner.journal.close()
    owner.close()
    app.processEvents()


def test_local_codec_recording_and_unavailable(tool):
    tool.owner.radio.ready = False
    tool.start(["codec", "record", "camera"])
    for _ in range(4):
        tool.advance()
    assert [tool.rows[k][0].text(1) for k in ("codec", "record", "camera")] == [
        "Passed", "Passed", "Hardware Unavailable"]
    tool.owner.station.send.assert_not_called()
    assert len(list(tool.owner.journal.folder.parent.iterdir())) == 1
    assert len(tool.owner.journal.folder.joinpath("events.jsonl").read_text().splitlines()) == 3


def test_request_matching_cancel_and_timeout(tool):
    tool.start(["camera", "tf", "odom"])
    token = tool.request
    tool.start(["record"])
    assert tool.owner.station.send.call_count == 1
    sent = tool.owner.station.send.call_args.args[1]
    assert (sent.operation, sent.app, sent.scenario, sent.request_id) == (
        "diagnose", "commissioning", "camera", token)
    for reply in ("[]", "?", json.dumps([token, "tf", "P", "wrong key"]),
                  json.dumps(["wrong", "camera", "P", "old"]),
                  json.dumps([token, "camera", {}, "bad code"])):
        tool.reply(reply)
        assert tool.active == "camera"
    tool.reply(json.dumps([token, "camera", "U", "No stamped frame received"]))
    assert tool.rows["camera"][0].text(1) == "Hardware Unavailable"
    tool.advance()
    tool.started = time.monotonic() - 15
    tool.advance()
    assert tool.rows["tf"][0].text(1) == "Failed"
    tool.advance()
    tool.cancel()
    assert tool.owner.station.send.call_args.args[1].operation == "cancel-check"
    assert tool.rows["odom"][0].text(1) == "Skipped"
    assert not tool.timer.isActive()
    tool.reply(json.dumps([token, "camera", "P", "late"]))
    assert tool.rows["camera"][0].text(1) == "Hardware Unavailable"


def test_close_cancels_active_and_pending(tool):
    tool.start(["resources", "nav"])
    tool.reject()
    assert tool.owner.station.send.call_args.args[1].operation == "cancel-check"
    assert tool.rows["resources"][0].text(1) == tool.rows["nav"][0].text(1) == "Skipped"
    assert not tool.timer.isActive()
