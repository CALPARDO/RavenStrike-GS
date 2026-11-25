import time
from types import SimpleNamespace as N

import pytest

from station import Journal, Station
from wire import Codec


class Sender:
    def send(self, topic, msg):
        return 123


@pytest.fixture
def station(tmp_path):
    c = Codec()
    log = Journal(tmp_path)
    st = Station(c, Sender(), log)
    st.start("UGV-1", "area", (1.0, 2.0, 0.0))
    yield st
    log.close()


def status(st, active, note, sec=1, state="working"):
    msg = st.codec.types["SessionStatus"](
        st.codec.types["Time"](sec, 0), st.session, state, active, [], False, 0, 0, note, ""
    )
    st.receive(8, msg)


def evidence(st, mission, kind, token, sec=2, **extra):
    values = {
        "session_id": st.session,
        "mission_id": mission,
        "kind": kind,
        "evidence_id": token,
        "stamp": N(sec=sec, nanosec=0),
        "preview": N(format="", data=b""),
    }
    msg = N(**(values | extra))
    # Use the real metadata logger for incoming generated dataclasses in the bench.
    st.journal.log = lambda *args, **kw: None
    st.receive(19, msg)


def test_review_matching_and_duplicates(station):
    st = station
    evidence(st, "x", "cycle_complete", "proof")
    status(st, "y", "OK_TO_MOVE_FORWARD")
    assert st.review_token() is None
    status(st, "x", "OK_TO_MOVE_FORWARD", 3)
    with pytest.raises(ValueError):
        st.decide("other", "approve")
    with pytest.raises(ValueError):
        st.decide("proof", "retry")
    st.decide("proof", "approve")
    with pytest.raises(ValueError):
        st.decide("proof", "approve")
    status(st, "x", "OK_TO_MOVE_FORWARD", 2)
    assert st.review_token() is None


def test_session_and_image_guards(station):
    st = station
    with pytest.raises(ValueError):
        st.add(40, 10, 0)
    status(st, "x", "OK_TO_MOVE_FORWARD")
    evidence(st, "x", "cycle_complete", "mismatch", session_id="another")
    assert st.review_token() is None
    with pytest.raises(ValueError):
        evidence(
            st, "x", "after", "bad", preview=N(format="jpeg", data=b"123"), preview_sha256="wrong"
        )
    assert st.review_token() is None
    status(st, "x", "new robot mode", 5)
    evidence(st, "x", "cycle_complete", "ok")
    assert st.note == "new robot mode" and st.review_token() is None


def test_queue_controls(station):
    st = station
    status(st, "", "accepted", state="standby")
    with pytest.raises(ValueError):
        st.add(float("nan"), 0.0, 0.0)
    assert st.add(40.0, -82.0, 2.0) in st.targets
    st.command("scan_complete")
    with pytest.raises(ValueError):
        st.add(40.0, -82.0, 2.0)
    st.command("abort")
    with pytest.raises(ValueError):
        st.command("abort")
    status(st, "", "complete", 10, "completed")
    status(st, "old", "OK_TO_MOVE_FORWARD", 11)
    assert st.state == "completed" and st.review_token() is None


def test_outcome_can_arrive_before_radio_ack(station):
    st = station
    status(st, "", "accepted", state="standby")
    mission = st.add(40, -83, 0)
    report_type = st.codec.store.types["ravenstrike_interfaces/msg/VerificationReport"]

    def report(sec, outcome):
        msg = report_type(
            st.codec.types["Time"](sec, 0), mission, st.callsign, outcome, [], "", "task rejected"
        )
        st.receive(2, msg)

    report(4, "blocked")
    report(3, "in_progress")
    report(4, "blocked")
    st.task_ack(7, 123, time.time_ns())
    st.task_ack(7, 123, time.time_ns())
    stages = [line.split(" UTC   ", 1)[1] for line in st.timeline[mission]]
    assert stages == ["Sent to radio queue", "Blocked · task rejected", "Radio ACK"]
    status(st, "", "complete", 6, "completed")
    st.start("UGV-1", "next", (0.0, 0.0, 0.0))
    assert not st.timeline and not st.pending_tasks


def test_selection_report_precedes_active_snapshot(station):
    st = station
    status(st, "", "ready", state="standby")
    mission = st.add(40, -83, 0)
    make_report = st.codec.types["VerificationReport"]
    msg = make_report(
        st.codec.types["Time"](3, 0),
        mission,
        st.callsign,
        "in_progress",
        [],
        "",
        "nearest target selected",
    )
    st.receive(2, msg)
    assert any(line.endswith("   Active") for line in st.timeline[mission])
    status(st, "", "SELECT_FOD_TARGET", sec=2)
    status(st, mission, "DETECTION_MODE", sec=5)
    assert sum(line.endswith("   Active") for line in st.timeline[mission]) == 1
    status(st, "return:" + st.session, "queue empty", sec=6, state="returning")
    assert set(st.timeline) == {mission}
