"""Vision: the OpenRouter free-model backend (failover) and the coordinator's analyze_frame flow."""

import asyncio
import base64
import io
import json
import time
import urllib.error

import pytest
from PIL import Image, ImageDraw

from agent.coordination.tool_router import ToolRouter
from agent.coordinator import FRAMES_PER_SESSION, MAX_FRAME_AGE_S, AgentCoordinator
from agent.llm_client import LLMResponse, MockLLMBackend
from agent.multimodal.vision import (
    MAX_FRAME_BYTES, MockVisionBackend, OpenRouterVisionBackend, VisionBackend, VisionError, VisionResult,
)
from agent.schemas.actions import ActionType, SpokenResponseAction, ToolCancelAction, ToolCallAction
from agent.schemas.events import InterruptSignalEvent, UserTextEvent, VideoFrameEvent


def jpeg(text="GOA", size=(320, 180)) -> bytes:
    img = Image.new("RGB", size, (20, 60, 120))
    ImageDraw.Draw(img).text((20, 80), text, fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


# ============================================================ OpenRouter backend (simulated HTTP)
class _Resp:
    def __init__(self, body):
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ok(text):
    return {"choices": [{"message": {"content": text}}]}


def _http(code, msg="x"):
    return urllib.error.HTTPError("http://x", code, msg, {}, io.BytesIO(msg.encode()))


def _install(monkeypatch, script):
    """script: {model: [reply, ...]} where a reply is a body dict, bytes, or an exception to raise."""
    seen = []

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode())
        seen.append(body)
        steps = script[body["model"]]
        step = steps.pop(0) if len(steps) > 1 else steps[0]
        if isinstance(step, Exception):
            raise step
        return _Resp(step)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return seen


def _backend(models, clock=None):
    return OpenRouterVisionBackend(api_key="k", models=models, clock=clock or time.monotonic)


async def test_success_sends_a_data_uri_and_puts_instructions_in_the_user_turn(monkeypatch):
    seen = _install(monkeypatch, {"m1": [_ok("  Goa\n")]})
    res = await _backend(["m1"]).analyze(jpeg(), "image/jpeg", "What city?")
    assert res.answer == "Goa" and res.model == "m1"
    msg = seen[0]["messages"]
    assert len(msg) == 1 and msg[0]["role"] == "user"          # no system role: some free hosts reject it
    parts = msg[0]["content"]
    assert any(p["type"] == "text" and "What city?" in p["text"] for p in parts)
    url = next(p["image_url"]["url"] for p in parts if p["type"] == "image_url")
    assert url.startswith("data:image/jpeg;base64,") and base64.b64decode(url.split(",", 1)[1])[:2] == b"\xff\xd8"


async def test_token_budget_leaves_room_for_reasoning_models(monkeypatch):
    # dots-3-note-preview returned an EMPTY answer at max_tokens=300: its reasoning used the whole budget.
    seen = _install(monkeypatch, {"m": [_ok("Goa")]})
    await _backend(["m"]).analyze(jpeg(), "image/jpeg", "q")
    assert seen[0]["max_tokens"] >= 1000


async def test_rate_limited_model_fails_over_and_is_then_skipped(monkeypatch):
    seen = _install(monkeypatch, {"a": [_http(429, "rate limited")], "b": [_ok("Goa")]})
    backend = _backend(["a", "b"])
    assert (await backend.analyze(jpeg(), "image/jpeg", "q")).model == "b"
    assert [b["model"] for b in seen] == ["a", "b"]
    await backend.analyze(jpeg(), "image/jpeg", "q")                # "a" is cooling down: not retried
    assert [b["model"] for b in seen] == ["a", "b", "b"]


