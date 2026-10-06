"""Ingest, routing, and post-retrieval assembly.

The extractor lists matching facts and does not choose a winner. Code applies
argmax only on current-value questions that declare a total order. Historical,
aggregate, abstention, and irreducible questions use other operators.
"""

from __future__ import annotations

import re
from typing import Protocol

from fieldlog.conflict import render_response, rubric, select_action
from fieldlog.memory import FifoWindow, MemoryStore, QueueManager, WorkingContext, canonicalize_subject
from fieldlog.models import Episode, episode_token_count, load_schemas, normalize_explicit_fact, parse_fact_lines
from fieldlog.retrieve import Hit, expand_month, retrieve
from fieldlog.schema_guard import assert_dialect, require_audit, validate
from fieldlog.support import (
    CitationError,
    Clock,
    Journal,
    RetryableError,
    RouterError,
    SchemaRejected,
    call_with_retry,
    parse_instant,
)

QUESTION_TYPES = (
    "current_value",
    "historical",
    "aggregate",
    "abstain",
    "temporal_reasoning",
    "irreducible",
    "direct_read",
)

ROUTER_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://example.com/fieldlog/router.json",
    "type": "object",
    "additionalProperties": False,
    "required": ["question_type"],
    "properties": {"question_type": {"enum": list(QUESTION_TYPES)}},
}


PLACEHOLDER = re.compile(r"\{[A-Za-z_]+\}")


class Extractor(Protocol):
    """Seam for a model-backed extractor. Its output passes the closed fact check before any write."""

    def extract(self, text: str, prior_texts: list[str], reference_time: str) -> list[dict]: ...


class ScriptedExtractor:
    """Deterministic stand-in for a model extractor. FACT lines in, candidates out."""

    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.calls = 0
        self.contexts: list[list[str]] = []

    def extract(self, text: str, prior_texts: list[str], reference_time: str) -> list[dict]:
        self.calls += 1
        self.contexts.append(list(prior_texts))
        if self.calls <= self.fail_times:
            raise RetryableError("transient extractor failure")
        return parse_fact_lines(text, reference_time)


