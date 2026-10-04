"""`kairos`: start the server and open the app, in one step.

    kairos                    # start on http://127.0.0.1:8000 and open Kairos in its own window
    kairos --login            # require an access key (shows the sign-in screen)
    kairos --host 0.0.0.0     # serve your LAN (an access key is generated and required)
    kairos --install-shortcut # add a Kairos icon to your application launcher (Linux)

Defaults are chosen so the first run needs no configuration: loopback-only and no key (it is your machine), a key
generated and required the moment you expose it to a network, the UI opened in a standalone app window if Chrome/Chromium/
Edge is installed (else your default browser), and an already-running instance is reused instead of failing on the port.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import List, Optional

FROZEN = bool(getattr(sys, "frozen", False))      # running as the packaged desktop app (PyInstaller)
ROOT = Path(getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parents[1])
ICON_SOURCE = ROOT / "frontend" / "public" / "kairos.svg"
BROWSERS = ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser", "microsoft-edge", "brave-browser")
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def config_dir() -> Path:
    """Per-user settings folder (%APPDATA%\\Kairos on Windows, ~/.config/kairos on Linux): the access key and API keys live here."""
    from agent.keystore import config_dir as _config_dir

    return _config_dir()


def load_or_create_key() -> str:
    """The access key, created once and kept (0600) so restarts and the desktop shortcut keep working."""
    path = config_dir() / "key"
    if path.exists() and path.read_text().strip():
        return path.read_text().strip()
    key = secrets.token_urlsafe(18)
    path.write_text(key + "\n")
    path.chmod(0o600)
    return key


def decide_auth(host: str, force_login: bool, no_auth: bool, env_token: Optional[str]) -> Optional[str]:
    """Which access key to require (None = open). An explicit AUTH_TOKEN always wins; exposing the server beyond loopback
    always requires a key; --login asks for one even locally (so the sign-in screen is used)."""
    if env_token:
        return env_token
    if no_auth and host in LOOPBACK:
        return None
    if force_login or host not in LOOPBACK:
        return load_or_create_key()
    return None


def _platform_browsers() -> List[str]:
    """Where browsers live when they are not on PATH: Windows and macOS install them in fixed places. Edge ships with Windows 10/11,
    so a Windows machine always has a browser that can host the app window."""
    found: List[str] = []
    if sys.platform == "win32":
        roots = [os.getenv(v) for v in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
        for root in filter(None, roots):
            for rel in (("Microsoft", "Edge", "Application", "msedge.exe"), ("Google", "Chrome", "Application", "chrome.exe"),
                        ("BraveSoftware", "Brave-Browser", "Application", "brave.exe"), ("Chromium", "Application", "chrome.exe")):
                found.append(str(Path(root, *rel)))
    elif sys.platform == "darwin":
        for app in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium"):
            found.append(f"/Applications/{app}.app/Contents/MacOS/{app}")
    return [p for p in found if Path(p).exists()]


def find_browser() -> Optional[str]:
    for name in BROWSERS:
        exe = shutil.which(name)
        if exe:
            return exe
    return next(iter(_platform_browsers()), None)


def health(url: str, timeout: float = 1.5) -> Optional[dict]:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def wait_until_ready(url: str, timeout: float = 90.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if health(url):
            return True
        time.sleep(0.25)
    return False


def open_app(url: str, prefer_window: bool = True) -> str:
    """Open `url` in a standalone app window (no tabs/address bar) when a Chromium-family browser exists, else the default
    browser. Returns what was used."""
    exe = find_browser() if prefer_window else None
    if exe:
        try:
            profile = config_dir() / "browser-profile"
            detach = {"creationflags": 0x00000008 | 0x00000200} if sys.platform == "win32" else {"start_new_session": True}
            subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1280,860",
                              "--use-fake-ui-for-media-stream" if os.getenv("KAIROS_FAKE_MEDIA") else "--no-first-run"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **detach)
            return Path(exe).name
        except OSError:
            pass
    webbrowser.open(url)
    return "default browser"


def port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def desktop_entry(python: str, cwd: Path, icon: str) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Kairos\n"
        "GenericName=Real-time agent\n"
        "Comment=Καιρός: a real-time agent you can interrupt\n"
        f"Exec={python} -m agent.launcher\n"
        f"Path={cwd}\n"
        f"Icon={icon}\n"
        "Terminal=false\n"
        "Categories=Utility;Office;\n"
        "StartupNotify=true\n"
        "StartupWMClass=Kairos\n"
    )


def install_shortcut(python: Optional[str] = None, cwd: Optional[Path] = None, home: Optional[Path] = None) -> Path:
    """Add Kairos to the Linux application launcher (a .desktop entry + icon). Returns the entry's path."""
    if not sys.platform.startswith("linux") and home is None:
        raise SystemExit("--install-shortcut currently supports Linux only. On other systems run `kairos` from a terminal.")
    home = home or Path.home()
    icon_dir = home / ".local/share/icons/hicolor/scalable/apps"
    apps_dir = home / ".local/share/applications"
    icon_dir.mkdir(parents=True, exist_ok=True)
    apps_dir.mkdir(parents=True, exist_ok=True)
    icon = icon_dir / "kairos.svg"
    shutil.copyfile(ICON_SOURCE, icon)
    entry = apps_dir / "kairos.desktop"
    entry.write_text(desktop_entry(python or sys.executable, cwd or ROOT, str(icon)))
    entry.chmod(0o755)
    return entry


