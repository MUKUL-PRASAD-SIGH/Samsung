"""Tier 2: Structural Knowledge & Conversation DAG ("L2 Graph Memory").

Persistent, cross-turn Directed Acyclic Graph connecting TurnNode / EntityNode /
ArtifactNode via typed edges. Slot state stays owned by SessionState -- this
module reads session.slots to snapshot state onto TurnNodes, and writes back to
session state ONLY via session.patch_slots() (never via session.rollback_snapshot(),
whose single-step LIFO semantics are reserved for its existing callers and are
incompatible with "jump to an arbitrary earlier turn").
"""

from __future__ import annotations

import itertools
from agent import clock
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from agent.coordination.state_machine import SessionState
    from agent.memory.scratchpad import CanonicalTurn

EDGE_TYPES = ("NEXT_TURN", "SUPERSEDES", "REFERENCES", "PRODUCED", "BRANCHES_FROM")


@dataclass
class TurnNode:
    id: str
    turn_index: int
    intent: Optional[str]
    user_prompt: str
    agent_response: str
    epoch: int
    slots_snapshot: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=clock.now)
    pruned: bool = False  # set True when superseded by a rollback/branch
    # Spoken replies can be cut off by the user: the words they actually heard (None = the full reply was delivered).
    spoken_text: Optional[str] = None


@dataclass
class EntityNode:
    id: str
    entity_type: str
    key: str
    value: Any
    source_turn_id: str
    timestamp: float = field(default_factory=clock.now)


@dataclass
class ArtifactNode:
    id: str
    title: str
    language: str
    content_hash: str
    source_turn_id: str
    timestamp: float = field(default_factory=clock.now)


@dataclass
class Edge:
    source: str
    target: str
    edge_type: str


class GraphMemoryError(ValueError):
    pass


