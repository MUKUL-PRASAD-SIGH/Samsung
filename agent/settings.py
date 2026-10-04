"""Deployment settings (Phase G): the security, limits and observability knobs in one documented place.

Model/runtime knobs that predate this module (LLM_*, WHISPER_*, VOICE_*, VISION_*, TTS_*, INTENT_EMBEDDINGS...) are still
read where they are used and are documented in `.env.example`; this object covers what an operator needs to expose the
server safely. `Settings.describe()` is logged at startup with secrets masked, so the effective configuration is visible.

Secure-by-default rules:
  * WebSocket connections from a browser are accepted only from the server's own origin or localhost, unless
    ALLOWED_ORIGINS lists others ("*" disables the check). Clients that send no Origin (scripts, the eval kit) are allowed.
  * If AUTH_TOKEN is set, everything except /health needs it (Authorization: Bearer, or ?token= for WebSockets).
  * Per-IP connection caps, per-connection message/byte rate limits, a global session cap, and size limits.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields
from typing import Dict, List, Optional

_TRUE = {"1", "true", "yes", "on"}


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in _TRUE


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _list(name: str) -> List[str]:
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


@dataclass
class Settings:
    # --- access control
    auth_token: Optional[str] = None              # AUTH_TOKEN: bearer token required for everything except /health
    allowed_origins: List[str] = field(default_factory=list)   # ALLOWED_ORIGINS=a,b or "*"; empty = same-origin + localhost
    trust_proxy: bool = False                     # TRUST_PROXY=1: take the client IP from X-Forwarded-For (behind Caddy/nginx)
    # --- limits
    max_connections_per_ip: int = 20              # MAX_CONNECTIONS_PER_IP
    max_sessions: int = 500                       # MAX_SESSIONS: new sessions beyond this are refused
    msg_rate_per_s: float = 30.0                  # MSG_RATE_PER_S: sustained text/control messages per second per connection
    msg_burst: int = 60                           # MSG_BURST
    audio_bytes_per_s: int = 256_000              # AUDIO_BYTES_PER_S: sustained binary audio per connection (16 kHz PCM16 = 32k)
    audio_burst_bytes: int = 1_000_000            # AUDIO_BURST_BYTES
    max_text_message_bytes: int = 4_000_000       # MAX_TEXT_MESSAGE_BYTES: a base64 camera frame is the largest legitimate one
    max_user_text_chars: int = 8_000              # MAX_USER_TEXT_CHARS
    warmup_min_interval_s: float = 30.0           # POST /warmup spends LLM tokens: at most once per interval
    # --- observability
    log_format: str = "text"                      # LOG_FORMAT=json|text
    log_level: str = "INFO"                       # LOG_LEVEL
    metrics_enabled: bool = True                  # METRICS_ENABLED
    session_id_pattern: str = r"^[A-Za-z0-9_-]{1,64}$"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            auth_token=os.getenv("AUTH_TOKEN") or None,
            allowed_origins=_list("ALLOWED_ORIGINS"),
            trust_proxy=_bool("TRUST_PROXY", False),
            max_connections_per_ip=_int("MAX_CONNECTIONS_PER_IP", 20),
            max_sessions=_int("MAX_SESSIONS", 500),
            msg_rate_per_s=_float("MSG_RATE_PER_S", 30.0),
            msg_burst=_int("MSG_BURST", 60),
            audio_bytes_per_s=_int("AUDIO_BYTES_PER_S", 256_000),
            audio_burst_bytes=_int("AUDIO_BURST_BYTES", 1_000_000),
            max_text_message_bytes=_int("MAX_TEXT_MESSAGE_BYTES", 4_000_000),
            max_user_text_chars=_int("MAX_USER_TEXT_CHARS", 8_000),
            warmup_min_interval_s=_float("WARMUP_MIN_INTERVAL_S", 30.0),
            log_format=os.getenv("LOG_FORMAT", "text").strip().lower(),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
            metrics_enabled=_bool("METRICS_ENABLED", True),
        )

    @property
    def session_id_re(self) -> "re.Pattern[str]":
        return re.compile(self.session_id_pattern)

    def describe(self) -> Dict[str, object]:
        """Effective configuration for the startup log; secrets are masked, never printed."""
        out: Dict[str, object] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            out[f.name] = ("<set>" if value else "<unset>") if f.name == "auth_token" else value
        return out

    def warnings(self) -> List[str]:
        """Things an operator exposing this to a network should hear about at startup."""
        out = []
        if not self.auth_token:
            out.append("AUTH_TOKEN is not set: anyone who can reach this server can use it (and spend your LLM quota).")
        if "*" in self.allowed_origins:
            out.append("ALLOWED_ORIGINS=* disables the WebSocket origin check (cross-site WebSocket hijacking is possible).")
        return out


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def reload_settings() -> Settings:
    """Re-read the environment (tests, and SIGHUP-style reloads)."""
    global _settings
    _settings = Settings.from_env()
    return _settings
