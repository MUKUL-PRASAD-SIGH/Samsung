"""Per-user storage for the LLM provider API keys (the desktop app's replacement for editing `.env`).

Keys live in `<config dir>/keys.json` (Windows: %APPDATA%\\Kairos, macOS: ~/Library/Application Support/Kairos, Linux:
$XDG_CONFIG_HOME/kairos), readable only by the current user where the OS supports it. They are never logged and never sent back
to a client: `status()` only returns whether a key exists and a masked hint.

`load_into_environment()` is called at startup so every module that reads GROQ_API_KEY / OPENROUTER_API_KEY keeps working
unchanged; `save()` updates both the file and `os.environ`, and the server then rebuilds the LLM and vision backends so a key
entered in the UI takes effect without a restart.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Optional

PROVIDERS: Dict[str, str] = {"groq": "GROQ_API_KEY", "openrouter": "OPENROUTER_API_KEY"}
_CHECK_URLS = {
    "groq": "https://api.groq.com/openai/v1/models",
    "openrouter": "https://openrouter.ai/api/v1/auth/key",
}
MAX_KEY_LEN = 512


def config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.getenv("APPDATA") or (Path.home() / "AppData" / "Roaming"))
        name = "Kairos"
    elif sys.platform == "darwin":
        base, name = Path.home() / "Library" / "Application Support", "Kairos"
    else:
        base, name = Path(os.getenv("XDG_CONFIG_HOME") or (Path.home() / ".config")), "kairos"
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def keys_path() -> Path:
    return config_dir() / "keys.json"


def _read() -> Dict[str, str]:
    try:
        data = json.loads(keys_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {p: v for p, v in data.items() if p in PROVIDERS and isinstance(v, str) and v.strip()} if isinstance(data, dict) else {}


def _write(keys: Dict[str, str]) -> None:
    path = keys_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(keys), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:  # Windows: the per-user %APPDATA% ACL already restricts it
        pass
    os.replace(tmp, path)


def load_into_environment() -> None:
    """Make stored keys visible as environment variables. A variable already set (shell, .env) wins, so developers keep control."""
    for provider, value in _read().items():
        os.environ.setdefault(PROVIDERS[provider], value)


def clean(value: Optional[str]) -> str:
    """Pasted keys often carry whitespace, quotes or a `Bearer ` prefix."""
    v = (value or "").strip().strip("\"'").strip()
    if v.lower().startswith("bearer "):
        v = v[7:].strip()
    return v


def validate_format(provider: str, key: str) -> Optional[str]:
    """A cheap offline sanity check; returns an error message or None."""
    if not key:
        return None
    if len(key) > MAX_KEY_LEN or any(c.isspace() for c in key) or not key.isascii():
        return "That does not look like an API key (it contains spaces or unusual characters)."
    if provider == "groq" and not key.startswith("gsk_"):
        return "Groq keys start with “gsk_”."
    if provider == "openrouter" and not key.startswith("sk-or-"):
        return "OpenRouter keys start with “sk-or-”."
    return None


def check_online(provider: str, key: str, timeout: float = 8.0) -> Optional[bool]:
    """Ask the provider whether the key is accepted. True / False, or None when it could not be checked (offline, provider
    down): the caller then saves the key anyway rather than blocking the user."""
    req = urllib.request.Request(_CHECK_URLS[provider], headers={"Authorization": f"Bearer {key}", "User-Agent": "kairos"})
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except urllib.error.HTTPError as e:
        return False if e.code in (401, 403) else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def save(updates: Dict[str, Optional[str]]) -> None:
    """Apply `updates` ({provider: key}; an empty string removes that provider's key; a missing provider is left alone)."""
    keys = _read()
    for provider, value in updates.items():
        if provider not in PROVIDERS or value is None:
            continue
        env = PROVIDERS[provider]
        if value:
            keys[provider] = value
            os.environ[env] = value
        else:
            keys.pop(provider, None)
            os.environ.pop(env, None)
    _write(keys)


def _hint(key: str) -> str:
    return f"{key[:4]}…{key[-4:]}" if len(key) >= 12 else "set"


def status() -> Dict[str, object]:
    """What the UI may know: which providers have a key (masked), and which backend the agent will use."""
    out: Dict[str, object] = {}
    for provider, env in PROVIDERS.items():
        key = os.getenv(env) or ""
        out[provider] = {"configured": bool(key), "hint": _hint(key) if key else ""}
    out["backend"] = "groq" if os.getenv("GROQ_API_KEY") else "openrouter" if os.getenv("OPENROUTER_API_KEY") else (
        "local" if os.getenv("USE_LOCAL_LLM") else "mock")
    out["configured"] = out["backend"] != "mock"
    return out
