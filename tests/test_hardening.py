"""Phase G: settings, request protection, structured logs and metrics."""

import json
import logging

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent import clock, metrics, security, server
from agent.coordinator import AgentCoordinator
from agent.llm_client import MockLLMBackend
from agent.logging_setup import JsonFormatter, TextFormatter, epoch_var, session_id_var, setup_logging
from agent.multimodal.vision import MockVisionBackend
from agent.settings import Settings, reload_settings


# ---------------------------------------------------------------------------------------------- settings
def test_settings_defaults_are_conservative(monkeypatch):
    for k in ("AUTH_TOKEN", "ALLOWED_ORIGINS", "TRUST_PROXY", "MAX_CONNECTIONS_PER_IP", "LOG_FORMAT"):
        monkeypatch.delenv(k, raising=False)
    s = Settings.from_env()
    assert s.auth_token is None and s.allowed_origins == [] and s.trust_proxy is False
    assert s.max_connections_per_ip == 20 and s.log_format == "text" and s.metrics_enabled
    assert any("AUTH_TOKEN is not set" in w for w in s.warnings())


def test_settings_parse_env_and_ignore_garbage(monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "s3cret")
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://a.example, https://b.example ,")
    monkeypatch.setenv("TRUST_PROXY", "yes")
    monkeypatch.setenv("MAX_SESSIONS", "not-a-number")
    monkeypatch.setenv("MSG_RATE_PER_S", "2.5")
    s = Settings.from_env()
    assert s.allowed_origins == ["https://a.example", "https://b.example"] and s.trust_proxy is True
    assert s.max_sessions == 500 and s.msg_rate_per_s == 2.5            # bad value -> default, never a crash
    assert not any("AUTH_TOKEN" in w for w in s.warnings())


def test_describe_never_leaks_the_token(monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "supersecretvalue")
    d = Settings.from_env().describe()
    assert d["auth_token"] == "<set>" and "supersecretvalue" not in json.dumps(d)
    assert any("ALLOWED_ORIGINS=*" in w for w in Settings(allowed_origins=["*"]).warnings())


# ------------------------------------------------------------------------------------------ pure rules
@pytest.mark.parametrize("sid,ok", [("sess_abc123", True), ("a-b_C9", True), ("x" * 64, True), ("x" * 65, False), ("", False),
                                    ("../etc/passwd", False), ("a b", False), ("a;b", False), ("<script>", False), ("naïve", False)])
def test_session_id_validation(sid, ok):
    assert security.valid_session_id(sid, Settings()) is ok


@pytest.mark.parametrize("origin,host,allowed,expected", [
    (None, "api.example.com", [], True),                                   # scripts / the eval kit send no Origin
    ("http://localhost:5173", "localhost:8000", [], True),                 # vite dev server
    ("http://127.0.0.1:3000", "x", [], True),
    ("https://app.example.com", "app.example.com", [], True),              # same-origin deployment
    ("https://evil.example", "app.example.com", [], False),                # cross-site WebSocket hijack
    ("https://evil.example", "localhost:8000", [], False),
    ("https://app.example.com", "other", ["https://app.example.com"], True),
    ("https://evil.example", "app.example.com", ["https://app.example.com"], False),   # an explicit list is exhaustive
    ("http://localhost:5173", "x", ["https://app.example.com"], False),
    ("https://evil.example", "x", ["*"], True),
    ("null", "x", [], False),
])
def test_origin_policy(origin, host, allowed, expected):
    assert security.origin_allowed(origin, host, Settings(allowed_origins=allowed)) is expected


def test_token_extraction_and_comparison():
    s = Settings(auth_token="tok")
    assert security.bearer_token({"authorization": "Bearer tok"}, {}) == "tok"
    assert security.bearer_token({}, {"token": "tok"}) == "tok"
    assert security.bearer_token({"authorization": "Basic abc"}, {}) is None
    assert security.token_ok("tok", s) and not security.token_ok("nope", s) and not security.token_ok(None, s)
    assert security.token_ok(None, Settings())          # no AUTH_TOKEN configured: open


def test_client_ip_only_trusts_forwarded_for_when_told_to():
    h = {"x-forwarded-for": "9.9.9.9, 10.0.0.1"}
    assert security.client_ip("1.2.3.4", h, Settings(trust_proxy=False)) == "1.2.3.4"   # else a client could pick its own IP
    assert security.client_ip("1.2.3.4", h, Settings(trust_proxy=True)) == "9.9.9.9"
    assert security.client_ip(None, {}, Settings()) == "unknown"


