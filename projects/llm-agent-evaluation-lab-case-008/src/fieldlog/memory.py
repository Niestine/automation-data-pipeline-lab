"""Four session-scoped stores: episode log, bi-temporal edges, working context, FIFO queue.

Edges are closed in place. Rows are not deleted when a later fact contradicts
them. Session deletion is the privacy operation, separate from invalidation.
"""

from __future__ import annotations

from fieldlog.models import Edge, Episode, make_edge_id
from fieldlog.schema_guard import validate
from fieldlog.support import (
    Clock,
    FlushIntegrityError,
    Journal,
    PolicyError,
    SchemaRejected,
    count_tokens,
    parse_instant,
)

ALLOWLIST_TOOLS = {
    "working_context_edit",
    "episode_search",
    "archival_insert",
    "archival_search",
    "session_delete",
}

INSERT_FIELDS = {
    "subject",
    "predicate",
    "object",
    "serial",
    "t_valid",
    "source_label",
    "context",
    "close",
    "span",
}


class WorkingContext:
    """Pinned facts. The model reaches this only through named edits."""

    def __init__(self, journal: Journal) -> None:
        self.persona: list[str] = []
        self.facts: list[str] = []
        self.open_conflicts: list[str] = []
        self.journal = journal
        self.pin_cap = 8

    def edit(self, operation: str, payload: str) -> None:
        if operation not in {"set_persona", "pin_fact", "add_conflict"}:
            raise PermissionError(f"unknown working-context operation: {operation}")
        self.journal.tool("working_context_edit", {"operation": operation, "payload": payload})
        if operation == "set_persona":
            if payload not in self.persona:
                self.persona.append(payload)
        elif operation == "pin_fact":
            if payload not in self.facts and len(self.facts) < self.pin_cap:
                self.facts.append(payload)
        elif operation == "add_conflict":
            if payload not in self.open_conflicts:
                self.open_conflicts.append(payload)

    def token_count(self) -> int:
        return count_tokens(" ".join(self.persona + self.facts + self.open_conflicts))

    def visible_pins(self, session_id: str) -> list[str]:
        marker = session_id + "|"
        return [fact for fact in self.facts if fact.startswith(marker)]

    def drop_session_pins(self, marker: str) -> None:
        self.facts = [fact for fact in self.facts if not fact.startswith(marker)]
        self.open_conflicts = [item for item in self.open_conflicts if marker not in item]


