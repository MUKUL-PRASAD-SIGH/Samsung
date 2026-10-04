"""API keys entered in the UI: storage, validation, masking, and the /settings/keys endpoints."""

import json

import pytest
from starlette.testclient import TestClient

from agent import keystore, server
from agent.coordinator import AgentCoordinator
from agent.llm_client import MockLLMBackend
from agent.multimodal.vision import MockVisionBackend
from agent.settings import reload_settings


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Isolated config dir and environment: nothing here may touch the developer's real keys."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr("sys.platform", "linux")
    for env in keystore.PROVIDERS.values():
        monkeypatch.delenv(env, raising=False)
    monkeypatch.delenv("USE_LOCAL_LLM", raising=False)
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    reload_settings()
    return tmp_path


def test_clean_strips_quotes_whitespace_and_bearer():
    assert keystore.clean('  "gsk_abc"  ') == "gsk_abc"
    assert keystore.clean("Bearer sk-or-xyz") == "sk-or-xyz"
    assert keystore.clean(None) == ""


@pytest.mark.parametrize("provider,key,ok", [("groq", "gsk_" + "a" * 20, True), ("groq", "sk-or-aaaa", False),
                                             ("openrouter", "sk-or-v1-" + "b" * 20, True), ("openrouter", "gsk_zzz", False),
                                             ("groq", "gsk_has space", False), ("groq", "", True), ("groq", "gsk_é", False)])
def test_validate_format(provider, key, ok):
    assert (keystore.validate_format(provider, key) is None) is ok


def test_save_persists_applies_env_and_masks(store):
    key = "gsk_" + "1234567890abcdef"
    keystore.save({"groq": key})
    path = keystore.keys_path()
    assert json.loads(path.read_text()) == {"groq": key} and (path.stat().st_mode & 0o077) == 0
    import os
    assert os.environ["GROQ_API_KEY"] == key
    st = keystore.status()
    assert st["backend"] == "groq" and st["configured"] and key not in json.dumps(st)
    assert st["groq"]["hint"] == "gsk_…cdef" and not st["openrouter"]["configured"]
    keystore.save({"groq": ""})                      # empty string removes it
    assert keystore.status()["backend"] == "mock" and json.loads(path.read_text()) == {}


def test_stored_keys_load_but_never_override_the_environment(store, monkeypatch):
    keystore.keys_path().write_text(json.dumps({"groq": "gsk_stored_value", "openrouter": "sk-or-stored"}))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-from-shell")
    keystore.load_into_environment()
    import os
    assert os.environ["GROQ_API_KEY"] == "gsk_stored_value" and os.environ["OPENROUTER_API_KEY"] == "sk-or-from-shell"


def test_corrupt_key_file_is_ignored(store):
    keystore.keys_path().write_text("{not json")
    assert keystore.status()["backend"] == "mock"


@pytest.fixture()
def client(store, monkeypatch):
    monkeypatch.setattr(keystore, "check_online", lambda provider, key, timeout=8.0: True)
    monkeypatch.setenv("INTENT_EMBEDDINGS", "0")      # a light coordinator: no model loads per test
    light = AgentCoordinator(llm_backend=MockLLMBackend(), vision_backend=MockVisionBackend(), enable_debounce=False)
    monkeypatch.setattr(server, "coordinator", light)
    reloaded = []
    monkeypatch.setattr(light, "reload_llm", lambda: reloaded.append(1))
    with TestClient(server.app) as c:
        c.reloaded = reloaded
        yield c


def test_put_keys_saves_validates_and_hot_swaps(client):
    r = client.put("/settings/keys", json={"groq_api_key": " gsk_" + "x" * 24 + " ", "openrouter_api_key": "sk-or-v1-" + "y" * 20})
    assert r.status_code == 200
    body = r.json()
    assert body["backend"] == "groq" and body["groq"]["configured"] and body["openrouter"]["configured"] and body["warnings"] == []
    assert "x" * 24 not in r.text and client.reloaded == [1]
    assert client.get("/settings/keys").json()["backend"] == "groq"
    assert client.get("/health").json()["llm"] == {"backend": "groq", "configured": True}


def test_put_keys_rejects_bad_input_and_changes_nothing(client):
    assert client.put("/settings/keys", json={"groq_api_key": "not-a-groq-key"}).status_code == 400
    assert client.put("/settings/keys", json={"groq_api_key": 123}).status_code == 400
    assert client.put("/settings/keys", content=b"[1]", headers={"content-type": "application/json"}).status_code == 400
    assert client.put("/settings/keys", content=b"{oops").status_code == 400
    assert client.get("/settings/keys").json()["configured"] is False and client.reloaded == []


def test_provider_refusal_is_reported_and_not_saved(client, monkeypatch):
    monkeypatch.setattr(keystore, "check_online", lambda provider, key, timeout=8.0: False)
    r = client.put("/settings/keys", json={"groq_api_key": "gsk_" + "z" * 24})
    assert r.status_code == 400 and r.json()["field"] == "groq" and client.get("/settings/keys").json()["configured"] is False


def test_unreachable_provider_still_saves_with_a_warning(client, monkeypatch):
    monkeypatch.setattr(keystore, "check_online", lambda provider, key, timeout=8.0: None)
    r = client.put("/settings/keys", json={"openrouter_api_key": "sk-or-v1-" + "q" * 20})
    assert r.status_code == 200 and r.json()["warnings"] and r.json()["backend"] == "openrouter"


def test_put_keys_refuses_a_foreign_browser_origin(client):
    r = client.put("/settings/keys", json={"groq_api_key": "gsk_" + "a" * 24}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and client.get("/settings/keys").json()["configured"] is False


def test_keys_endpoints_need_the_token_when_one_is_configured(client, monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "tok")
    reload_settings()
    assert client.get("/settings/keys").status_code == 401
    assert client.put("/settings/keys", json={}).status_code == 401
    assert client.get("/settings/keys", headers={"authorization": "Bearer tok"}).status_code == 200


def test_reload_llm_really_switches_the_running_coordinator(store, monkeypatch):
    from agent.llm_client import GroqBackend, OpenRouterBackend

    monkeypatch.setenv("INTENT_EMBEDDINGS", "0")
    c = AgentCoordinator(llm_backend=MockLLMBackend(), vision_backend=MockVisionBackend(), enable_debounce=False)
    old_planner = c.planner
    monkeypatch.setenv("GROQ_API_KEY", "gsk_" + "k" * 20)
    c.reload_llm()
    assert isinstance(c.llm_backend, GroqBackend) and c.planner is not old_planner
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-" + "k" * 20)
    c.reload_llm()
    assert isinstance(c.llm_backend, OpenRouterBackend)
