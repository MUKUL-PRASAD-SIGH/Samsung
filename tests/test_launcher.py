"""`kairos`: one-step startup. The decisions (auth policy, ports, browser) are pure and tested; one test starts the real thing."""

import os
import socket
import stat
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from agent import launcher


@pytest.fixture(autouse=True)
def config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    return tmp_path / "cfg"


def test_the_access_key_is_created_once_private_and_reused(config_home):
    k1 = launcher.load_or_create_key()
    k2 = launcher.load_or_create_key()
    path = config_home / "kairos" / "key"
    assert k1 == k2 and len(k1) >= 20
    assert stat.S_IMODE(path.stat().st_mode) == 0o600              # readable by you only


@pytest.mark.parametrize("host,login,no_auth,env,expect_key", [
    ("127.0.0.1", False, False, None, False),      # your own machine: no friction
    ("127.0.0.1", True, False, None, True),        # --login: use the sign-in screen
    ("0.0.0.0", False, False, None, True),         # exposed to the network: a key is required, automatically
    ("0.0.0.0", False, True, None, True),          # --no-auth is ignored when exposed
    ("192.168.1.9", False, False, None, True),
    ("127.0.0.1", False, True, None, False),
])
def test_auth_policy(host, login, no_auth, env, expect_key):
    assert (launcher.decide_auth(host, login, no_auth, env) is not None) is expect_key


def test_an_explicit_auth_token_always_wins():
    assert launcher.decide_auth("127.0.0.1", False, True, "mine") == "mine"
    assert launcher.decide_auth("0.0.0.0", False, False, "mine") == "mine"


def test_port_detection():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    port = s.getsockname()[1]
    assert not launcher.port_free("127.0.0.1", port)
    s.close()
    assert launcher.port_free("127.0.0.1", port)


def test_browser_discovery_prefers_chrome_and_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert launcher.find_browser() is None
    for name in ("chromium", "google-chrome-stable"):
        exe = tmp_path / name
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
    assert Path(launcher.find_browser()).name == "google-chrome-stable"


def test_open_app_uses_an_app_window_with_its_own_profile(tmp_path, monkeypatch, config_home):
    log = tmp_path / "args.txt"
    exe = tmp_path / "bin" / "chromium"
    exe.parent.mkdir()
    exe.write_text(f'#!/bin/sh\necho "$@" > "{log}"\n')
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(exe.parent))
    assert launcher.open_app("http://127.0.0.1:8000") == "chromium"
    for _ in range(50):
        if log.exists() and log.read_text().strip():
            break
        time.sleep(0.05)
    args = log.read_text()
    assert "--app=http://127.0.0.1:8000" in args and f"--user-data-dir={config_home / 'kairos' / 'browser-profile'}" in args


def test_open_app_falls_back_to_the_default_browser(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    opened = []
    monkeypatch.setattr(launcher.webbrowser, "open", lambda u: opened.append(u))
    assert launcher.open_app("http://x") == "default browser" and opened == ["http://x"]


class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, *a):
        pass


def test_waiting_for_the_server_and_detecting_one_already_running():
    srv = HTTPServer(("127.0.0.1", 0), _Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    assert launcher.health(url) == {"status": "ok"} and launcher.wait_until_ready(url, timeout=2)
    srv.shutdown()
    assert launcher.health(url, timeout=0.3) is None and not launcher.wait_until_ready(url, timeout=0.6)


def test_the_desktop_entry_launches_the_right_python_in_the_right_folder(tmp_path):
    home = tmp_path / "home"
    entry = launcher.install_shortcut(python="/opt/venv/bin/python", cwd=Path("/srv/kairos"), home=home)
    text = entry.read_text()
    assert entry == home / ".local/share/applications/kairos.desktop"
    assert "Exec=/opt/venv/bin/python -m agent.launcher" in text and "Path=/srv/kairos" in text and "Name=Kairos" in text
    icon = home / ".local/share/icons/hicolor/scalable/apps/kairos.svg"
    assert icon.exists() and f"Icon={icon}" in text and "<svg" in icon.read_text()
    assert entry.stat().st_mode & stat.S_IEXEC


def test_banner_tells_you_everything_needed_to_start():
    b = launcher.banner("http://127.0.0.1:8000", "k3y", "/home/me/kairos-exports", "Groq (x)", reused=False)
    assert "ΚΑΙΡΟΣ" in b and "http://127.0.0.1:8000" in b and "k3y" in b and "kairos-exports" in b and "Ctrl+C" in b
    assert "already running" in launcher.banner("http://x", None, "/e", "llm", reused=True)
    assert "Access key" not in launcher.banner("http://x", None, "/e", "llm", reused=False)


def test_the_real_server_starts_and_a_second_launch_reuses_it(tmp_path, monkeypatch):
    """End to end: `kairos --no-open` brings the app up; running it again opens the existing one instead of failing."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "exports"))
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("rc", launcher.main(["--port", str(port), "--no-open"])), daemon=True)
    t.start()
    url = f"http://127.0.0.1:{port}"
    assert launcher.wait_until_ready(url, timeout=60)
    assert launcher.health(url)["auth_required"] is False
    assert launcher.main(["--port", str(port), "--no-open"]) == 0               # second launch: reuses the instance
    assert urllib.request.urlopen(url + "/health").status == 200
    os.environ.pop("AUTH_TOKEN", None)