class MemoryStore:
    """Append-only episodes and non-lossy edges. There is no execute_query."""

    def __init__(self, clock: Clock, journal: Journal, schemas: dict[str, dict]) -> None:
        self.clock = clock
        self.journal = journal
        self.schemas = schemas
        self.episodes: list[Episode] = []
        self.edges: list[Edge] = []
        self._episode_index: dict[str, Episode] = {}
        self._turns: dict[str, int] = {}

    def next_episode_id(self, session_id: str) -> tuple[str, int]:
        turn = self._turns.get(session_id, 0) + 1
        self._turns[session_id] = turn
        return f"ep-{session_id}-{turn:04d}", turn

    def add_episode(self, episode: Episode) -> None:
        result = validate(episode.public_dict(), self.schemas["episode"])
        if not result["valid"]:
            raise SchemaRejected(result["errors"][0]["error"])
        if episode.episode_id in self._episode_index:
            raise PolicyError(f"episode already stored: {episode.episode_id}")
        self.episodes.append(episode)
        self._episode_index[episode.episode_id] = episode
        self.journal.add(
            event="ingest_episode",
            episode_id=episode.episode_id,
            session_id=episode.session_id,
            tokens=episode.token_count,
        )

    def peek_episode(self, episode_id: str) -> Episode | None:
        return self._episode_index.get(episode_id)

    def get_episode(self, episode_id: str) -> Episode | None:
        self.journal.tool("episode_search", {"episode_id": episode_id})
        return self.peek_episode(episode_id)

    def session_episodes(self, session_id: str) -> list[Episode]:
        return [episode for episode in self.episodes if episode.session_id == session_id]

    def recent_texts(self, session_id: str, limit: int = 4) -> list[str]:
        texts = [episode.text for episode in self.session_episodes(session_id)]
        return texts[-limit:]

    def known_subjects(self, session_id: str) -> list[str]:
        seen: list[str] = []
        for edge in self.edges:
            if edge.session_id == session_id and edge.subject not in seen:
                seen.append(edge.subject)
        return seen

    def check_fact(self, text: str, fact: dict) -> dict:
        """Closed-argument check for one extracted fact. Raises before anything is written."""

        if "query" in fact or "cypher" in fact:
            raise PolicyError("model-authored query rejected")
        unknown = set(fact) - INSERT_FIELDS
        if unknown:
            raise PolicyError(f"closed inserter rejected fields: {sorted(unknown)}")
        for name in ("subject", "predicate", "object", "span"):
            if not isinstance(fact.get(name), str) or not fact[name]:
                raise PolicyError(f"extracted fact is missing {name}")
        if fact["span"] not in text:
            raise PolicyError("edge span is not contained in the source episode")
        if fact.get("close", "supersede") not in {"supersede", "hold"}:
            raise PolicyError(f"unknown close mode: {fact.get('close')}")
        checked = dict(fact)
        try:
            if checked.get("t_valid") is not None:
                checked["t_valid"] = parse_instant(checked["t_valid"])
        except ValueError as exc:
            raise PolicyError(str(exc)) from exc
        return checked

    def insert_edge(self, session_id: str, episode: Episode, fact: dict) -> Edge:
        """Fixed inserter. Closed arguments only; a query string is rejected."""

        fact = self.check_fact(episode.text, fact)
        created = self.clock.now()
        edge = Edge(
            edge_id=make_edge_id(
                session_id,
                episode.episode_id,
                fact["subject"],
                fact["predicate"],
                fact.get("serial"),
                fact["object"],
            ),
            session_id=session_id,
            episode_id=episode.episode_id,
            subject=fact["subject"],
            predicate=fact["predicate"],
            object=fact["object"],
            serial=fact.get("serial"),
            t_valid=fact.get("t_valid"),
            t_invalid=None,
            t_created=created,
            t_expired=None,
            source_label=fact.get("source_label") or "unspecified",
            status="active",
            span=fact["span"],
            context=fact.get("context"),
        )
        if not _provenance_ok(edge):
            raise PolicyError("edge is missing serial and a validity interval")
        existing = self._find_id(edge.edge_id)
        if existing is not None:
            return existing
        self._apply_closure(edge, fact.get("close", "supersede"))
        self._require_edge_instance(edge)
        self.edges.append(edge)
        self.journal.tool(
            "archival_insert",
            {
                "edge_id": edge.edge_id,
                "subject": edge.subject,
                "predicate": edge.predicate,
                "object": edge.object,
                "serial": edge.serial,
            },
        )
        self.journal.add(event="insert_edge", edge_id=edge.edge_id, status=edge.status, session_id=session_id)
        return edge

    def edges_for(self, session_id: str, subject: str | None = None, predicate: str | None = None) -> list[Edge]:
        self.journal.tool(
            "archival_search",
            {"session_id": session_id, "subject": subject, "predicate": predicate},
        )
        found = []
        for edge in self.edges:
            if edge.session_id != session_id:
                continue
            if subject is not None and edge.subject != subject:
                continue
            if predicate is not None and edge.predicate != predicate:
                continue
            found.append(edge)
        return found

    def edges_for_episode(self, episode_id: str) -> list[Edge]:
        return [edge for edge in self.edges if edge.episode_id == episode_id]

    def delete_session(self, session_id: str) -> int:
        """Privacy deletion. Contradiction closure does not use this path."""

        removed = [episode.episode_id for episode in self.episodes if episode.session_id == session_id]
        self.episodes = [episode for episode in self.episodes if episode.session_id != session_id]
        for episode_id in removed:
            self._episode_index.pop(episode_id, None)
        before = len(self.edges)
        self.edges = [edge for edge in self.edges if edge.session_id != session_id]
        self._turns.pop(session_id, None)
        self.journal.tool("session_delete", {"session_id": session_id, "episodes": len(removed)})
        return before - len(self.edges)

    def subject_mentioned(self, session_id: str, subject: str) -> bool:
        needle = subject.casefold()
        for episode in self.session_episodes(session_id):
            if needle in episode.text.casefold():
                return True
        return False

    def _apply_closure(self, edge: Edge, close: str) -> None:
        group = [
            other
            for other in self.edges
            if other.session_id == edge.session_id
            and other.subject == edge.subject
            and other.predicate == edge.predicate
        ]
        if close == "hold":
            edge.status = "unresolved"
            for other in group:
                other.status = "unresolved"
            return
        for other in group:
            if other.object == edge.object:
                continue
            if _strictly_newer(edge, other):
                _close(other, edge.t_valid, self.clock.now())
                self._require_edge_instance(other)
            elif _strictly_newer(other, edge):
                # Late arrival of an older fact: it is stored already closed.
                _close(edge, other.t_valid, self.clock.now())
            elif _tie(edge, other):
                edge.status = "unresolved"
                other.status = "unresolved"

    def _require_edge_instance(self, edge: Edge) -> None:
        result = validate(edge.public_dict(), self.schemas["memory_edge"])
        if not result["valid"]:
            raise SchemaRejected(result["errors"][0]["error"])

    def _find_id(self, edge_id: str) -> Edge | None:
        for edge in self.edges:
            if edge.edge_id == edge_id:
                return edge
        return None