@pytest.mark.parametrize("failure", [
    _http(403, "only available on agentic harnesses"), _http(404, "no endpoints"), _http(500, "boom"),
    {"error": {"message": "provider hiccup"}},                       # HTTP 200 carrying an error body
    _ok(""), _ok(None), b"<html>not json</html>", urllib.error.URLError("dns"), TimeoutError("slow"),
])
async def test_every_kind_of_free_model_failure_falls_through_to_the_next(monkeypatch, failure):
    _install(monkeypatch, {"bad": [failure], "good": [_ok("Goa")]})
    assert (await _backend(["bad", "good"]).analyze(jpeg(), "image/jpeg", "q")).answer == "Goa"


async def test_all_models_failing_raises_with_every_reason(monkeypatch):
    _install(monkeypatch, {"a": [_http(429, "busy")], "b": [_http(403, "restricted")]})
    with pytest.raises(VisionError) as exc:
        await _backend(["a", "b"]).analyze(jpeg(), "image/jpeg", "q")
    assert "a:" in str(exc.value) and "b:" in str(exc.value) and "429" in str(exc.value)


async def test_cooldown_expires_and_a_fully_cooling_pool_still_tries_something(monkeypatch):
    now = [0.0]
    seen = _install(monkeypatch, {"a": [_http(429, "busy"), _ok("Goa")]})
    backend = _backend(["a"], clock=lambda: now[0])
    with pytest.raises(VisionError):
        await backend.analyze(jpeg(), "image/jpeg", "q")
    # every model is cooling down -> still attempts the one that recovers soonest instead of giving up
    assert (await backend.analyze(jpeg(), "image/jpeg", "q")).answer == "Goa"
    assert len(seen) == 2


async def test_content_parts_reply_is_flattened(monkeypatch):
    _install(monkeypatch, {"m": [{"choices": [{"message": {"content": [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}]}}]}]})
    assert (await _backend(["m"]).analyze(jpeg(), "image/jpeg", "q")).answer == "A B"


async def test_bad_input_is_rejected_before_any_network_call(monkeypatch):
    seen = _install(monkeypatch, {"m": [_ok("x")]})
    b = _backend(["m"])
    for image, mime in [(jpeg(), "text/plain"), (b"", "image/jpeg"), (b"x" * (MAX_FRAME_BYTES + 1), "image/jpeg")]:
        with pytest.raises(VisionError):
            await b.analyze(image, mime, "q")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)   # the backend falls back to the environment
    with pytest.raises(VisionError, match="OPENROUTER_API_KEY"):
        await OpenRouterVisionBackend(api_key="", models=["m"]).analyze(jpeg(), "image/jpeg", "q")
    assert seen == []


# ==================================================================== coordinator: analyze_frame
class RecordingLLM(MockLLMBackend):
    """Also records which tools were offered on each call."""

    def __init__(self, responses):
        super().__init__(canned_responses=responses)
        self.tools_offered = []

    async def generate(self, messages, tools=None):
        self.tools_offered.append([t["function"]["name"] for t in (tools or [])])
        return await super().generate(messages, tools)


def analyze(question="What city is on the poster?"):
    return LLMResponse(response_type="tool_call", tool_name="analyze_frame", arguments={"question": question})


def search(**args):
    return LLMResponse(response_type="tool_call", tool_name="search_flights", arguments=args)


def fast_router():
    router = ToolRouter()
    definition = router._tools["search_flights"]

    async def quick(**kw):
        return {"flights": [{"flight": "6E-1", "airline": "IndiGo", "origin": kw.get("origin"), "destination": kw.get("destination"), "price": "$1", "departure": "9 AM"}]}
    definition.handler = quick
    return router


def frame_event(sid, data=None, mime="image/jpeg"):
    return VideoFrameEvent(session_id=sid, frame_data=data if data is not None else jpeg(), mime=mime, source="screen")


async def make(responses, vision=None, router=None):
    llm = RecordingLLM(responses)
    coordinator = AgentCoordinator(tool_router=router or fast_router(), llm_backend=llm,
                                   vision_backend=vision or MockVisionBackend(["The poster says Goa."]), enable_debounce=False)
    await coordinator.start()
    return coordinator, llm