def test_token_bucket_refills_over_time():
    import asyncio

    async def main():
        b = security.TokenBucket(rate=2, burst=3)
        burst = [b.allow() for _ in range(5)]
        await asyncio.sleep(1.0)                  # +2 tokens
        after = [b.allow() for _ in range(3)]
        big = security.TokenBucket(rate=100, burst=100).allow(500)   # a single oversized request is refused outright
        return burst, after, big

    burst, after, big = clock.run_virtual(main())
    assert burst == [True, True, True, False, False] and after == [True, True, False] and big is False


def test_connection_limiter_counts_and_releases():
    lim = security.ConnectionLimiter(2)
    assert lim.acquire("a") and lim.acquire("a") and not lim.acquire("a") and lim.acquire("b")
    lim.release("a")
    assert lim.acquire("a") and lim.count("a") == 2
    lim.release("zzz")                           # releasing something never acquired must not go negative


# --------------------------------------------------------------------------------------------- server
@pytest.fixture()
def make_client(monkeypatch):
    clients = []

    def make(**env):
        for key in ("AUTH_TOKEN", "ALLOWED_ORIGINS", "MAX_CONNECTIONS_PER_IP", "MAX_SESSIONS", "MSG_RATE_PER_S", "MSG_BURST",
                    "MAX_USER_TEXT_CHARS", "MAX_TEXT_MESSAGE_BYTES", "TRUST_PROXY", "WARMUP_MIN_INTERVAL_S", "AUDIO_BYTES_PER_S",
                    "AUDIO_BURST_BYTES"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, str(value))
        reload_settings()
        server.reset_runtime_state()
        monkeypatch.setattr(server, "coordinator", AgentCoordinator(
            llm_backend=MockLLMBackend(), vision_backend=MockVisionBackend(), enable_debounce=False))
        monkeypatch.setattr(server, "VIOLATIONS_BEFORE_DISCONNECT", 5)
        c = TestClient(server.app)
        c.__enter__()
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.__exit__(None, None, None)
    reload_settings()
    server.reset_runtime_state()


def _refused(client, path, **kw):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(path, **kw):
            pass


def test_hostile_session_ids_never_create_a_session(make_client):
    c = make_client()
    before = metrics.REJECTED.value(reason="session_id")
    for sid in ("a%20b", "x" * 80, "a%3Bb", "%3Cscript%3E"):
        _refused(c, f"/ws/{sid}")
    assert server.coordinator.sessions == {} and metrics.REJECTED.value(reason="session_id") == before + 4


def test_cross_site_websocket_is_refused_but_localhost_and_scripts_are_not(make_client):
    c = make_client()
    _refused(c, "/ws/s1", headers={"origin": "https://evil.example"})
    assert "s1" not in server.coordinator.sessions
    with c.websocket_connect("/ws/s2", headers={"origin": "http://localhost:5173"}):
        pass
    with c.websocket_connect("/ws/s3"):          # no Origin header at all
        pass


def test_explicit_origin_allow_list(make_client):
    c = make_client(ALLOWED_ORIGINS="https://app.example.com")
    with c.websocket_connect("/ws/ok", headers={"origin": "https://app.example.com"}):
        pass
    _refused(c, "/ws/nope", headers={"origin": "http://localhost:5173"})


def test_auth_token_protects_websocket_and_sensitive_http(make_client):
    c = make_client(AUTH_TOKEN="hunter2")
    _refused(c, "/ws/s1")
    _refused(c, "/ws/s1?token=wrong")
    with c.websocket_connect("/ws/s1?token=hunter2"):
        pass
    with c.websocket_connect("/ws/s1", headers={"authorization": "Bearer hunter2"}):
        pass
    assert c.post("/warmup").status_code == 401 and c.get("/metrics").status_code == 401
    assert c.get("/metrics", headers={"authorization": "Bearer hunter2"}).status_code == 200
    anon = c.get("/health").json()
    assert anon == {"status": "ok", "auth_required": True}               # a load balancer learns it is up; the login screen learns a key is needed
    assert "sessions" in c.get("/health", headers={"authorization": "Bearer hunter2"}).json()


def test_per_ip_connection_cap_and_release(make_client):
    c = make_client(MAX_CONNECTIONS_PER_IP=2)
    with c.websocket_connect("/ws/a"), c.websocket_connect("/ws/b"):
        _refused(c, "/ws/c")
    with c.websocket_connect("/ws/d"):           # closing frees slots again
        pass


