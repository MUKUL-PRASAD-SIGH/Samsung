"""Builds compact, clean LLM prompt context from SessionState + GraphMemory.

Phase 0 note: before this module existed, agent/slow_path/planner.py sent the LLM
only `session.intent`/`session.slots` plus the single current-turn text -- there
was no multi-turn history at all. build_context_block/build_history_messages are
the single call sites planner.py uses; when session.graph_memory hasn't been
initialized yet (ensure_memory() not called), both functions degrade to exactly
that original stateless behavior so nothing regresses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from agent.coordination.state_machine import SessionState


def build_context_block(session: "SessionState") -> str:
    """Returns the system-prompt fragment describing current intent/slots."""
    return f"Current session intent: {session.intent or 'unknown'}. Current slots: {session.slots}."


def build_history_messages(session: "SessionState", max_turns: int = 5) -> List[Dict[str, str]]:
    """Returns clean prior-turn chat messages via subgraph retrieval, or [] if no
    graph memory exists yet for this session (matches pre-memory-system behavior)."""
    graph_memory = getattr(session, "graph_memory", None)
    if graph_memory is None:
        return []
    return graph_memory.get_subgraph_prompt_context(current_intent=session.intent, max_turns=max_turns)


def build_resolved_entity_state(session: "SessionState") -> str:
    """Injects active persistent entities as a short line, e.g. 'Active destination: BOM'."""
    if not session.slots:
        return ""
    parts = [f"{key}={value}" for key, value in session.slots.items()]
    return "Resolved entities: " + ", ".join(parts)