async def collect(coordinator, seconds):
    await asyncio.sleep(seconds)
    out = []
    while not coordinator.action_queue.empty():
        out.append(coordinator.action_queue.get_nowait())
    return out


async def test_vision_result_feeds_the_next_plan_and_drives_a_tool_call():
    vision = MockVisionBackend(["The poster says Goa."])
    c, llm = await make([analyze(), search(origin="Delhi", destination="Goa")], vision)
    try:
        await c.post_event(frame_event("v1"))
        await c.post_event(UserTextEvent(session_id="v1", text="Find flights from Delhi to the city on this poster"))
        actions = await collect(c, 0.8)

        calls = [a for a in actions if isinstance(a, ToolCallAction)]
        assert [a.tool_name for a in calls] == ["analyze_frame", "search_flights"]
        assert calls[1].arguments["destination"] == "Goa"
        assert len(vision.calls) == 1 and vision.calls[0]["question"] == "What city is on the poster?"
        assert any(isinstance(a, SpokenResponseAction) and "flights" in a.text for a in actions)

        # continuation saw the observation, and could NOT call analyze_frame again (no loops)
        second_prompt = json.dumps(llm.call_history[1])
        assert "The poster says Goa." in second_prompt
        assert "analyze_frame" in llm.tools_offered[0] and "analyze_frame" not in llm.tools_offered[1]
        # the vision question is not a user fact and must not leak into slots
        assert "question" not in c.sessions["v1"].slots
        assert c.sessions["v1"].slots["destination"] == "Goa"
    finally:
        await c.stop()


async def test_no_frame_gives_the_planner_an_honest_note_not_a_hallucination():
    vision = MockVisionBackend(["should never be used"])
    c, llm = await make([analyze("What is on my screen?"),
                         LLMResponse(response_type="spoken_response", content="I can't see anything yet -- please share your screen.")], vision)
    try:
        await c.post_event(UserTextEvent(session_id="v2", text="What's on my screen?"))
        actions = await collect(c, 0.6)
        assert vision.calls == []                                     # nothing to look at: backend never called
        assert "No camera or screen frame" in json.dumps(llm.call_history[1])
        assert any(isinstance(a, SpokenResponseAction) and "share your screen" in a.text for a in actions)
    finally:
        await c.stop()


async def test_stale_frames_are_ignored():
    c, _ = await make([])
    try:
        await c.post_event(frame_event("v3"))
        await asyncio.sleep(0.1)
        assert c.latest_frame("v3") is not None
        c._frames["v3"][-1].ts = time.time() - MAX_FRAME_AGE_S - 1
        assert c.latest_frame("v3") is None
    finally:
        await c.stop()


async def test_frame_buffer_is_bounded_and_invalid_frames_are_dropped():
    c, _ = await make([])
    try:
        for i in range(FRAMES_PER_SESSION + 4):
            await c.post_event(frame_event("v4", jpeg(str(i))))
        await c.post_event(frame_event("v4", mime="text/html"))
        await c.post_event(frame_event("v4", b"x" * (MAX_FRAME_BYTES + 1)))
        await c.post_event(VideoFrameEvent(session_id="v4", frame_data=None))
        await asyncio.sleep(0.2)
        assert len(c._frames["v4"]) == FRAMES_PER_SESSION
    finally:
        await c.stop()


async def test_vision_failure_is_reported_and_does_not_continue():
    class Broken(VisionBackend):
        async def analyze(self, image, mime, question):
            raise VisionError("all vision models failed: 429")

    c, llm = await make([analyze()], Broken())
    try:
        await c.post_event(frame_event("v5"))
        await c.post_event(UserTextEvent(session_id="v5", text="What city is on this poster?"))
        actions = await collect(c, 0.6)
        replies = [a.text for a in actions if isinstance(a, SpokenResponseAction)]
        assert len(replies) == 1 and "couldn't analyze the image" in replies[0]
        assert len(llm.call_history) == 1                             # no continuation after a failure
    finally:
        await c.stop()


