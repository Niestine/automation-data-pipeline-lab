"""Synthetic catalog, knowledge base, drafts, calendar, and publish queue."""

from __future__ import annotations

from typing import Any, Optional
import copy

from .models import CLASS_RANK


DEFAULT_ASSETS: dict[str, dict[str, Any]] = {
    "AST-101": {"title": "Weekly recap source A", "classification": "public", "kind": "clip"},
    "AST-102": {"title": "Studio B-roll notes", "classification": "internal", "kind": "clip"},
    "AST-103": {"title": "Unreleased launch cut", "classification": "restricted", "kind": "clip"},
}

DEFAULT_NOTES: dict[str, dict[str, Any]] = {
    "NOTE-10": {
        "title": "Public style guide",
        "classification": "public",
        "body": "Keep recaps under 120 words.",
    },
    "NOTE-20": {
        "title": "Internal tone notes",
        "classification": "internal",
        "body": "Avoid unreleased titles.",
    },
    "NOTE-30": {
        "title": "Legal hold",
        "classification": "restricted",
        "body": "Do not publish embargoed cuts.",
    },
}

DEFAULT_SLOTS: dict[str, dict[str, Any]] = {
    "SLOT-A": {"at": "2026-10-06T10:00:00Z", "available": True, "held_by": None},
    "SLOT-B": {"at": "2026-10-06T15:00:00Z", "available": True, "held_by": None},
}


def _classified_records(kind: str, records: Any) -> dict[str, dict[str, Any]]:
    """Copy catalog/note records, failing closed on a missing or unknown classification."""
    if not isinstance(records, dict):
        raise ValueError(f"{kind} records must be an object")
    copied: dict[str, dict[str, Any]] = {}
    for key, value in records.items():
        if not isinstance(value, dict):
            raise ValueError(f"{kind} {key} must be an object")
        if value.get("classification") not in CLASS_RANK:
            raise ValueError(f"{kind} {key} has missing or unknown classification")
        copied[str(key)] = dict(value)
    return copied