class Lab:
    def __init__(
        self,
        budget_tokens: int = 80,
        warning_fraction: float = 0.70,
        system_tokens: int = 0,
        clock: Clock | None = None,
        provider: Extractor | None = None,
        eviction_mode: str = "structured",
        schemas: dict | None = None,
    ) -> None:
        self.journal = Journal()
        self.clock = clock or Clock()
        self.provider = provider or ScriptedExtractor()
        self.schemas = schemas or load_schemas()
        for schema in self.schemas.values():
            assert_dialect(schema)
            require_audit(schema)
        require_audit(ROUTER_SCHEMA)
        self.store = MemoryStore(self.clock, self.journal, self.schemas)
        self.working = WorkingContext(self.journal)
        self.queue = QueueManager(
            budget_tokens,
            self.journal,
            self.store,
            self.working,
            warning_fraction=warning_fraction,
            system_tokens=system_tokens,
            eviction_mode=eviction_mode,
        )
        self.fifo = FifoWindow(budget_tokens)
        self._done: dict[str, dict] = {}
        self.budget_tokens = budget_tokens

    def ingest(
        self,
        session_id: str,
        text: str,
        reference_time: str,
        actor: str = "user",
        kind: str = "message",
        facts: list[dict] | None = None,
        client_token: str | None = None,
        user_id: str = "crew",
        user_scope: bool = False,
    ) -> dict:
        if client_token and client_token in self._done:
            self.journal.add(event="idempotent_replay", client_token=client_token)
            return self._done[client_token]
        prior = self.store.recent_texts(session_id, 4)

        def attempt() -> list[dict]:
            parsed = self.provider.extract(text, prior, reference_time)
            explicit = [normalize_explicit_fact(fact, reference_time, text) for fact in (facts or [])]
            return parsed + explicit

        extracted = call_with_retry(attempt, self.clock, journal=self.journal)
        recent_blob = "\n".join(prior + [text])
        known = self.store.known_subjects(session_id)
        normalized = []
        for fact in extracted:
            # Extractor output is untrusted: check every fact before the episode is written.
            updated = self.store.check_fact(text, fact)
            updated["subject"] = canonicalize_subject(updated["subject"], known, recent_blob)
            if updated["subject"] not in known:
                known.append(updated["subject"])
            normalized.append(updated)
        episode_id, turn_index = self.store.next_episode_id(session_id)
        episode = Episode(
            episode_id=episode_id,
            session_id=session_id,
            actor=actor,
            kind=kind,
            text=text,
            reference_time=parse_instant(reference_time),
            token_count=episode_token_count(text),
            turn_index=turn_index,
            user_id=user_id,
            user_scope=user_scope,
            declared_facts=normalized,
        )
        self.store.add_episode(episode)
        if any(line.strip().upper().startswith("INSTR") for line in text.splitlines()):
            self.journal.add(event="untrusted_episode", episode_id=episode_id, session_id=session_id)
        edges = []
        for fact in normalized:
            edges.append(self.store.insert_edge(session_id, episode, fact))
        self.queue.append_turn(episode)
        self.fifo.add(episode_id, text)
        self.clock.advance(1)
        result = {"episode_id": episode_id, "edge_ids": [edge.edge_id for edge in edges]}
        if client_token:
            self._done[client_token] = result
        return result

    def corpus_tokens(self, session_id: str | None = None) -> int:
        episodes = self.store.episodes if session_id is None else self.store.session_episodes(session_id)
        return sum(episode.token_count for episode in episodes)

    def ask(self, session_id: str, question: dict, k: int = 10, key_mode: str = "fact_augmented", scope: str = "session") -> dict:
        routed = route(question)
        qtype = routed["question_type"]
        self.journal.add(event="route", question_type=qtype, question_id=question.get("id"))
        if qtype == "irreducible":
            return self.ask_conflict(session_id, question)
        if qtype == "aggregate":
            answer = assemble_aggregate(self.store, session_id, question)
            return self._finish(session_id, question, answer, [], [])
        window = None
        if qtype == "temporal_reasoning":
            window = tuple(question["window"]) if question.get("window") else expand_month(question.get("text", ""))
        query = question.get("query") or f"{question.get('subject', '')} {question.get('predicate', '')} {question.get('text', '')}"
        hits = retrieve(
            self.store,
            self.journal,
            session_id,
            query,
            k=k,
            scope=scope,
            user_id=question.get("user_id", "crew"),
            window=window,
            key_mode=key_mode,
        )
        notes = annotate_notes(hits, question)
        retrieved_ids = list(dict.fromkeys(hit.episode_id for hit in hits))
        if qtype == "direct_read" and question.get("require_all"):
            answer = assemble_all(self.store, session_id, question)
            return self._finish(session_id, question, answer, notes, retrieved_ids)
        if qtype == "direct_read":
            considered = [hit for hit, note in zip(hits, notes) if note["note"] != "irrelevant"]
            answer = assemble_current(considered, question, self.store, session_id)
            return self._finish(session_id, question, answer, notes, retrieved_ids)
        matched = _matched(hits, question)
        if qtype == "abstain" and not matched:
            answer = abstain_answer(self.store, session_id, question)
            return self._finish(session_id, question, answer, notes, retrieved_ids)
        if qtype == "abstain":
            # The question presumed the fact was never stated, but a stored edge matches.
            # Answer from the cited edge instead of reporting a false abstention reason.
            answer = assemble_current(matched, question, self.store, session_id)
            return self._finish(session_id, question, answer, notes, retrieved_ids)
        if qtype == "historical":
            answer = assemble_historical(matched, question, self.store, session_id)
            return self._finish(session_id, question, answer, notes, retrieved_ids)
        if qtype in {"current_value", "temporal_reasoning"}:
            answer = assemble_current(matched, question, self.store, session_id)
            return self._finish(session_id, question, answer, notes, retrieved_ids)
        raise RouterError(qtype)

    def ask_conflict(self, session_id: str, question: dict) -> dict:
        subject = question["aspect_subject"]
        predicate = question["aspect_predicate"]
        edges = self.store.edges_for(session_id, subject, predicate)
        memories = [
            {
                "value": edge.object,
                "context": edge.context or "",
                "source": edge.source_label,
                "time": edge.t_valid or "",
                "episode_id": edge.episode_id,
                "text": edge.span,
                "subject": edge.subject,
                "predicate": edge.predicate,
            }
            for edge in edges
        ]
        consequence = question.get("consequence", "low")
        decision = select_action(memories, question.get("text", ""), consequence)
        rendered = render_response(decision, memories)
        if decision["action"] == "commit":
            answer = resolved(
                decision.get("entity", ""),
                [memory["episode_id"] for memory in memories if memory.get("episode_id")],
                "direct",
                decision["action"],
            )
        else:
            answer = unresolved(decision["action"], decision["missing_variable"], decision["alternatives"])
        packed = self._finish(session_id, question, answer, [], [memory["episode_id"] for memory in memories])
        packed["decision"] = decision
        packed["rendered"] = rendered
        packed["memories"] = memories
        packed["rubric"] = rubric(decision, memories, consequence)
        return packed

    def chain_aware(self, session_id: str, hops: list[dict]) -> dict:
        """Chain-aware resolution: one relation per hop, substitute the entity, then continue.

        A hop may carry `{previous}` in its text; it is replaced by the entity resolved on the
        hop before. An empty hop, or a placeholder that is still present after substitution,
        abstains instead of guessing.
        """

        if not hops:
            return self._finish(session_id, {"id": "chain"}, abstain("empty_retrieval"), [], [])
        subject = None
        episode_ids: list[str] = []
        order_key: dict = {}
        for index, hop in enumerate(hops):
            if hop.get("subject_from_previous"):
                if not subject:
                    answer = abstain("empty_retrieval")
                    return self._finish(session_id, {"id": f"hop-{index}"}, answer, [], episode_ids)
                hop_subject = subject
            else:
                hop_subject = hop["subject"]
            text = hop.get("text", f"{hop_subject} {hop['predicate']}")
            if subject:
                text = text.replace("{previous}", subject)
            hop_question = {
                "id": f"hop-{index}",
                "question_type": "current_value",
                "subject": hop_subject,
                "predicate": hop["predicate"],
                "text": text,
            }
            if PLACEHOLDER.search(text) or PLACEHOLDER.search(hop_subject):
                self.journal.add(event="unsubstituted_placeholder", question_id=hop_question["id"])
                return self._finish(session_id, hop_question, abstain("empty_retrieval"), [], episode_ids)
            result = self.ask(session_id, hop_question)
            if result["answer"]["status"] != "resolved":
                answer = abstain("empty_retrieval")
                return self._finish(session_id, hop_question, answer, [], episode_ids)
            subject = result["answer"]["entity"]
            episode_ids.extend(result["answer"]["episode_ids"])
            order_key = result["answer"]["order_key"]
        answer = resolved(subject or "", episode_ids, order_key["kind"], order_key["value"])
        return self._finish(session_id, {"id": "chain"}, answer, [], episode_ids)

    def no_decomposition(self, session_id: str, range_subject: str, range_predicate: str, warden_predicate: str) -> str:
        ranges = sorted(
            self.store.edges_for(session_id, range_subject, range_predicate),
            key=lambda edge: edge.order_key(),
        )
        if not ranges:
            return ""
        oldest = ranges[0]
        wardens = self.store.edges_for(session_id, oldest.object, warden_predicate)
        return wardens[0].object if wardens else ""

    def fifo_entity(self, subject: str, predicate: str) -> str:
        facts = parse_fact_lines(self.fifo.text(), "2026-04-01T00:00:00Z")
        matched = [fact for fact in facts if fact["subject"] == subject and fact["predicate"] == predicate]
        if not matched:
            return ""
        if all(fact.get("serial") is not None for fact in matched):
            winner = max(matched, key=lambda fact: fact["serial"])
        else:
            winner = max(matched, key=lambda fact: fact.get("t_valid") or "")
        return winner["object"]

    def direct_entity(self, session_id: str, question: dict, confuse_at: int, k: int = 10) -> str:
        query = f"{question.get('subject', '')} {question.get('predicate', '')} {question.get('text', '')}"
        hits = retrieve(self.store, self.journal, session_id, query, k=k)
        matched = _matched(hits, question)
        return scripted_direct(question.get("text", ""), matched, self.corpus_tokens(session_id), confuse_at)

    def delete_session(self, session_id: str) -> None:
        ids = {episode.episode_id for episode in self.store.session_episodes(session_id)}
        self.store.delete_session(session_id)
        self.queue.drop_episode_ids(ids)
        self.working.drop_session_pins(session_id)

    def _finish(self, session_id: str, question: dict, answer: dict, notes: list[dict], retrieved_ids: list[str]) -> dict:
        result = validate(answer, self.schemas["answer"])
        if not result["valid"]:
            raise SchemaRejected(result["errors"][0]["error"])
        if answer["status"] == "resolved":
            for episode_id in answer["episode_ids"]:
                if self.store.peek_episode(episode_id) is None:
                    raise CitationError(episode_id)
        self.journal.add(
            event="answer",
            question_id=question.get("id"),
            status=answer["status"],
            session_id=session_id,
            entity=answer.get("entity"),
            order_key=answer.get("order_key"),
            action=answer.get("action"),
        )
        return {"answer": answer, "notes": notes, "retrieved_ids": retrieved_ids}


