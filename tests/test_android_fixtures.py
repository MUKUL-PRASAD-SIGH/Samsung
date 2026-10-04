"""The Android app and the server must agree on the wire protocol. Both sides are tested against shared files:

* android/app/src/test/resources/server_messages.jsonl -- produced here from the REAL action classes; the Kotlin tests parse it.
* android/app/src/test/resources/client_frames.json    -- what the app sends; the Kotlin tests assert the app builds exactly
  these, and this test replays them against the real server and checks it accepts every one.
"""

import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agent import server
from agent.coordinator import AgentCoordinator
from agent.llm_client import MockLLMBackend
from agent.multimodal.vision import MockVisionBackend
from agent.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "android/app/src/test/resources"


def test_server_fixtures_are_up_to_date():
    """If this fails the server's message schema changed: run `python scripts/dump_android_fixtures.py` and update the Kotlin
    models in android/app/src/main/java/com/samsung/interruptible/data/Protocol.kt until the Android tests pass again."""
    r = subprocess.run([sys.executable, "scripts/dump_android_fixtures.py", "--check"], cwd=ROOT, capture_output=True, text=True,
                       env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stdout + r.stderr


def test_every_action_type_the_server_can_send_is_covered_by_a_fixture_or_knowingly_ignored():
    from agent.schemas.actions import ActionType

    fixtures = {json.loads(line).get("action_type") for line in (RES / "server_messages.jsonl").read_text().splitlines() if line}
    missing = {a.value for a in ActionType} - fixtures
    assert not missing, f"new server action type(s) {missing}: add a fixture and a Kotlin model (or extend IGNORED here on purpose)"


def test_the_apps_generated_session_ids_pass_the_servers_validation():
    s = Settings()
    assert s.session_id_re.match("android_" + "0123456789ab")
    assert not s.session_id_re.match("android_" + "0123456789ab" * 6)     # 64+ chars: the app truncates, the server refuses


class _Reader:
    def __init__(self, ws):
        self.q = queue.Queue()
        threading.Thread(target=self._run, args=(ws,), daemon=True).start()

    def _run(self, ws):
        try:
            while True:
                self.q.put(json.loads(ws.receive_text()))
        except Exception:
            pass

    def drain(self, seconds):
        out, end = [], time.time() + seconds
        while time.time() < end:
            try:
                out.append(self.q.get(timeout=0.1))
            except queue.Empty:
                pass
        return out


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(server, "coordinator", AgentCoordinator(llm_backend=MockLLMBackend(), vision_backend=MockVisionBackend(),
                                                                enable_debounce=False))
    with TestClient(server.app) as c:
        yield c


def test_the_server_accepts_every_frame_the_app_sends(client):
    frames = {f["name"]: f["frame"] for f in json.loads((RES / "client_frames.json").read_text())}
    with client.websocket_connect("/ws/android_0123456789ab") as ws:
        reader = _Reader(ws)
        for name in ("tts_on", "voice_start", "voice_stop", "tts_off", "interrupt", "video_frame"):
            ws.send_text(json.dumps(frames[name]))
        got = reader.drain(1.0)
        assert not [m for m in got if m.get("type") == "error"], f"the server rejected an app frame: {got}"
        assert [m for m in got if m.get("type") == "tts_status"], "the tts opt-in must be acknowledged"

        ws.send_text(json.dumps(frames["user_text"]))
        types = [m.get("action_type") for m in reader.drain(2.0)]
        assert types[0] == "filler" and "tool_call" in types


def test_the_server_stores_the_frame_the_app_sends(client):
    frames = {f["name"]: f["frame"] for f in json.loads((RES / "client_frames.json").read_text())}
    with client.websocket_connect("/ws/android_0123456789ab") as ws:
        ws.send_text(json.dumps(frames["video_frame"]))
        time.sleep(0.5)
    stored = server.coordinator._frames.get("android_0123456789ab")
    assert stored and stored[-1].mime == "image/jpeg"