class ToolError(Exception):
    def __init__(self, message: str, *, code: str, transient: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.transient = transient
        self.message = message


class ToolFaults:
    """Ordered per-packet fault script consumed when a matching tool is invoked."""

    def __init__(self, script: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.script = script or {}
        self._cursors: dict[str, int] = {}

    def next_for(self, packet_id: str, tool: str) -> Optional[dict[str, Any]]:
        steps = self.script.get(packet_id) or []
        index = self._cursors.get(packet_id, 0)
        while index < len(steps):
            step = steps[index]
            index += 1
            self._cursors[packet_id] = index
            if str(step.get("tool") or "") == tool:
                return step
        self._cursors[packet_id] = index
        return None

    def reset(self) -> None:
        self._cursors.clear()


class BriefWorkspace:
    def __init__(
        self,
        *,
        assets: dict[str, dict[str, Any]] | None = None,
        notes: dict[str, dict[str, Any]] | None = None,
        slots: dict[str, dict[str, Any]] | None = None,
        faults: ToolFaults | None = None,
    ) -> None:
        source_assets = assets if assets is not None else DEFAULT_ASSETS
        source_notes = notes if notes is not None else DEFAULT_NOTES
        source_slots = slots if slots is not None else DEFAULT_SLOTS
        self.assets = _classified_records("asset", source_assets)
        self.notes = _classified_records("note", source_notes)
        if not isinstance(source_slots, dict) or not all(isinstance(v, dict) for v in source_slots.values()):
            raise ValueError("slots must be an object of objects")
        self.slots = {str(key): dict(value) for key, value in source_slots.items()}
        self.drafts: dict[str, dict[str, Any]] = {}
        self.publish_queue: list[dict[str, Any]] = []
        self.mutations: list[tuple[str, str, dict[str, Any]]] = []
        self.faults = faults if faults is not None else ToolFaults()

    def classification(self, kind: str, item_id: str) -> Optional[str]:
        if kind == "asset":
            item = self.assets.get(item_id)
        elif kind == "note":
            item = self.notes.get(item_id)
        else:
            return None
        if item is None:
            return None
        return str(item["classification"])

    def invoke(
        self,
        packet_id: str,
        tool: str,
        args: dict[str, Any],
        *,
        dry_run: bool,
        clearance: str = "public",
    ) -> dict[str, Any]:
        """Run one tool. `clearance` is the packet workspace; search hits above it are hidden."""
        if clearance not in CLASS_RANK:
            raise ToolError(f"unknown clearance {clearance}", code="bad_request", transient=False)
        fault = self.faults.next_for(packet_id, tool)
        if fault and fault.get("error"):
            raise ToolError(
                str(fault.get("message") or "tool fault"),
                code=str(fault.get("code") or "transient_io"),
                transient=bool(fault.get("transient", True)),
            )
        if tool == "catalog.search":
            return self._catalog_search(args, clearance)
        if tool == "catalog.get":
            return self._catalog_get(args)
        if tool == "kb.search":
            return self._kb_search(args, clearance)
        if tool == "kb.get":
            return self._kb_get(args)
        if tool == "draft.compose":
            return self._compose(packet_id, args, dry_run=dry_run)
        if tool == "draft.cite":
            return self._cite(args, dry_run=dry_run)
        if tool == "review.submit":
            return self._submit(args, dry_run=dry_run)
        if tool == "publish.queue":
            return self._publish(args, dry_run=dry_run)
        if tool == "calendar.hold":
            return self._hold(args, dry_run=dry_run)
        if tool == "refuse":
            return {"refused": True, "reason": str(args.get("reason") or "refused")}
        raise ToolError(f"unknown tool {tool}", code="unknown_tool", transient=False)

    def _catalog_search(self, args: dict[str, Any], clearance: str) -> dict[str, Any]:
        query = str(args["query"]).lower()
        limit = int(args["limit"])
        hits = []
        for asset_id, asset in sorted(self.assets.items()):
            if CLASS_RANK[asset["classification"]] > CLASS_RANK[clearance]:
                continue
            if query in str(asset.get("title") or "").lower():
                hits.append(
                    {
                        "asset_id": asset_id,
                        "title": str(asset.get("title") or ""),
                        "classification": asset["classification"],
                    }
                )
            if len(hits) >= limit:
                break
        return {"hits": hits, "count": len(hits)}

    def _catalog_get(self, args: dict[str, Any]) -> dict[str, Any]:
        asset_id = str(args["asset_id"])
        asset = self.assets.get(asset_id)
        if asset is None:
            return {"found": False, "asset_id": asset_id}
        return {"found": True, "asset_id": asset_id, "asset": dict(asset)}

    def _kb_search(self, args: dict[str, Any], clearance: str) -> dict[str, Any]:
        query = str(args["query"]).lower()
        hits = []
        for note_id, note in sorted(self.notes.items()):
            if CLASS_RANK[note["classification"]] > CLASS_RANK[clearance]:
                continue
            blob = f"{note.get('title', '')} {note.get('body', '')}".lower()
            if query in blob:
                hits.append(
                    {
                        "note_id": note_id,
                        "title": str(note.get("title") or ""),
                        "classification": note["classification"],
                    }
                )
        return {"hits": hits, "count": len(hits)}

    def _kb_get(self, args: dict[str, Any]) -> dict[str, Any]:
        note_id = str(args["note_id"])
        note = self.notes.get(note_id)
        if note is None:
            return {"found": False, "note_id": note_id}
        return {"found": True, "note_id": note_id, "note": dict(note)}

    def _compose(self, packet_id: str, args: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
        draft_id = f"DRF-{packet_id}"
        payload = {
            "draft_id": draft_id,
            "status": "draft",
            "title": str(args["title"]),
            "source_ids": list(args["source_ids"]),
            "notes": str(args["notes"]),
            "citations": [],
        }
        if dry_run:
            return {**payload, "would_mutate": True}
        existing = self.drafts.get(draft_id)
        if existing is not None:
            if any(existing[key] != payload[key] for key in ("title", "source_ids", "notes")):
                raise ToolError(
                    f"draft {draft_id} already exists with different content",
                    code="draft_conflict",
                    transient=False,
                )
            return {**copy.deepcopy(existing), "replayed": True}
        stored = copy.deepcopy(payload)
        self.drafts[draft_id] = stored
        self.mutations.append(("compose", draft_id, {"title": stored["title"]}))
        return copy.deepcopy(stored)

    def _cite(self, args: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
        draft_id = str(args["draft_id"])
        note_id = str(args["note_id"])
        if note_id not in self.notes:
            raise ToolError(f"note {note_id} not found", code="not_found", transient=False)
        if dry_run:
            return {"draft_id": draft_id, "cited": note_id, "would_mutate": True}
        draft = self.drafts.get(draft_id)
        if draft is None:
            raise ToolError(f"draft {draft_id} not found", code="not_found", transient=False)
        if note_id not in draft["citations"]:
            draft["citations"].append(note_id)
            self.mutations.append(("cite", draft_id, {"note_id": note_id}))
        return {"draft_id": draft_id, "cited": note_id, "citations": list(draft["citations"])}

    def _submit(self, args: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
        draft_id = str(args["draft_id"])
        if dry_run:
            return {"draft_id": draft_id, "status": "submitted", "would_mutate": True}
        draft = self.drafts.get(draft_id)
        if draft is None:
            raise ToolError(f"draft {draft_id} not found", code="not_found", transient=False)
        if draft["status"] == "submitted" or draft["status"] == "queued":
            return {"draft_id": draft_id, "status": draft["status"], "replayed": True}
        draft["status"] = "submitted"
        self.mutations.append(("submit", draft_id, {"status": "submitted"}))
        return {"draft_id": draft_id, "status": "submitted"}

    def _publish(self, args: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
        draft_id = str(args["draft_id"])
        if dry_run:
            return {"draft_id": draft_id, "status": "queued", "would_mutate": True}
        draft = self.drafts.get(draft_id)
        if draft is None:
            raise ToolError(f"draft {draft_id} not found", code="not_found", transient=False)
        if draft["status"] == "queued":
            return {"draft_id": draft_id, "status": "queued", "replayed": True}
        if draft["status"] != "submitted":
            raise ToolError("draft has not been submitted for review", code="not_submitted", transient=False)
        draft["status"] = "queued"
        item = {"draft_id": draft_id, "title": draft["title"]}
        self.publish_queue.append(item)
        self.mutations.append(("publish", draft_id, dict(item)))
        return {"draft_id": draft_id, "status": "queued"}

    def _hold(self, args: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
        slot_id = str(args["slot_id"])
        draft_id = str(args["draft_id"])
        slot = self.slots.get(slot_id)
        if slot is None:
            raise ToolError(f"slot {slot_id} not found", code="not_found", transient=False)
        if dry_run:
            return {"slot_id": slot_id, "draft_id": draft_id, "held": True, "would_mutate": True}
        if slot.get("held_by") == draft_id:
            return {"slot_id": slot_id, "draft_id": draft_id, "held": True, "replayed": True}
        if not slot.get("available", False):
            raise ToolError(f"slot {slot_id} is taken", code="slot_taken", transient=False)
        slot["available"] = False
        slot["held_by"] = draft_id
        self.mutations.append(("hold", slot_id, {"draft_id": draft_id}))
        return {"slot_id": slot_id, "draft_id": draft_id, "held": True}
