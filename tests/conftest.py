"""Test isolation: the default suite must never reach a real LLM provider.

`agent.llm_client` loads `.env` at import, so a developer's real GROQ_API_KEY / OPENROUTER_API_KEY would otherwise turn every
`AgentCoordinator()` built with defaults into a live Groq client: slow, flaky once the daily token cap or rate limit is hit, and
it spends tokens. Keys are blanked for every test except those marked `live`, which exist precisely to use them.
"""

import pytest

_PROVIDER_KEYS = ("GROQ_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "LLM_FALLBACK_API_KEY")


@pytest.fixture(autouse=True)
def _no_real_llm(request, monkeypatch):
    if request.node.get_closest_marker("live"):
        return
    for key in _PROVIDER_KEYS:
        monkeypatch.setenv(key, "")
