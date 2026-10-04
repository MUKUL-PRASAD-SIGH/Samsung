"""The packaged desktop app's first-run chores: voice download, Windows browser discovery, console encoding."""

import io

import pytest

from agent import desktop, launcher


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_voice_is_downloaded_once_then_reused(tmp_path):
    calls = []

    def opener(url, timeout):
        calls.append(url)
        return _Resp(b"x" * 5000)

    said = []
    model = desktop.ensure_voice(tmp_path, opener=opener, say=said.append)
    assert model == tmp_path / desktop.VOICE_FILES[0] and model.stat().st_size == 5000
    assert len(calls) == 2 and all(u.startswith(desktop.VOICE_BASE) for u in calls)
    desktop.ensure_voice(tmp_path, opener=opener, say=said.append)
    assert len(calls) == 2                                            # already present: no second download


def test_offline_first_run_degrades_instead_of_crashing(tmp_path):
    def opener(url, timeout):
        raise OSError("no network")

    said = []
    assert desktop.ensure_voice(tmp_path, opener=opener, say=said.append) is None
    assert any("without spoken replies" in m for m in said) and not list(tmp_path.glob("*.part"))


def test_truncated_download_is_never_kept(tmp_path):
    assert desktop.ensure_voice(tmp_path, opener=lambda u, timeout: _Resp(b"tiny"), say=lambda m: None) is None
    assert not list(tmp_path.iterdir())


@pytest.fixture(autouse=True)
def isolated_environ(monkeypatch):
    """prepare_environment writes to os.environ: work on a copy so nothing leaks into other tests (a leaked TTS_VOICE_PATH
    broke the Piper tests once)."""
    import os

    monkeypatch.setattr(os, "environ", os.environ.copy())


def test_prepare_environment_points_tts_at_the_downloaded_voice(tmp_path, monkeypatch):
    monkeypatch.delenv("TTS_VOICE_PATH", raising=False)
    monkeypatch.delenv("TTS_BACKEND", raising=False)
    monkeypatch.setattr(desktop, "ensure_voice", lambda *a, **k: tmp_path / "v.onnx")
    desktop.prepare_environment()
    import os
    assert os.environ["TTS_VOICE_PATH"] == str(tmp_path / "v.onnx")


def test_prepare_environment_respects_an_explicit_voice_and_tts_off(monkeypatch):
    monkeypatch.setenv("TTS_VOICE_PATH", "/my/voice.onnx")
    monkeypatch.setattr(desktop, "ensure_voice", lambda *a, **k: pytest.fail("must not download"))
    desktop.prepare_environment()
    monkeypatch.delenv("TTS_VOICE_PATH")
    monkeypatch.setenv("TTS_BACKEND", "off")
    desktop.prepare_environment()


def test_windows_finds_edge_in_program_files(tmp_path, monkeypatch):
    edge = tmp_path / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    edge.parent.mkdir(parents=True)
    edge.write_text("")
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("PROGRAMFILES(X86)", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert launcher.find_browser() == str(edge)


def test_config_dir_is_appdata_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert launcher.config_dir() == tmp_path / "Kairos"
