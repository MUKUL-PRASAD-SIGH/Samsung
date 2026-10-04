"""export_artifact: code written by the agent becomes a real file, safely, and opens in the editor."""

import asyncio
import os
import stat
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agent import clock, exporter, server
from agent.coordinator import AgentCoordinator
from agent.llm_client import LLMResponse, MockLLMBackend
from agent.schemas.actions import ActionType, AgentStepAction
from agent.schemas.events import UserTextEvent
from agent.settings import reload_settings


@pytest.fixture(autouse=True)
def export_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "exports"))
    monkeypatch.setenv("EXPORT_OPEN", "0")
    return tmp_path / "exports"


# ----------------------------------------------------------------------------------------- file names
@pytest.mark.parametrize("name,language,expected", [
    ("debounce.ts", None, "debounce.ts"),
    ("My Script!.py", None, "My_Script_.py"),
    ("../../etc/passwd.py", None, "passwd.py"),                 # directory part dropped
    ("..\\..\\windows\\evil.js", None, "evil.js"),
    (".bashrc.py", None, "bashrc.py"),                         # no hidden files
    ("noext", "python", "noext.py"),                           # extension from the language
    (None, "typescript", "kairos_export.ts"),
    ("", None, "kairos_export.txt"),
    ("UPPER.PY", None, "UPPER.py"),
    ("x" * 300 + ".py", None, "x" * 80 + ".py"),
])
def test_safe_filename(name, language, expected):
    assert exporter.safe_filename(name, language) == expected


@pytest.mark.parametrize("name", ["run.sh", "setup.bat", "tool.exe", "x.ps1", "evil.dll", "a.desktop"])
def test_files_a_user_might_double_click_are_refused(name):
    with pytest.raises(exporter.ExportError):
        exporter.safe_filename(name, None)


# ------------------------------------------------------------------------------------------- writing
def test_export_writes_exact_content_never_overwrites_and_stays_inside_the_folder(export_env):
    a = exporter.export_file("print('hi')\n", "hello.py", "python")
    b = exporter.export_file("print('second')\n", "hello.py", "python")
    assert Path(a.path).read_text() == "print('hi')\n" and Path(b.path).read_text() == "print('second')\n"
    assert a.filename == "hello.py" and b.filename == "hello-1.py"          # the first file is untouched
    assert Path(a.path).parent == export_env
    assert a.bytes == len("print('hi')\n") and a.download_path == "/exports/hello.py"
    c = exporter.export_file("x", "../../escape.py")
    assert Path(c.path).parent == export_env and c.filename == "escape.py"


@pytest.mark.parametrize("content", ["", "   \n", None, 5])
def test_nothing_to_export_is_an_error_not_an_empty_file(content, export_env):
    with pytest.raises(exporter.ExportError):
        exporter.export_file(content, "a.py")
    assert not list(export_env.glob("*"))


def test_size_cap():
    with pytest.raises(exporter.ExportError, match="too large"):
        exporter.export_file("x" * (exporter.MAX_BYTES + 1), "big.txt")
    exporter.export_file("x" * exporter.MAX_BYTES, "ok.txt")


def test_unicode_survives_a_round_trip():
    r = exporter.export_file("# héllo 日本語 🙂\nprint('ok')\n", "u.py")
    assert Path(r.path).read_text(encoding="utf-8") == "# héllo 日本語 🙂\nprint('ok')\n"


def test_editor_uri_is_percent_encoded_and_absolute(export_env):
    r = exporter.export_file("x", "my file.py")
    assert r.editor_uri.startswith("vscode://file/") and " " not in r.editor_uri and r.editor_uri.endswith("my_file.py")


# ----------------------------------------------------------------------------------- opening the editor
def _fake_editor(tmp_path, monkeypatch):
    log = tmp_path / "editor.log"
    exe = tmp_path / "bin" / "code"
    exe.parent.mkdir()
    exe.write_text(f'#!/bin/sh\necho "$@" > "{log}"\n')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{exe.parent}:{os.environ['PATH']}")
    return log


def test_the_editor_is_launched_with_the_file_when_available(tmp_path, monkeypatch):
    log = _fake_editor(tmp_path, monkeypatch)
    monkeypatch.setenv("EXPORT_OPEN", "1")
    r = exporter.export_file("print(1)", "run.py")
    assert r.opened_with == "code"
    for _ in range(50):
        if log.exists() and log.read_text().strip():
            break
        import time

        time.sleep(0.05)
    assert log.read_text().split() == ["--reuse-window", r.path]


