"""Guard: API keys must never appear in tracked files (keys live in the gitignored .env)."""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PATTERN = re.compile(r"gsk_[A-Za-z0-9]{20,}|sk-or-v1-[A-Za-z0-9]{20,}|hf_[A-Za-z0-9]{30,}|sk-ant-[A-Za-z0-9_-]{20,}")
SKIP_PREFIXES = ("frontend/node_modules/", "frontend/dist/")
SKIP_SUFFIXES = (".wav", ".webm", ".pcm", ".png", ".jpg", ".ico")


def _tracked_files():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("not a git checkout")
    return [f for f in out.splitlines() if not f.startswith(SKIP_PREFIXES) and not f.endswith(SKIP_SUFFIXES)]


def test_no_api_keys_in_tracked_files():
    leaks = []
    for name in _tracked_files():
        path = ROOT / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        if PATTERN.search(text):
            leaks.append(name)
    assert not leaks, f"API key pattern found in tracked files: {leaks}"


def test_env_file_is_gitignored():
    result = subprocess.run(["git", "check-ignore", ".env"], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, ".env must be gitignored"