async def test_an_interrupt_cancels_an_in_flight_vision_call():
    vision = MockVisionBackend(["never delivered"], latency_s=2.0)
    c, llm = await make([analyze()], vision)
    try:
        await c.post_event(frame_event("v6"))
        await c.post_event(UserTextEvent(session_id="v6", text="What city is on this poster?"))
        await asyncio.sleep(0.4)
        await c.post_event(InterruptSignalEvent(session_id="v6", reason="user_barge_in"))
        actions = await collect(c, 2.6)
        cancels = [a for a in actions if isinstance(a, ToolCancelAction)]
        assert len(cancels) == 1 and cancels[0].tool_name == "analyze_frame"
        assert len(llm.call_history) == 1                             # the cancelled observation never re-planned
        assert not any(isinstance(a, SpokenResponseAction) for a in actions)
    finally:
        await c.stop()


async def test_text_only_turns_never_touch_the_vision_model():
    vision = MockVisionBackend()
    c, _ = await make([search(origin="Delhi", destination="Mumbai")], vision)
    try:
        await c.post_event(frame_event("v7"))                          # a frame is available but irrelevant
        await c.post_event(UserTextEvent(session_id="v7", text="Find flights from Delhi to Mumbai"))
        await collect(c, 0.6)
        assert vision.calls == []
    finally:
        await c.stop()


async def test_frames_are_redacted_from_the_trace():
    c, _ = await make([])
    try:
        await c.post_event(frame_event("v8", jpeg("SECRET")))
        await asyncio.sleep(0.2)
        records = [r for r in c.trace_logger.trace_history if r.get("event_type") == "video_frame"]
        assert records and records[0]["payload"]["frame_data"].endswith(" bytes>")
        json.dumps(c.trace_logger.trace_history)                       # serializable (bytes would raise)
    finally:
        await c.stop()


# ======================================================================= server protocol
def test_websocket_video_frame_message_reaches_the_coordinator(monkeypatch):
    from starlette.testclient import TestClient
    from agent import server

    fresh = AgentCoordinator(llm_backend=MockLLMBackend(), vision_backend=MockVisionBackend(), enable_debounce=False)
    monkeypatch.setattr(server, "coordinator", fresh)
    with TestClient(server.app) as client:
        with client.websocket_connect("/ws/vid_ws") as ws:
            good = base64.b64encode(jpeg()).decode()
            ws.send_text(json.dumps({"type": "video_frame", "mime": "image/jpeg", "data": good, "source": "screen"}))
            ws.send_text(json.dumps({"type": "video_frame", "mime": "image/jpeg", "data": "!!!not-base64!!!"}))
            ws.send_text(json.dumps({"type": "video_frame", "mime": "text/html", "data": good}))
            deadline = time.time() + 5
            while time.time() < deadline and not fresh._frames.get("vid_ws"):
                time.sleep(0.05)
            time.sleep(0.3)
            frames = fresh._frames["vid_ws"]
            assert len(frames) == 1 and frames[0].source == "screen"   # the two bad messages were dropped


# ========================================================================= live (real free VLM)
@pytest.mark.live
async def test_live_free_vision_model_reads_a_sign():
    import os
    if not os.getenv("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY not configured")
    from PIL import ImageFont
    img = Image.new("RGB", (640, 360), (18, 60, 120))
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 54)
    except OSError:
        pytest.skip("no TrueType font available to render the test sign")
    ImageDraw.Draw(img).text((40, 120), "FLIGHT TO GOA", fill=(255, 255, 255), font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    from dotenv import load_dotenv
    load_dotenv(".env")
    backend = OpenRouterVisionBackend()
    try:
        res = await backend.analyze(buf.getvalue(), "image/png", "What destination city is written on this sign?")
    except VisionError as e:
        pytest.skip(f"every free vision model is busy right now: {str(e)[:200]}")
    assert "goa" in res.answer.lower(), res