class GraphMemory:
    def __init__(self, session: "SessionState"):
        self._session = session
        self.turns: Dict[str, TurnNode] = {}
        self.entities: Dict[str, EntityNode] = {}
        self.artifacts: Dict[str, ArtifactNode] = {}
        self.edges: List[Edge] = []
        self._turn_order: List[str] = []  # insertion order of turn ids
        self._id_counter = itertools.count(1)
        self._head_turn_id: Optional[str] = None

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}_{next(self._id_counter)}_{uuid.uuid4().hex[:6]}"

    # ------------------------------------------------------------------ insert

    def insert_turn(self, canonical_turn: "CanonicalTurn") -> TurnNode:
        node = TurnNode(
            id=canonical_turn.turn_id,
            turn_index=len(self._turn_order),
            intent=self._session.intent,
            user_prompt=canonical_turn.cleaned_user_intent,
            agent_response=canonical_turn.agent_response,
            epoch=canonical_turn.epoch,
            slots_snapshot=dict(canonical_turn.final_slots),
        )
        self.turns[node.id] = node
        if self._head_turn_id is not None:
            self.edges.append(Edge(source=self._head_turn_id, target=node.id, edge_type="NEXT_TURN"))
        # SUPERSEDES marks a real correction: this turn replaced a slot value an earlier turn had
        # established (e.g. destination Mumbai -> Goa). Point at each turn that held the old value.
        superseded = set()
        for key, old_value in canonical_turn.overrides.items():
            # Prefer the turn whose entity introduced the old value (later turns merely inherit
            # it in their slot snapshots); fall back to the latest turn that held it.
            source_id = next(
                (
                    e.source_turn_id
                    for e in reversed(list(self.entities.values()))
                    if e.key == key and e.value == old_value
                    and e.source_turn_id in self.turns and not self.turns[e.source_turn_id].pruned
                ),
                None,
            )
            if source_id is None:
                source_id = next(
                    (
                        pid for pid in reversed(self._turn_order)
                        if not self.turns[pid].pruned and self.turns[pid].slots_snapshot.get(key) == old_value
                    ),
                    None,
                )
            if source_id is not None:
                superseded.add(source_id)
        for prior_id in sorted(superseded):
            self.edges.append(Edge(source=node.id, target=prior_id, edge_type="SUPERSEDES"))
        self._turn_order.append(node.id)
        self._head_turn_id = node.id

        for cand in canonical_turn.entity_candidates:
            entity_node = self.link_entity(node.id, cand.entity_type, cand.key, cand.value)
            self.edges.append(Edge(source=node.id, target=entity_node.id, edge_type="REFERENCES"))

        for artifact in canonical_turn.artifacts_produced:
            self.link_artifact(node.id, artifact)

        if self._would_cycle():
            raise GraphMemoryError("insert_turn would introduce a cycle in the conversation DAG")

        return node

    def link_entity(self, turn_id: str, entity_type: str, key: str, value: Any) -> EntityNode:
        node = EntityNode(
            id=self._new_id("ent"),
            entity_type=entity_type,
            key=key,
            value=value,
            source_turn_id=turn_id,
        )
        self.entities[node.id] = node
        return node

    def link_artifact(self, turn_id: str, artifact: Dict[str, Any]) -> ArtifactNode:
        content = artifact.get("content", "")
        node = ArtifactNode(
            id=self._new_id("art"),
            title=artifact.get("title", "artifact"),
            language=artifact.get("language", "text"),
            content_hash=str(hash(content)),
            source_turn_id=turn_id,
        )
        self.artifacts[node.id] = node
        self.edges.append(Edge(source=turn_id, target=node.id, edge_type="PRODUCED"))
        return node

    # ---------------------------------------------------------------- rollback

    def rollback_to_node(self, target_turn_id: str) -> TurnNode:
        """Jump conversation state back to an arbitrary earlier turn.

        Does NOT call session.rollback_snapshot() (that method is a single-step
        LIFO pop over SessionState._history, pinned by
        tests/test_state_machine.py::test_slot_diff_patching_and_rollback, and has
        no notion of "jump to turn N"). Instead applies the target turn's stored
        slots_snapshot via the same session.patch_slots() primitive used everywhere
        else, then marks all turns after the target as pruned and records a
        BRANCHES_FROM edge from the new head.
        """
        target = self.turns.get(target_turn_id)
        if target is None:
            raise GraphMemoryError(f"Unknown turn_id: {target_turn_id}")

        for turn_id in self._turn_order[target.turn_index + 1 :]:
            self.turns[turn_id].pruned = True

        self._session.patch_slots(dict(target.slots_snapshot), new_intent=target.intent)

        branch_id = self._new_id("branch")
        branch_node = TurnNode(
            id=branch_id,
            turn_index=len(self._turn_order),
            intent=target.intent,
            user_prompt=f"[rollback to {target_turn_id}]",
            agent_response="",
            epoch=self._session.epoch,
            slots_snapshot=dict(target.slots_snapshot),
        )
        self.turns[branch_id] = branch_node
        self.edges.append(Edge(source=branch_id, target=target_turn_id, edge_type="BRANCHES_FROM"))
        self._turn_order.append(branch_id)
        self._head_turn_id = branch_id

        return branch_node

    # ------------------------------------------------------------------- read

    def get_active_thread(self) -> List[TurnNode]:
        """Traverse backwards from the head following non-pruned NEXT_TURN/BRANCHES_FROM
        edges, returning turns in chronological order."""
        if self._head_turn_id is None:
            return []

        thread: List[TurnNode] = []
        current_id: Optional[str] = self._head_turn_id
        visited = set()
        while current_id is not None and current_id not in visited:
            visited.add(current_id)
            node = self.turns[current_id]
            if not node.pruned:
                thread.append(node)

            prev_id = None
            for edge in self.edges:
                if edge.target == current_id and edge.edge_type == "NEXT_TURN":
                    prev_id = edge.source
                    break
            if prev_id is None:
                for edge in self.edges:
                    if edge.source == current_id and edge.edge_type == "BRANCHES_FROM":
                        prev_id = edge.target
                        break
            current_id = prev_id

        thread.reverse()
        return thread

    def mark_response_truncated(self, full_text: str, spoken_text: str) -> bool:
        """Record that the latest turn whose reply was `full_text` was only spoken up to `spoken_text` (the user
        interrupted). Prompt context then shows what the user actually heard, so the model doesn't assume they
        received information they were cut off before hearing."""
        for node in reversed(list(self.turns.values())):
            if node.agent_response == full_text and node.spoken_text is None:
                node.spoken_text = spoken_text
                return True
        return False

    def get_subgraph_prompt_context(self, max_turns: int = 5) -> List[Dict[str, str]]:
        """Build clean chat-style messages from the last `max_turns` active turns."""
        thread = self.get_active_thread()[-max_turns:]
        messages: List[Dict[str, str]] = []
        for node in thread:
            if node.user_prompt:
                messages.append({"role": "user", "content": node.user_prompt})
            if node.agent_response:
                reply = node.agent_response
                if node.spoken_text is not None:
                    heard = node.spoken_text.strip()
                    reply = f"{heard} [interrupted by the user; the rest was never heard]" if heard else \
                        "[interrupted by the user before this was heard]"
                messages.append({"role": "assistant", "content": reply})
        return messages

    def get_entities_for_turn(self, turn_id: str) -> List[EntityNode]:
        return [e for e in self.entities.values() if e.source_turn_id == turn_id]

    # -------------------------------------------------------------- invariant

    def _would_cycle(self) -> bool:
        """DAG acyclicity check over the structural/temporal edges (NEXT_TURN, BRANCHES_FROM).

        SUPERSEDES/REFERENCES are semantic annotation edges that intentionally point
        backward in time (e.g. new_turn --SUPERSEDES--> old_turn) alongside a forward
        NEXT_TURN edge between the same pair -- including them here would flag every
        ordinary turn-with-override as a false-positive 2-cycle. Only the edges that
        define turn ordering are checked for real cycles.
        """
        adjacency: Dict[str, List[str]] = {}
        for edge in self.edges:
            if edge.edge_type in ("NEXT_TURN", "BRANCHES_FROM"):
                adjacency.setdefault(edge.source, []).append(edge.target)

        visiting: set = set()
        visited: set = set()

        def _dfs(node_id: str) -> bool:
            if node_id in visiting:
                return True
            if node_id in visited:
                return False
            visiting.add(node_id)
            for neighbor in adjacency.get(node_id, []):
                if _dfs(neighbor):
                    return True
            visiting.discard(node_id)
            visited.add(node_id)
            return False

        return any(_dfs(node_id) for node_id in list(self.turns.keys()) if node_id not in visited)

    def to_graph_payload(self) -> Dict[str, List[Dict[str, Any]]]:
        """Serialize the full graph into node/edge payloads for the GraphUpdateAction."""
        nodes: List[Dict[str, Any]] = []
        for t in self.turns.values():
            nodes.append({
                "id": t.id,
                "node_type": "turn",
                "label": t.user_prompt[:40] or "(turn)",
                "data": {
                    "agent_response": t.agent_response,
                    "epoch": t.epoch,
                    "pruned": t.pruned,
                    "slots_snapshot": t.slots_snapshot,
                },
            })
        for e in self.entities.values():
            nodes.append({
                "id": e.id,
                "node_type": "entity",
                "label": f"{e.key}={e.value}",
                "data": {"entity_type": e.entity_type, "source_turn_id": e.source_turn_id},
            })
        for a in self.artifacts.values():
            nodes.append({
                "id": a.id,
                "node_type": "artifact",
                "label": a.title,
                "data": {"language": a.language, "source_turn_id": a.source_turn_id},
            })
        edges = [{"source": edge.source, "target": edge.target, "edge_type": edge.edge_type} for edge in self.edges]
        return {"nodes": nodes, "edges": edges}