def test_open_none_disables_the_editor_and_a_missing_editor_never_fails_the_export(tmp_path, monkeypatch):
    log = _fake_editor(tmp_path, monkeypatch)
    monkeypatch.setenv("EXPORT_OPEN", "1")
    assert exporter.export_file("1", "a.py", open_in="none").opened_with is None
    monkeypatch.setenv("EXPORT_EDITOR_CMD", "definitely-not-an-editor")
    r = exporter.export_file("1", "b.py")
    assert r.opened_with is None and Path(r.path).exists()
    assert not log.exists()


def test_a_hostile_filename_cannot_inject_editor_arguments(tmp_path, monkeypatch):
    _fake_editor(tmp_path, monkeypatch)
    monkeypatch.setenv("EXPORT_OPEN", "1")
    r = exporter.export_file("1", "--install-extension evil.py; rm -rf x.py")
    assert Path(r.path).is_absolute()                       # the editor gets an absolute path, so a leading "-" can't be an option
    assert ";" not in r.filename and " " not in r.filename and r.filename.endswith(".py")


# ------------------------------------------------------------------------------------------- downloads
def test_resolve_export_only_returns_files_inside_the_folder(export_env):
    r = exporter.export_file("data", "ok.txt")
    assert exporter.resolve_export("ok.txt") == Path(r.path)
    for bad in ("../ok.txt", "/etc/passwd", ".hidden", "sub/ok.txt", "", "missing.txt", "..", "ok.txt/.."):
        assert exporter.resolve_export(bad) is None, bad


# --------------------------------------------------------------------------------- through the agent
def _coord(*responses):
    return AgentCoordinator(llm_backend=MockLLMBackend(list(responses)), enable_debounce=False)


def _drain(c):
    out = []
    while not c.action_queue.empty():
        out.append(c.action_queue.get_nowait())
    return out


def _tool(**args):
    return LLMResponse(response_type="tool_call", tool_name="export_artifact", arguments=args)


async def _run(c, text="export it", settle=1.0):
    await c.start()
    await c.post_event(UserTextEvent(session_id="s", text=text))
    await asyncio.sleep(settle)
    await c.stop()
    return _drain(c)


async def test_the_model_can_write_code_and_export_it_in_one_call(export_env):
    c = _coord(_tool(filename="fizz.py", language="python", content="for i in range(3): print(i)\n"))
    acts = await _run(c, "write fizz and open in vscode")
    exported = [a for a in acts if a.action_type == ActionType.FILE_EXPORTED]
    assert len(exported) == 1 and exported[0].filename == "fizz.py" and exported[0].bytes > 0
    assert (export_env / "fizz.py").read_text() == "for i in range(3): print(i)\n"
    reply = next(a for a in acts if a.action_type == ActionType.SPOKEN_RESPONSE)
    assert "fizz.py" in reply.text and "KB" in reply.text
    snap = [a for a in acts if a.action_type == ActionType.STATE_SNAPSHOT][-1]
    assert "content" not in snap.slots and "print" not in str(snap.slots)       # the file text never becomes a slot


async def test_export_with_no_arguments_exports_the_workers_latest_artifact(export_env):
    c = _coord(_tool(open_in="none"))
    await c.emit_action(AgentStepAction(session_id="s", epoch=1, call_id="w1", name="coder", role="Coder", step=3, total_steps=3,
                                        thought="done", status="completed",
                                        artifact={"title": "clock.tsx", "language": "tsx", "content": "export const A = () => null;\n"}))
    acts = await _run(c)
    exported = [a for a in acts if a.action_type == ActionType.FILE_EXPORTED]
    assert exported and exported[0].filename == "clock.tsx"
    assert (export_env / "clock.tsx").read_text() == "export const A = () => null;\n"


async def test_export_with_nothing_to_export_says_so_and_writes_nothing(export_env):
    acts = await _run(_coord(_tool(open_in="vscode")))
    assert not [a for a in acts if a.action_type == ActionType.FILE_EXPORTED]
    reply = next(a for a in acts if a.action_type == ActionType.SPOKEN_RESPONSE)
    assert "don't have any code" in reply.text and not list(export_env.glob("*"))