def banner(url: str, key: Optional[str], export_dir: str, llm: str, reused: bool) -> str:
    lines = ["", "  ╭─────────────────────────────────────────────╮",
             "  │   KAIROS · ΚΑΙΡΟΣ   the right moment to act │",
             "  ╰─────────────────────────────────────────────╯", ""]
    lines.append(f"  {'Using the instance already running at' if reused else 'Running at'}  {url}")
    if key:
        lines.append(f"  Access key: {key}   (saved in {config_dir() / 'key'})")
    lines.append(f"  Brain: {llm}")
    lines.append(f"  Code you export is saved to: {export_dir}")
    lines.append("  Press Ctrl+C to stop." if not reused else "")
    return "\n".join(lines) + "\n"


def llm_description() -> str:
    if os.getenv("GROQ_API_KEY"):
        return f"Groq ({os.getenv('LLM_MODEL_NAME', 'openai/gpt-oss-120b')})"
    if os.getenv("OPENROUTER_API_KEY"):
        return "OpenRouter"
    if os.getenv("USE_LOCAL_LLM"):
        return "your local model server"
    return "no API key yet: the app asks for your Groq / OpenRouter key on first run (or use its offline demo mode)"


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="kairos", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-open", action="store_true", help="start the server without opening a window")
    p.add_argument("--browser-tab", action="store_true", help="open in a normal browser tab instead of an app window")
    p.add_argument("--login", action="store_true", help="require an access key even on localhost (shows the sign-in screen)")
    p.add_argument("--no-auth", action="store_true", help="never require a key (only honoured on loopback)")
    p.add_argument("--install-shortcut", action="store_true", help="add a Kairos icon to your application launcher (Linux)")
    return p.parse_args(argv)


def _utf8_console() -> None:
    """The banner has box-drawing and Greek characters; a Windows console on a legacy code page would raise UnicodeEncodeError."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: Optional[List[str]] = None) -> int:
    _utf8_console()
    args = parse_args(argv)
    if args.install_shortcut:
        entry = install_shortcut()
        print(f"Installed: {entry}\nKairos now appears in your application launcher.")
        return 0

    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
        load_dotenv(Path.cwd() / ".env")
    except ImportError:
        pass

    try:
        from agent import keystore

        keystore.load_into_environment()       # keys saved from the app's settings screen on an earlier run
    except OSError:
        pass

    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}"
    if not (ROOT / "frontend" / "dist").exists():
        print("Note: the web UI is not built yet. Run:  cd frontend && npm ci && npm run build   (the API still works)", file=sys.stderr)

    # Already running here? Open it instead of failing on the port.
    existing = health(url)
    if existing is not None:
        print(banner(url, None, os.getenv("EXPORT_DIR") or str(Path.home() / "kairos-exports"), llm_description(), reused=True))
        if not args.no_open:
            open_app(url, prefer_window=not args.browser_tab)
        return 0
    if not port_free(args.host, args.port):
        print(f"Port {args.port} is in use by something else. Try:  kairos --port {args.port + 1}", file=sys.stderr)
        return 2

    key = decide_auth(args.host, args.login, args.no_auth, os.getenv("AUTH_TOKEN"))
    if key:
        os.environ["AUTH_TOKEN"] = key
    os.environ.setdefault("EXPORT_DIR", str(Path.home() / "kairos-exports"))
    print(banner(url, key, os.environ["EXPORT_DIR"], llm_description(), reused=False))

    import uvicorn

    server = uvicorn.Server(uvicorn.Config("agent.server:app", host=args.host, port=args.port, log_level="warning"))

    def opener() -> None:
        if wait_until_ready(url) and not args.no_open:
            used = open_app(url, prefer_window=not args.browser_tab)
            print(f"  Opened in {used}.")

    threading.Thread(target=opener, daemon=True).start()
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
