"""Request-level protection for the HTTP and WebSocket surface (Phase G). Pure functions + small stateful helpers so
every rule can be unit-tested without a server."""

from __future__ import annotations

import hmac
from typing import Dict, Mapping, Optional
from urllib.parse import urlparse

from agent import clock
from agent.settings import Settings

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def valid_session_id(session_id: str, settings: Settings) -> bool:
    return bool(settings.session_id_re.match(session_id or ""))


def origin_allowed(origin: Optional[str], host: Optional[str], settings: Settings) -> bool:
    """Browsers always send Origin on a WebSocket handshake; scripts usually don't (allowed: they are not a
    cross-site attack vector, and AUTH_TOKEN is the control for them)."""
    if not origin:
        return True
    if "*" in settings.allowed_origins:
        return True
    if origin in settings.allowed_origins:
        return True
    if settings.allowed_origins:
        return False        # an explicit allow-list is exhaustive
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    origin_host = (parsed.hostname or "").lower()
    if origin_host in LOCAL_HOSTS:
        return True
    request_host = (host or "").split(":")[0].lower()
    return bool(origin_host) and origin_host == request_host     # same-origin deployment (UI served by this server)


def bearer_token(headers: Mapping[str, str], query: Mapping[str, str]) -> Optional[str]:
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return query.get("token") or None


def token_ok(provided: Optional[str], settings: Settings) -> bool:
    if not settings.auth_token:
        return True
    return provided is not None and hmac.compare_digest(provided.encode(), settings.auth_token.encode())


def client_ip(peer: Optional[str], headers: Mapping[str, str], settings: Settings) -> str:
    """The peer address, or -- only when TRUST_PROXY=1 -- the left-most X-Forwarded-For entry. Trusting that header
    when the server is directly exposed would let any client pick its own IP and dodge the per-IP limits."""
    if settings.trust_proxy:
        fwd = headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip() or (peer or "unknown")
    return peer or "unknown"


class TokenBucket:
    """Classic token bucket. `cost` units are taken per call; refuses (returns False) when the bucket is empty."""

    def __init__(self, rate: float, burst: float):
        self.rate, self.burst = float(rate), float(burst)
        self._tokens = float(burst)
        self._at = clock.monotonic()

    def allow(self, cost: float = 1.0) -> bool:
        now = clock.monotonic()
        self._tokens = min(self.burst, self._tokens + (now - self._at) * self.rate)
        self._at = now
        if self._tokens >= cost:
            self._tokens -= cost
            return True
        return False


class ConnectionLimiter:
    """Open WebSocket connections per client IP."""

    def __init__(self, limit: int):
        self.limit = limit
        self._open: Dict[str, int] = {}

    def acquire(self, ip: str) -> bool:
        if self._open.get(ip, 0) >= self.limit:
            return False
        self._open[ip] = self._open.get(ip, 0) + 1
        return True

    def release(self, ip: str) -> None:
        n = self._open.get(ip, 0) - 1
        if n <= 0:
            self._open.pop(ip, None)
        else:
            self._open[ip] = n

    def count(self, ip: str) -> int:
        return self._open.get(ip, 0)