async def test_a_refused_export_is_reported_not_swallowed(export_env):
    acts = await _run(_coord(_tool(filename="install.sh", content="rm -rf ~\n")))
    reply = next(a for a in acts if a.action_type == ActionType.SPOKEN_RESPONSE)
    assert "won't write a .sh" in reply.text and not list(export_env.glob("*"))


async def test_the_same_export_asked_twice_runs_once(export_env):
    c = _coord(_tool(filename="a.py", content="x = 1\n"), _tool(filename="a.py", content="x = 1\n"))
    await c.start()
    await c.post_event(UserTextEvent(session_id="s", text="save it"))
    await asyncio.sleep(0.6)
    await c.post_event(UserTextEvent(session_id="s", text="save it"))
    await asyncio.sleep(0.8)
    await c.stop()
    assert sorted(p.name for p in export_env.glob("*")) == ["a.py"]               # idempotency: no a-1.py


def test_artifacts_are_bounded_per_session():
    async def main():
        c = _coord()
        for i in range(25):
            await c.emit_action(AgentStepAction(session_id="s", epoch=1, call_id=f"w{i}", name="n", role="r", step=1, total_steps=1,
                                                thought="t", status="completed", artifact={"title": f"{i}.py", "language": "python", "content": "x"}))
        return len(c._artifacts["s"]), c._artifacts["s"][-1]["title"]

    assert clock.run_virtual(main()) == (10, "24.py")


# ------------------------------------------------------------------------------------------- timer
def test_a_timer_finishes_announces_itself_and_can_be_cancelled():
    async def run(cancel):
        c = AgentCoordinator(llm_backend=MockLLMBackend([LLMResponse(response_type="tool_call", tool_name="set_timer",
                                                                     arguments={"seconds": 30, "label": "tea"})]), enable_debounce=False)
        await c.start()
        await c.post_event(UserTextEvent(session_id="s", text="set a 30 second timer for tea"))
        await asyncio.sleep(5)
        if cancel:
            from agent.schemas.events import InterruptSignalEvent

            await c.post_event(InterruptSignalEvent(session_id="s", reason="ui"))
        await asyncio.sleep(40)
        await c.stop()
        return [a for a in _drain(c) if a.action_type == ActionType.SPOKEN_RESPONSE]

    done = clock.run_virtual(run(False))
    assert len(done) == 1 and "tea" in done[0].text and "30 seconds" in done[0].text
    assert clock.run_virtual(run(True)) == []                                    # cancelled: never announces itself


# --------------------------------------------------------------------------------------------- HTTP
@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(server, "coordinator", _coord())
    reload_settings()
    server.reset_runtime_state()
    with TestClient(server.app) as c:
        yield c
    reload_settings()


def test_download_serves_exported_files_and_refuses_everything_else(client, export_env):
    r = exporter.export_file("print('dl')\n", "dl.py")
    got = client.get(r.download_path)
    assert got.status_code == 200 and got.text == "print('dl')\n"
    assert 'attachment; filename="dl.py"' in got.headers["content-disposition"]
    for bad in ("/exports/missing.py", "/exports/..%2F..%2Fetc%2Fpasswd", "/exports/.hidden"):
        assert client.get(bad).status_code == 404


def test_downloads_need_the_token_when_one_is_configured(client, export_env, monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "k3y")
    reload_settings()
    exporter.export_file("secret", "s.txt")
    assert client.get("/exports/s.txt").status_code == 401
    assert client.get("/exports/s.txt", headers={"authorization": "Bearer k3y"}).text == "secret"


def test_auth_check_and_health_tell_the_login_screen_what_it_needs(client, monkeypatch):
    assert client.get("/health").json()["auth_required"] is False
    assert client.get("/auth/check").status_code == 200                          # open server: anyone passes
    monkeypatch.setenv("AUTH_TOKEN", "k3y")
    reload_settings()
    assert client.get("/health").json() == {"status": "ok", "auth_required": True}
    assert client.get("/auth/check").status_code == 401
    assert client.get("/auth/check", headers={"authorization": "Bearer nope"}).status_code == 401
    assert client.get("/auth/check", headers={"authorization": "Bearer k3y"}).json() == {"ok": True}
