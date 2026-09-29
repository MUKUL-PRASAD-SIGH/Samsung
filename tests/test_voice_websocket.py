"""WebSocket voice protocol: streaming PCM mode, legacy WebM push-to-talk, and disconnect cleanup."""

import json
import queue
import subprocess
import threading
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agent import server
from agent.coordinator import AgentCoordinator
from agent.llm_client import MockLLMBackend
from agent.multimodal.asr import ASRProcessor

FIXTURES = Path(__file__).parent / "fixtures" / "audio"


def _pcm(name, lead_s=0.5, tail_s=1.5):
    raw = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(FIXTURES / name), "-ar", "16000", "-ac", "1", "-f", "s16le", "-"],
        capture_output=True, check=True,
    ).stdout
    return b"\x00\x00" * int(16000 * lead_s) + raw + b"\x00\x00" * int(16000 * tail_s)


def _norm(t):
    return "".join(c for c in t.lower() if c.isalnum() or c == " ").strip()


@pytest.fixture()
def client(monkeypatch):
    asr = ASRProcessor()
    asr._ensure_model_loaded()
    if not asr.is_loaded:
        pytest.skip("faster-whisper model unavailable (offline / not cached)")
    # A fresh coordinator per test: the module-level one has asyncio queues that bind to the first
    # TestClient's event loop, so reusing it across tests deadlocks. MockLLMBackend keeps the live
    # LLM out of these tests.
    fresh = AgentCoordinator(llm_backend=MockLLMBackend(), asr_processor=asr, enable_debounce=False)
    monkeypatch.setattr(server, "coordinator", fresh)
    with TestClient(server.app) as c:
        yield c


class _Reader:
    """Drains a websocket in a thread so a misbehaving server fails the test instead of hanging it."""

    def __init__(self, ws):
        self.q = queue.Queue()
        self.t = threading.Thread(target=self._run, args=(ws,), daemon=True)
        self.t.start()

    def _run(self, ws):
        try:
            while True:
                self.q.put(json.loads(ws.receive_text()))
        except Exception:
            self.q.put(None)

    def until(self, predicate, timeout=15.0):
        seen, deadline = [], time.time() + timeout
        while time.time() < deadline:
            try:
                msg = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            if msg is None:
                break
            seen.append(msg)
            if predicate(msg):
                return seen
        pytest.fail(f"timed out; saw: {[(m['action_type'], m.get('state') or m.get('text')) for m in seen]}")


def test_streaming_pcm_over_websocket_yields_activity_partials_and_final(client):
    with client.websocket_connect("/ws/voice_ws_1") as ws:
        reader = _Reader(ws)
        ws.send_text(json.dumps({"type": "voice_stream", "action": "start"}))
        audio = _pcm("book_flight.wav")
        for i in range(0, len(audio), 3200):
            ws.send_bytes(audio[i:i + 3200])
            time.sleep(0.03)
        seen = reader.until(lambda m: m["action_type"] == "transcript" and not m["is_partial"])

        states = [m["state"] for m in seen if m["action_type"] == "voice_activity"]
        assert states[0] == "listening" and "speech_start" in states and "speech_end" in states
        final = seen[-1]
        assert _norm(final["text"]) == "book a flight from delhi to mumbai"
        assert final["utterance_id"] == "utt_1"
        assert any(m["action_type"] == "transcript" and m["is_partial"] and m["utterance_id"] == "utt_1" for m in seen)

        ws.send_text(json.dumps({"type": "voice_stream", "action": "stop"}))
        reader.until(lambda m: m["action_type"] == "voice_activity" and m["state"] == "idle")


def test_binary_without_voice_start_is_still_legacy_webm_push_to_talk(client):
    webm = (FIXTURES / "book_flight.webm").read_bytes()
    with client.websocket_connect("/ws/voice_ws_2") as ws:
        reader = _Reader(ws)
        ws.send_bytes(webm)
        seen = reader.until(lambda m: m["action_type"] == "transcript")
        t = seen[-1]
        assert _norm(t["text"]) == "book a flight from delhi to mumbai"
        assert t["utterance_id"] is None and t["is_partial"] is False   # legacy shape unchanged


def test_disconnect_mid_stream_frees_the_voice_runtime(client):
    sid = "voice_ws_3"
    with client.websocket_connect(f"/ws/{sid}") as ws:
        ws.send_text(json.dumps({"type": "voice_stream", "action": "start"}))
        audio = _pcm("book_flight.wav", tail_s=0.0)
        for i in range(0, 16000, 3200):        # speech in progress, then the client just vanishes
            ws.send_bytes(audio[i:i + 3200])
        time.sleep(0.5)
        assert sid in server.coordinator._voice
    deadline = time.time() + 5
    while time.time() < deadline and sid in server.coordinator._voice:
        time.sleep(0.1)
    assert sid not in server.coordinator._voice