def test_global_session_cap_refuses_new_sessions_but_not_existing_ones(make_client):
    c = make_client(MAX_SESSIONS=2)
    with c.websocket_connect("/ws/a"):
        pass
    with c.websocket_connect("/ws/b"):
        pass
    _refused(c, "/ws/c")
    with c.websocket_connect("/ws/a"):           # a returning session is not a new one
        pass


def _recv_json(ws):
    return json.loads(ws.receive_text())


def test_message_flood_is_throttled_and_then_disconnected(make_client):
    c = make_client(MSG_RATE_PER_S=0.1, MSG_BURST=3)
    with c.websocket_connect("/ws/flood") as ws:
        for _ in range(12):
            ws.send_text(json.dumps({"type": "interrupt"}))
        codes = []
        with pytest.raises(WebSocketDisconnect):
            for _ in range(40):
                m = _recv_json(ws)
                if m.get("type") == "error":
                    codes.append(m["code"])
    assert codes and set(codes) == {"rate_limit"}
    assert metrics.REJECTED.value(reason="rate_limit") >= 5


def test_oversized_and_malformed_messages_are_rejected_without_killing_the_server(make_client):
    c = make_client(MAX_USER_TEXT_CHARS=50, MAX_TEXT_MESSAGE_BYTES=2000)
    with c.websocket_connect("/ws/big") as ws:
        ws.send_text(json.dumps({"type": "user_text", "text": "x" * 500}))
        assert _recv_json(ws) == {"type": "error", "code": "too_large"}
        ws.send_text("y" * 5000)
        assert _recv_json(ws) == {"type": "error", "code": "too_large"}
        ws.send_text("{this is not json")
        assert _recv_json(ws) == {"type": "error", "code": "bad_json"}
        ws.send_text(json.dumps([1, 2, 3]))      # valid JSON, wrong shape: ignored
        ws.send_text(json.dumps({"type": "user_text", "text": "Find flights from Delhi to Mumbai"}))
        assert _recv_json(ws)["action_type"] == "filler"      # still healthy afterwards


def test_audio_flood_is_throttled(make_client):
    c = make_client(AUDIO_BYTES_PER_S=1000, AUDIO_BURST_BYTES=5000)
    before = metrics.REJECTED.value(reason="rate_limit")
    with c.websocket_connect("/ws/audio") as ws:
        for _ in range(4):
            ws.send_bytes(b"\x00" * 3200)
        assert _recv_json(ws) == {"type": "error", "code": "rate_limit"}
    assert metrics.REJECTED.value(reason="rate_limit") > before


def test_warmup_is_rate_limited_because_it_spends_llm_tokens(make_client, monkeypatch):
    c = make_client(WARMUP_MIN_INTERVAL_S=60)
    calls = []

    async def fake(coordinator, include_llm=True):
        calls.append(include_llm)
        return {"all_ok": True}

    monkeypatch.setattr(server, "run_full_warmup", fake)
    assert c.post("/warmup").status_code == 200
    r = c.post("/warmup")
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0 and calls == [True]


def test_every_response_carries_a_request_id_and_security_headers(make_client):
    c = make_client()
    r = c.get("/health", headers={"x-request-id": "abc123"})
    assert r.headers["x-request-id"] == "abc123"
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    hostile = c.get("/health", headers={"x-request-id": "a b\r\nSet-Cookie: x"})      # never reflected
    assert hostile.headers["x-request-id"].isalnum() and "set-cookie" not in hostile.headers


# ---------------------------------------------------------------------------------------------- metrics
def test_metrics_endpoint_reports_agent_reactions_in_prometheus_format(make_client):
    c = make_client()
    with c.websocket_connect("/ws/m1") as ws:
        ws.send_text(json.dumps({"type": "user_text", "text": "Find flights from Delhi to Mumbai"}))
        assert _recv_json(ws)["action_type"] == "filler"
        _recv_json(ws)                            # tool_call
    r = c.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert 'agent_events_total{type="user_text"}' in body
    assert 'agent_actions_total{type="filler"}' in body and "agent_first_ack_seconds_count" in body
    assert "agent_sessions 1" in body and "# TYPE agent_first_ack_seconds histogram" in body
    for line in body.strip().splitlines():
        assert line.startswith("#") or len(line.rsplit(" ", 1)) == 2 and float(line.rsplit(" ", 1)[1]) >= 0, line


def test_metrics_can_be_disabled(make_client):
    c = make_client()
    import agent.settings as st

    st._settings.metrics_enabled = False
    assert c.get("/metrics").status_code == 404