class QueueManager:
    """Bounded FIFO queue. External attribute replacement is rejected."""

    def __init__(
        self,
        budget_tokens: int,
        journal: Journal,
        store: MemoryStore,
        working: WorkingContext,
        warning_fraction: float = 0.70,
        system_tokens: int = 0,
        eviction_mode: str = "structured",
    ) -> None:
        object.__setattr__(self, "_ready", False)
        self.budget = int(budget_tokens)
        self.warning_fraction = float(warning_fraction)
        self.system_tokens = int(system_tokens)
        self.eviction_mode = eviction_mode
        self.journal = journal
        self.store = store
        self.working = working
        self._items: list[dict] = []
        self.summary = ""
        self.summary_pointer: str | None = None
        self.flush_count = 0
        self._pressure_latched = False
        object.__setattr__(self, "_ready", True)

    def __setattr__(self, name: str, value) -> None:
        if getattr(self, "_ready", False) and name in {"items", "_items", "summary", "summary_pointer"}:
            raise PermissionError("queue state changes only through append_turn or session deletion")
        object.__setattr__(self, name, value)

    def snapshot(self) -> tuple[str, ...]:
        return tuple(item["episode_id"] for item in self._items)

    def prompt_tokens(self) -> int:
        return self.system_tokens + self.working.token_count() + self._queue_tokens() + count_tokens(self.summary)

    def append_turn(self, episode: Episode) -> None:
        self._items.append(
            {
                "episode_id": episode.episode_id,
                "text": episode.text,
                "tokens": episode.token_count,
                "session_id": episode.session_id,
            }
        )
        self.journal.add(event="enqueue", episode_id=episode.episode_id, prompt_tokens=self.prompt_tokens())
        self._flush_until_under_budget()
        self._pressure()
        # Pressure pins add working-context tokens, so the budget is checked again.
        self._flush_until_under_budget()

    def _flush_until_under_budget(self) -> None:
        cycles = 0
        while self.prompt_tokens() >= self.budget and cycles < 8:
            self._flush_once()
            cycles += 1
        if self.prompt_tokens() >= self.budget:
            raise FlushIntegrityError("queue remained over budget after flush")

    def drop_episode_ids(self, episode_ids: set[str]) -> None:
        self._items[:] = [item for item in self._items if item["episode_id"] not in episode_ids]

    def _pressure(self) -> None:
        if self.budget <= 0:
            return
        ratio = self.prompt_tokens() / self.budget
        if ratio < self.warning_fraction:
            self._pressure_latched = False
            return
        if self._pressure_latched:
            return
        self._pressure_latched = True
        self.journal.add(event="memory_pressure", prompt_tokens=self.prompt_tokens(), budget=self.budget)
        for edge in self.store.edges:
            if edge.status == "closed":
                continue
            compact = (
                f"{edge.session_id}|{edge.subject}|{edge.predicate}|{edge.object}|"
                f"{edge.serial if edge.serial is not None else '-'}"
            )
            self.working.edit("pin_fact", compact)
            if edge.status == "unresolved":
                self.working.edit("add_conflict", f"{edge.session_id}|{edge.subject}|{edge.predicate}|{edge.object}")

    def _flush_once(self) -> None:
        if not self._items:
            raise FlushIntegrityError("prompt is over budget and the queue is empty")
        remaining = list(self._items)
        planned: list[dict] = []
        while remaining:
            evicted = sum(item["tokens"] for item in planned)
            estimate = self._estimate(remaining, self.summary)
            under = estimate <= self.budget
            half = evicted >= max(1, self.budget // 2)
            if under and half:
                break
            planned.append(remaining.pop(0))
        if not planned:
            raise FlushIntegrityError("flush planned an empty span")
        if self.eviction_mode == "structured":
            self._assert_integrity(planned)
        new_summary = self._next_summary(planned)
        self._items[:] = remaining
        object.__setattr__(self, "summary", new_summary)
        object.__setattr__(self, "summary_pointer", "summary-" + planned[-1]["episode_id"])
        self.flush_count += 1
        self.journal.add(
            event="flush",
            evicted=[item["episode_id"] for item in planned],
            prompt_tokens=self.prompt_tokens(),
            mode=self.eviction_mode,
            summary_pointer=self.summary_pointer,
        )
        if self.prompt_tokens() < self.warning_fraction * self.budget:
            self._pressure_latched = False

    def _assert_integrity(self, planned: list[dict]) -> None:
        for item in planned:
            episode = self.store.peek_episode(item["episode_id"])
            if episode is None:
                raise FlushIntegrityError(f"missing episode {item['episode_id']}")
            for fact in episode.declared_facts:
                matched = [
                    edge
                    for edge in self.store.edges_for_episode(episode.episode_id)
                    if edge.subject == fact["subject"]
                    and edge.predicate == fact["predicate"]
                    and edge.object == fact["object"]
                ]
                if not matched:
                    raise FlushIntegrityError(
                        f"flush would drop {fact['subject']} {fact['predicate']} without an edge"
                    )

    def _next_summary(self, planned: list[dict]) -> str:
        if self.eviction_mode == "structured":
            ids = []
            planned_ids = {item["episode_id"] for item in planned}
            for edge in self.store.edges:
                if edge.episode_id in planned_ids:
                    ids.append(edge.edge_id)
            addition = "edges:" + ",".join(ids)
        else:
            prose = " ".join(item["text"] for item in planned)
            addition = " ".join(prose.split()[:12])
        words = (self.summary + " " + addition).split()
        cap = min(40, max(8, self.budget // 4))
        return " ".join(words[-cap:])

    def _estimate(self, items: list[dict], summary: str) -> int:
        return self.system_tokens + self.working.token_count() + sum(item["tokens"] for item in items) + count_tokens(summary)

    def _queue_tokens(self) -> int:
        return sum(item["tokens"] for item in self._items)


class FifoWindow:
    """Long-context control: concatenate chunks and drop the earliest past the budget."""

    def __init__(self, budget_tokens: int) -> None:
        self.budget = int(budget_tokens)
        self.chunks: list[tuple[str, str, int]] = []

    def add(self, chunk_id: str, text: str) -> None:
        self.chunks.append((chunk_id, text, count_tokens(text) or 1))
        while len(self.chunks) > 1 and sum(chunk[2] for chunk in self.chunks) > self.budget:
            self.chunks.pop(0)

    def contains(self, chunk_id: str) -> bool:
        return any(chunk[0] == chunk_id for chunk in self.chunks)

    def text(self) -> str:
        return "\n".join(chunk[1] for chunk in self.chunks)

    def ids(self) -> list[str]:
        return [chunk[0] for chunk in self.chunks]


def canonicalize_subject(subject: str, known: list[str], recent_blob: str) -> str:
    """Exact strings match anywhere. A casefold variant merges only if the canonical spelling is in the recent window."""

    if subject in known:
        return subject
    folded = subject.casefold()
    for name in known:
        if name.casefold() == folded and name in recent_blob:
            return name
    return subject


def _provenance_ok(edge: Edge) -> bool:
    has_serial = edge.serial is not None
    has_interval = edge.t_valid is not None and edge.t_created is not None
    return bool(edge.episode_id and edge.session_id and (has_serial or has_interval))


def _strictly_newer(edge: Edge, other: Edge) -> bool:
    if edge.serial is not None and other.serial is not None:
        return edge.serial > other.serial
    if edge.t_valid and other.t_valid:
        return edge.t_valid > other.t_valid
    return False


def _close(edge: Edge, t_invalid: str | None, now: str) -> None:
    """Close an edge. A second closure only narrows the interval."""

    if edge.t_invalid is None or (t_invalid is not None and t_invalid < edge.t_invalid):
        edge.t_invalid = t_invalid
    edge.t_expired = edge.t_expired or now
    edge.status = "closed"


def _tie(edge: Edge, other: Edge) -> bool:
    return edge.serial is not None and edge.serial == other.serial and edge.object != other.object


def valid_at(edge: Edge, instant: str) -> bool:
    if edge.t_valid is None or edge.t_valid > instant:
        return False
    if edge.t_invalid is not None and edge.t_invalid <= instant:
        return False
    return True
