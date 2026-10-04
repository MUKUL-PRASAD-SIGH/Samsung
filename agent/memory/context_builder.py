"""Builds compact, clean LLM prompt context from SessionState + GraphMemory.

Phase 0 note: before this module existed, agent/slow_path/planner.py sent the LLM
only `session.intent`/`session.slots` plus the single current-turn text -- there
was no multi-turn history at all. build_context_block/build_history_messages are
the single call sites planner.py uses; when session.graph_memory hasn't been
initialized yet (ensure_memory() not called), both functions degrade to exactly
that original stateless behavior so nothing regresses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List

if TYPE_CHECKING:
    from agent.coordination.state_machine import SessionState


def build_interrupted_work_note(session: "SessionState") -> str:
    """Tell the LLM what the user's newest message may be correcting.

    A request that was interrupted before finishing is never committed to history (it produced no
    completed turn) and its slot changes are rolled back, so without this the model sees "Actually make
    it Goa" with nothing to correct and asks the user to repeat themselves. The scratchpad still holds
    the uncommitted utterances and the cancelled calls' arguments; surface them.
    """
    scratchpad = getattr(session, "scratchpad", None)
    if scratchpad is None:
        return ""
    earlier = [c.strip() for c in scratchpad.raw_chunks[:-1] if c.strip()]  # last chunk = the newest message
    cancelled = [c for c in scratchpad.aborted_calls if c.arguments]
    if not earlier and not cancelled:
        return ""

    parts = []
    if earlier:
        parts.append("Earlier requests in this exchange that did not finish: " + "; ".join(f'"{c}"' for c in earlier[-3:]) + ".")
    if cancelled:
        described = "; ".join(
            f"{c.tool_name}({', '.join(f'{k}={v}' for k, v in c.arguments.items())})" for c in cancelled[-3:]
        )
        parts.append(f"Tool calls cancelled by the user's newest message: {described}.")
    parts.append(
        "If the newest message corrects or adjusts that work, apply the correction and re-issue the tool call "
        "with the updated arguments instead of asking the user to repeat or confirm."
    )
    return " ".join(parts)


def build_context_block(session: "SessionState") -> str:
    """Returns the system-prompt fragment describing current intent/slots (and interrupted work)."""
    block = f"Current session intent: {session.intent or 'unknown'}. Current slots: {session.slots}."
    note = build_interrupted_work_note(session)
    return f"{block} {note}" if note else block


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