def route(question: dict) -> dict:
    explicit = question.get("question_type")
    if explicit is None:
        explicit = infer_type(_strip_instructions(question.get("text") or ""))
    if explicit not in QUESTION_TYPES:
        raise RouterError(f"unknown question type: {explicit}")
    record = {"question_type": explicit}
    result = validate(record, ROUTER_SCHEMA)
    if not result["valid"]:
        raise RouterError(result["errors"][0]["error"])
    return record


def infer_type(text: str) -> str:
    lowered = text.casefold()
    if "never" in lowered and ("mentioned" in lowered or "stated" in lowered):
        return "abstain"
    if any(word in lowered for word in ("previous", "earlier", "before", "used to", "as of")):
        return "historical"
    if lowered.startswith("how many") or "total number" in lowered:
        return "aggregate"
    if "which ridge" in lowered or "may the crew" in lowered:
        return "irreducible"
    return "current_value"


def assemble_current(hits: list[Hit], question: dict, store: MemoryStore, session_id: str) -> dict:
    matched = [hit for hit in hits if hit.object]
    if question.get("total_order") is False and matched:
        return unresolved("defer", "partial_order", _unique(hit.object for hit in matched))
    if not matched:
        return abstain_answer(store, session_id, question)
    if all(hit.serial is not None for hit in matched):
        best = max(hit.serial for hit in matched)
        winners = [hit for hit in matched if hit.serial == best]
        objects = _unique(hit.object for hit in winners)
        if len(objects) > 1:
            return unresolved("defer", "serial_tie", objects)
        winner = sorted(winners, key=lambda hit: hit.episode_id)[0]
        return resolved(_prefix(winner.object, question), [winner.episode_id], "serial", winner.serial)
    if all(hit.t_valid for hit in matched):
        best_time = max(hit.t_valid for hit in matched)
        winners = [hit for hit in matched if hit.t_valid == best_time]
        objects = _unique(hit.object for hit in winners)
        if len(objects) > 1:
            return unresolved("defer", "timestamp_tie", objects)
        winner = sorted(winners, key=lambda hit: hit.episode_id)[0]
        return resolved(_prefix(winner.object, question), [winner.episode_id], "timestamp", winner.t_valid or "")
    return unresolved("defer", "partial_order", _unique(hit.object for hit in matched if hit.object))


