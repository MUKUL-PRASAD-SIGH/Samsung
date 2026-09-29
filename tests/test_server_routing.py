"""Actions must reach the session they belong to, no matter how many other connections exist.

Regression: every WebSocket handler used to run its own forwarder on ONE shared action queue and dropped
whatever wasn't for its session, so a second connection (another tab, a stale connection, React dev-mode
double-connect) randomly stole and discarded another session's actions (e.g. the acknowledgement filler)."""

import json
import queue
import threading
import time

import pytest
from starlette.testclient import TestClient

from agent import server
from agent.coordinator import AgentCoordinator
from agent.llm_client import MockLLMBackend
from agent.multimodal.vision import MockVisionBackend


@pytest.fixture()
def client(monkeypatch):
    fresh = AgentCoordinator(llm_backend=MockLLMBackend(), vision_backend=MockVisionBackend(), enable_debounce=False)
    monkeypatch.setattr(server, "coordinator", fresh)
    with TestClient(server.app) as c:
        yield c


class Reader:
    def __init__(self, ws):
        self.q = queue.Queue()
        self.seen = []
        threading.Thread(target=self._run, args=(ws,), daemon=True).start()

    def _run(self, ws):
        try:
            while True:
                self.q.put(json.loads(ws.receive_text()))
        except Exception:
            pass

    def drain(self, seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                self.seen.append(self.q.get(timeout=0.1))
            except queue.Empty:
                pass
        return [m["action_type"] for m in self.seen]


def say(ws, text="Find flights from Delhi to Mumbai"):
    ws.send_text(json.dumps({"type": "user_text", "text": text}))


def test_an_idle_second_connection_does_not_steal_actions(client):
    with client.websocket_connect("/ws/tab_idle") as idle, client.websocket_connect("/ws/tab_active") as active:
        idle_r, active_r = Reader(idle), Reader(active)
        time.sleep(0.3)
        say(active)
        got = active_r.drain(4.0)
        assert got[0] == "filler", f"the acknowledgement was stolen: {got}"
        assert {"filler", "tool_call", "state_snapshot", "spoken_response"} <= set(got)
        assert idle_r.drain(0.5) == []          # and the idle tab sees nothing that isn't its own


def test_many_idle_connections_never_lose_an_action(client):
    conns = [client.websocket_connect(f"/ws/idle_{i}") for i in range(5)]
    sockets = [c.__enter__() for c in conns]
    try:
        for s in sockets:
            Reader(s)
        with client.websocket_connect("/ws/busy") as active:
            r = Reader(active)
            for _ in range(3):
                say(active)
            got = r.drain(6.0)
            assert got.count("filler") == 3 and got.count("tool_call") == 3, got
    finally:
        for c in conns:
            c.__exit__(None, None, None)


def test_two_tabs_on_the_same_session_both_receive_every_action(client):
    with client.websocket_connect("/ws/shared") as tab1, client.websocket_connect("/ws/shared") as tab2:
        r1, r2 = Reader(tab1), Reader(tab2)
        time.sleep(0.3)
        say(tab1)
        a, b = r1.drain(4.0), r2.drain(0.5)
        assert a == b and "filler" in a


def test_a_closed_connection_does_not_disturb_the_others(client):
    with client.websocket_connect("/ws/stays") as stays:
        r = Reader(stays)
        with client.websocket_connect("/ws/leaves"):
            pass                                  # connects and immediately goes away
        time.sleep(0.3)
        say(stays)
        assert "filler" in r.drain(4.0)
