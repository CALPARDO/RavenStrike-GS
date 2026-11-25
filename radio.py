"""One field connection per explicit operator connect. A dropped lease ends this connection."""

import queue
import threading
import time

from rfd_bridge.config import load_config
from rfd_bridge.engine import BridgeEngine
from rfd_bridge.reliability import CTRL_ACK, CTRL_NACK, unpack_ack
from rfd_bridge.transport import SerialTransport

from wire import CONTRACT


class FieldEngine(BridgeEngine):
    def __init__(self, *args, event):
        super().__init__(*args)
        self.event = event
        self.link_since = None
        self.established = False
        self.lost = False

    def check_link(self):
        connected = self.link_state == "CONNECTED" and self.config_match is True
        if not connected:
            self.link_since = None
            if self.established:
                self.lost = True
        elif self.link_since is None:
            self.link_since = time.monotonic()
        elif time.monotonic() - self.link_since >= self.config.link.link_timeout_sec:
            self.established = True
        return self.established and connected and not self.lost

    def receive_bytes(self, data):
        with self._lock:
            self.check_link()
            super().receive_bytes(data)
            self.check_link()

    def _transport_state(self, connected, detail):
        if not connected and self.established:
            self.lost = True
        super()._transport_state(connected, detail)

    @property
    def user_data_allowed(self):
        return super().user_data_allowed and not self.lost

    def _handle_control(self, payload):
        if payload and payload[0] in (CTRL_ACK, CTRL_NACK):
            try:
                nack, topic, mid = unpack_ack(payload)
                self.event("nack" if nack else "ack", topic=topic, wire_id=mid)
            except ValueError:
                pass
        super()._handle_control(payload)


class Radio:
    def __init__(self, port, codec):
        self.codec = codec
        self.events = queue.Queue(maxsize=2048)
        self.overflow = threading.Event()
        self.stop = threading.Event()
        config = load_config(str(CONTRACT / "gs.yaml"), resolve_types=False)
        config.serial.port = port
        config.serial.reconnect = False
        self.engine = FieldEngine(
            config,
            SerialTransport(config.serial),
            self.receive,
            lambda level, detail: self.event("radio", level=level, detail=detail),
            event=self.event,
        )
        self.worker = threading.Thread(target=self.run, daemon=True)
        self.ready = False
        self.ui_seen = time.monotonic()

    def start(self):
        self.engine.start()
        self.worker.start()

    def event(self, kind, **fields):
        try:
            self.events.put_nowait((time.time_ns(), kind, fields))
        except queue.Full:
            self.overflow.set()

    def receive(self, topic, payload):
        self.event("incoming", topic=topic, payload=payload)

    def run(self):
        previous = None
        first_heartbeat = None
        while not self.stop.is_set():
            with self.engine._lock:
                if time.monotonic() - self.ui_seen > 3:
                    self.engine.lost = True
                    self.event("operator_timeout", detail="Operator window stopped responding")
                linked = self.engine.check_link()
                if linked and not self.overflow.is_set():
                    if first_heartbeat is None:
                        first_heartbeat = time.monotonic()
                    self.engine.enqueue(
                        self.engine.topics[3], self.codec.encode(self.codec.stamp())
                    )
                stats = (self.engine.reliability.retransmissions, self.engine.reliability.exhausted)
                if previous != stats and previous is not None:
                    self.event("reliability", retransmissions=stats[0], exhausted=stats[1])
                previous = stats
                self.ready = (
                    linked
                    and not self.overflow.is_set()
                    and first_heartbeat is not None
                    and time.monotonic() - first_heartbeat >= 0.4
                )
            if self.engine.lost or self.overflow.is_set():
                self.engine.close()
                self.event("link_lost", detail="Disconnect, reconcile UGV state, then reconnect.")
                return
            self.stop.wait(0.2)

    def send(self, topic, msg):
        with self.engine._lock:
            if not self.ready or not self.engine.check_link() or self.overflow.is_set():
                raise ValueError("A healthy radio connection is required.")
            mid = self.engine._next_message_id
            if not self.engine.enqueue(self.engine.topics[topic], self.codec.encode(msg)):
                raise ValueError("Radio transmit queue rejected the command.")
            return mid

    def close(self):
        self.stop.set()
        self.worker.join()
        self.engine.close()