def assemble_historical(hits: list[Hit], question: dict, store: MemoryStore, session_id: str) -> dict:
    matched = [hit for hit in hits if hit.object]
    if not matched:
        return abstain_answer(store, session_id, question)
    if question.get("which") == "as_of":
        instant = question["as_of"]
        chosen = [hit for hit in matched if _hit_valid_at(hit, instant)]
        objects = _unique(hit.object for hit in chosen)
        if len(objects) != 1:
            if not objects:
                return abstain("empty_retrieval")
            return unresolved("defer", "interval", objects)
        hit = chosen[0]
        return resolved(hit.object or "", [hit.episode_id], "timestamp", hit.t_valid or "")
    if all(hit.serial is not None for hit in matched):
        ordered = sorted(matched, key=lambda hit: (hit.serial, hit.episode_id))
        kind = "serial"
    else:
        ordered = sorted(matched, key=lambda hit: (hit.t_valid or "", hit.episode_id))
        kind = "timestamp"
    if len(ordered) < 2:
        return abstain("empty_retrieval")
    chosen = ordered[-2]
    value = chosen.serial if kind == "serial" else (chosen.t_valid or "")
    return resolved(chosen.object or "", [chosen.episode_id], kind, value)


def assemble_aggregate(store: MemoryStore, session_id: str, question: dict) -> dict:
    start, end = question["interval"]
    edges = store.edges_for(session_id, question.get("subject"), question.get("predicate"))
    chosen = [edge for edge in edges if edge.t_valid and start <= edge.t_valid < end]
    if question.get("reduction") == "list":
        entity = ",".join(sorted({edge.object for edge in chosen}))
    else:
        entity = str(len(chosen))
    return resolved(entity, [edge.episode_id for edge in chosen], "reduction", entity)


