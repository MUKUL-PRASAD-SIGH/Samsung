"""One-command setup for Kairos (Linux, macOS, Windows).

    python scripts/setup.py              # venv + dependencies + UI build + models + .env + self-check
    python scripts/setup.py --quick      # no model downloads (voice/TTS/semantic classifier download on first use)
    python scripts/setup.py --check      # only report what is installed / missing; change nothing
    python scripts/setup.py --no-frontend --no-models --no-verify     # pick steps individually

Everything lands in `.venv/` inside the repository; nothing is installed system-wide. It needs Python 3.10-3.12 (use
`setup.sh` / `setup.bat`, which find a suitable interpreter for you) and, for the web UI build, Node.js 18+.
Without an LLM key Kairos still runs end to end in its offline mock mode, so a judge can try it before adding a key.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
IS_WIN = os.name == "nt"
SUPPORTED = ((3, 10), (3, 12))


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")


def say(msg: str = "") -> None:
    print(msg, flush=True)


def step(n: int, total: int, title: str) -> None:
    say(f"\n[{n}/{total}] {title}")


def run(cmd: List[str], cwd: Optional[Path] = None, env: Optional[dict] = None) -> int:
    say("    $ " + " ".join(str(c) for c in cmd))
    return subprocess.call([str(c) for c in cmd], cwd=str(cwd or ROOT), env=env)


def python_ok() -> bool:
    return SUPPORTED[0] <= sys.version_info[:2] <= SUPPORTED[1]


def ensure_env_file() -> str:
    env, example = ROOT / ".env", ROOT / ".env.example"
    if env.exists():
        return ".env already exists: left untouched"
    if example.exists():
        shutil.copyfile(example, env)
        return ".env created from .env.example (no LLM key yet: Kairos will run in offline mock mode)"
    return ".env.example is missing; skipped"


def node_version() -> Optional[int]:
    exe = shutil.which("node")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10).stdout.strip().lstrip("v")
        return int(out.split(".")[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def llm_mode() -> str:
    keys = {}
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                keys[k.strip()] = v.strip().strip("\"'")
    for k in ("GROQ_API_KEY", "OPENROUTER_API_KEY"):
        if os.getenv(k) or keys.get(k):
            return f"live LLM ({k.split('_')[0].title()})"
    if os.getenv("USE_LOCAL_LLM") or keys.get("USE_LOCAL_LLM"):
        return "local LLM server"
    return "offline mock (no API key set)"


def check() -> int:
    say("Kairos environment check")
    py = venv_python()
    rows = [
        ("Python 3.10-3.12 (this interpreter)", python_ok(), sys.version.split()[0]),
        ("virtualenv .venv", py.exists(), str(VENV)),
        ("frontend/dist (web UI build)", (ROOT / "frontend" / "dist" / "index.html").exists(), "run: cd frontend && npm ci && npm run build"),
        ("Node.js >= 18 (only to build the UI)", (node_version() or 0) >= 18, str(node_version() or "not found")),
        (".env", (ROOT / ".env").exists(), "created by setup"),
        ("Piper voice (spoken replies)", (ROOT / "models" / "piper" / "en_US-lessac-medium.onnx").exists(), "python scripts/fetch_models.py"),
    ]
    for name, ok, detail in rows:
        say(f"  [{'ok' if ok else '--'}] {name}: {detail}")
    if py.exists():
        code = subprocess.call([str(py), "-c", "import agent.server, fastapi, faster_whisper"], cwd=str(ROOT), stderr=subprocess.DEVNULL)
        say(f"  [{'ok' if code == 0 else '--'}] Python dependencies import")
    say(f"  LLM mode: {llm_mode()}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true", help="skip model downloads (they happen on first use instead)")
    ap.add_argument("--no-models", action="store_true", help="skip downloading Whisper / MiniLM / Piper models")
    ap.add_argument("--no-frontend", action="store_true", help="skip building the web UI")
    ap.add_argument("--no-verify", action="store_true", help="skip the final self-check (offline eval)")
    ap.add_argument("--check", action="store_true", help="only report the state of the installation")
    args = ap.parse_args(argv)
    if args.check:
        return check()

    if not python_ok():
        say(f"Python {sys.version.split()[0]} is not supported (need 3.10, 3.11 or 3.12). Use setup.sh / setup.bat, or run this\n"
            "script with a supported interpreter, e.g.  python3.11 scripts/setup.py")
        return 1

    total = 6
    warnings: List[str] = []

    step(1, total, "Creating the virtual environment (.venv)")
    if venv_python().exists():
        say("    already present")
    else:
        venv.create(VENV, with_pip=True)
    py = venv_python()

    step(2, total, "Installing Python dependencies (a few minutes; PyTorch for the semantic classifier is the large part)")
    run([py, "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
    code = run([py, "-m", "pip", "install", "-e", ".[dev,embeddings,tts]"])
    if code != 0:
        say("    full install failed; retrying without the optional extras (semantic classifier / spoken replies)")
        code = run([py, "-m", "pip", "install", "-e", ".[dev]"])
        warnings.append("optional extras not installed: Kairos falls back to keyword interrupt detection and has no spoken replies")
    if code != 0:
        say("Could not install the dependencies. Check your network connection and the output above.")
        return 1

    step(3, total, "Configuration")
    say("    " + ensure_env_file())

    step(4, total, "Building the web UI")
    if args.no_frontend:
        say("    skipped (--no-frontend)")
    elif (ROOT / "frontend" / "dist" / "index.html").exists():
        say("    frontend/dist already built")
    else:
        nv, npm = node_version(), shutil.which("npm")
        if not nv or not npm or nv < 18:
            warnings.append("Node.js 18+ was not found, so the web UI was not built. Install Node (https://nodejs.org) and run: "
                            "cd frontend && npm ci && npm run build   (or use Docker: docker compose up --build)")
            say("    skipped: Node.js 18+ not found")
        elif run([npm, "ci"], cwd=ROOT / "frontend") != 0 or run([npm, "run", "build"], cwd=ROOT / "frontend") != 0:
            warnings.append("the web UI build failed; see the output above")

    step(5, total, "Downloading models (speech recognition, semantic classifier, voice)")
    if args.quick or args.no_models:
        say("    skipped: they download on first use instead (the first voice request will be slower)")
    elif run([py, "scripts/fetch_models.py"]) != 0:
        warnings.append("some models could not be downloaded (offline?). Kairos still works; they are fetched on first use.")

    step(6, total, "Self-check: the offline evaluation suite (no network, no API key, about 10 s)")
    if args.no_verify:
        say("    skipped (--no-verify)")
    else:
        env = dict(os.environ, INTENT_EMBEDDINGS="0")
        if run([py, "-m", "agent.eval", "--llm", "mock", "--virtual", "--set", "all", "--fail-under", "97", "--min-scenario", "95"], env=env) != 0:
            warnings.append("the self-check did not pass; see the output above")

    run_cmd = r".venv\Scripts\kairos" if IS_WIN else ".venv/bin/kairos"
    say("\n" + "=" * 70)
    say("Setup finished." if not warnings else "Setup finished with notes:")
    for w in warnings:
        say(f"  ! {w}")
    say(f"\n  LLM mode right now: {llm_mode()}")
    say("  To use the real model, put GROQ_API_KEY=... (or OPENROUTER_API_KEY=...) in .env")
    say(f"\n  Start it:   {run_cmd}")
    say(f"  Run tests:  {py} -m pytest -q")
    say("  Docs:       docs/ (start with 00_Judges_Guide.docx)")
    say("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