def test_histogram_buckets_are_cumulative():
    h = metrics.Histogram("t_seconds", "t", buckets=(0.1, 1.0))
    for v in (0.05, 0.5, 5.0):
        h.observe(v)
    lines = {l.split(" ")[0]: float(l.split(" ")[1]) for l in h.render() if not l.startswith("#")}
    assert lines['t_seconds_bucket{le="0.1"}'] == 1 and lines['t_seconds_bucket{le="1"}'] == 2
    assert lines['t_seconds_bucket{le="+Inf"}'] == 3 and lines["t_seconds_count"] == 3 and lines["t_seconds_sum"] == pytest.approx(5.55)


def test_interrupt_cancel_latency_and_llm_outcomes_are_recorded():
    async def main():
        c = AgentCoordinator(llm_backend=MockLLMBackend(), enable_debounce=False)
        s = c.get_or_create_session("lat")
        await c.emit_action(s.register_tool_call(call_id="c1", tool_name="search_flights", arguments={"origin": "DEL", "destination": "BOM"}))
        from agent.schemas.events import InterruptSignalEvent

        n = metrics.CANCEL_LATENCY.count()
        await c.post_event(InterruptSignalEvent(session_id="lat", reason="ui"))
        await c.start()
        await asyncio.sleep(0.3)
        await c.stop()
        return metrics.CANCEL_LATENCY.count() - n

    import asyncio

    assert clock.run_virtual(main()) == 1


async def test_llm_client_records_ok_timeout_and_error_outcomes():
    from agent.llm_client import CircuitBreakerLLMClient, LLMBackend, LLMConfig, LLMResponse, RateLimitError

    class Behave(LLMBackend):
        def __init__(self, what): self.what = what
        async def generate(self, messages, tools=None):
            if self.what == "ok": return LLMResponse(response_type="spoken_response", content="hi")
            if self.what == "429": raise RateLimitError("slow down", 60)
            raise RuntimeError("boom")

    def cfg(): return LLMConfig(backend_type="mock", timeout_s=2, soft_deadline_s=0)
    base = {o: metrics.LLM_REQUESTS.value(outcome=o) for o in ("ok", "error", "rate_limited")}
    await CircuitBreakerLLMClient(Behave("ok"), cfg()).generate([])
    await CircuitBreakerLLMClient(Behave("429"), cfg()).generate([])
    await CircuitBreakerLLMClient(Behave("err"), cfg()).generate([])
    now = {o: metrics.LLM_REQUESTS.value(outcome=o) for o in base}
    assert [now[o] - base[o] for o in ("ok", "error", "rate_limited")] == [1, 1, 1]


# ------------------------------------------------------------------------------------------- logging
def _record(msg="hello", **extra):
    r = logging.LogRecord("agent.test", logging.INFO, __file__, 1, msg, (), None)
    r.__dict__.update(extra)
    return r


def test_json_logs_carry_session_epoch_and_extras():
    from agent.logging_setup import ContextFilter

    session_id_var.set("sess_1")
    epoch_var.set(7)
    rec = _record("cancelled %s", )
    rec.args = ("c9",)
    ContextFilter().filter(rec)
    out = json.loads(JsonFormatter().format(rec))
    assert out["msg"] == "cancelled c9" and out["level"] == "INFO" and out["logger"] == "agent.test"
    assert out["session_id"] == "sess_1" and out["epoch"] == 7 and out["ts"].endswith("Z")
    rec2 = _record("with extra", call_id="c1", tool="book_flight")
    assert json.loads(JsonFormatter().format(rec2))["call_id"] == "c1"
    session_id_var.set(None)
    epoch_var.set(None)


def test_json_log_survives_unserialisable_extras_and_exceptions():
    try:
        raise ValueError("bad")
    except ValueError:
        import sys

        rec = _record("failed", obj=object())
        rec.exc_info = sys.exc_info()
    out = json.loads(JsonFormatter().format(rec))
    assert "ValueError: bad" in out["exc"] and "obj" in out


def test_text_format_shows_context_and_setup_is_idempotent(capsys):
    from agent.logging_setup import ContextFilter

    session_id_var.set("s9")
    rec = _record("hi")
    ContextFilter().filter(rec)
    assert "[sid=s9]" in TextFormatter().format(rec)
    session_id_var.set(None)
    setup_logging("json", "INFO")
    setup_logging("json", "INFO")
    assert sum(1 for h in logging.getLogger().handlers if getattr(h, "_agent_handler", False)) == 1
    setup_logging("text", "INFO")