def assemble_all(store: MemoryStore, session_id: str, question: dict) -> dict:
    edges = store.edges_for(session_id, question.get("subject"), question.get("predicate"))
    entity = ",".join(sorted({edge.object for edge in edges}))
    return resolved(entity, [edge.episode_id for edge in edges], "direct", entity)


def abstain_answer(store: MemoryStore, session_id: str, question: dict) -> dict:
    """No candidate matched. never_stated only when the subject appears in no visible episode."""

    subject = question.get("subject", "")
    if subject and store.subject_mentioned(session_id, subject):
        return abstain("empty_retrieval")
    return abstain("never_stated")


def annotate_notes(hits: list[Hit], question: dict) -> list[dict]:
    subject = question.get("subject")
    predicate = question.get("predicate")
    matched = _matched(hits, question)
    objects = {hit.object for hit in matched}
    notes = []
    for hit in hits:
        if hit.subject != subject or hit.predicate != predicate:
            note = "irrelevant"
        elif len(objects) > 1:
            note = "contradicts"
        else:
            note = "supports"
        notes.append({"episode_id": hit.episode_id, "note": note, "object": hit.object})
    return notes


def scripted_direct(question_text: str, candidates: list, corpus_tokens: int, confuse_at: int) -> str:
    """Entangled reader. Long corpora prefer the stale serial; historical wording branches."""

    if not candidates:
        return ""
    text = question_text.casefold()
    if corpus_tokens >= confuse_at:
        chosen = min(candidates, key=_candidate_key)
    elif any(word in text for word in ("previous", "earlier", "before", "used to")):
        ordered = sorted(candidates, key=_candidate_key)
        chosen = ordered[-2] if len(ordered) >= 2 else ordered[-1]
    else:
        chosen = max(candidates, key=_candidate_key)
    entity = chosen.object
    if text.startswith("was ") or text.startswith("did "):
        return f"yes: {entity}"
    return entity


def naive_newest(candidates: list) -> str:
    if not candidates:
        return ""
    return max(candidates, key=_candidate_key).object


def resolved(entity: str, episode_ids: list[str], kind: str, value) -> dict:
    return {
        "status": "resolved",
        "entity": entity,
        "episode_ids": list(dict.fromkeys(episode_ids)),
        "order_key": {"kind": kind, "value": value},
    }


def abstain(reason: str) -> dict:
    return {"status": "abstain", "reason": reason}


def unresolved(action: str, missing: str, alternatives: list[str]) -> dict:
    return {
        "status": "unresolved",
        "action": action,
        "missing_variable": missing,
        "alternatives": list(alternatives),
    }


def subem(prediction: str, gold: str) -> bool:
    return gold.casefold() in (prediction or "").casefold()


def _matched(hits: list[Hit], question: dict) -> list[Hit]:
    subject = question.get("subject")
    predicate = question.get("predicate")
    return [hit for hit in hits if hit.subject == subject and hit.predicate == predicate and hit.object]


def _prefix(entity: str, question: dict) -> str:
    if question.get("yes_no"):
        return f"yes: {entity}"
    return entity


def _unique(values) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen


def _candidate_key(candidate) -> tuple:
    serial = candidate.serial if candidate.serial is not None else -1
    return (serial, getattr(candidate, "t_valid", None) or "", getattr(candidate, "episode_id", "") or "")


def _hit_valid_at(hit: Hit, instant: str) -> bool:
    if hit.t_valid is None or hit.t_valid > instant:
        return False
    if hit.t_invalid is not None and hit.t_invalid <= instant:
        return False
    return True


def _strip_instructions(text: str) -> str:
    kept = []
    for line in text.splitlines():
        if line.strip().upper().startswith("INSTR"):
            continue
        kept.append(line)
    return "\n".join(kept)


def filler_text(tokens: int, label: str = "pad") -> str:
    return " ".join([label] * tokens)
