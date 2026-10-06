"""Planted maintenance scenarios. Variants share yield names when a journal must survive the repair."""

from __future__ import annotations

import json
from pathlib import Path

from yieldlab.engine import Scenario

PROJECT = Path(__file__).resolve().parents[2]
EXAMPLES = PROJECT / "examples"


def load_port(path: Path | None = None) -> dict:
    raw = json.loads((path or (EXAMPLES / "port_tasks.json")).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("port file must be an object")
    tasks = raw.get("tasks")
    if not isinstance(tasks, list) or len(tasks) < 2:
        raise ValueError("port needs at least two tasks")
    copied = []
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError("task must be an object")
        ident = task.get("id")
        if not isinstance(ident, str) or not ident:
            raise ValueError("task id must be a non-empty string")
        if not isinstance(task.get("cancelled"), bool):
            raise ValueError("task cancelled must be a bool")
        copied.append({"id": ident, "cancelled": task["cancelled"]})
    return {"port": raw.get("port", ""), "tasks": copied}


def load_levels(path: Path | None = None) -> dict:
    raw = json.loads((path or (EXAMPLES / "level_batch.json")).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("level file must be an object")
    if not isinstance(raw.get("handler_level"), int):
        raise ValueError("handler_level must be an int")
    records = raw.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("records must be a non-empty list")
    cleaned = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("record must be an object")
        if not isinstance(record.get("message"), str) or not record["message"]:
            raise ValueError("record message must be a non-empty string")
        if not isinstance(record.get("level"), int):
            raise ValueError("record level must be an int")
        cleaned.append({"message": record["message"], "level": record["level"]})
    return {
        "handler_level": raw["handler_level"],
        "respect_handler_level": bool(raw.get("respect_handler_level", True)),
        "records": cleaned,
    }


def _header(**overrides) -> dict:
    header = {
        "owner": 1,
        "owner_alive": True,
        "local": 0,
        "shared": 0,
        "immortal": False,
        "state": "default",
        "alive": True,
        "finalizer": None,
        "deferred": False,
    }
    header.update(overrides)
    return header


def _ordering(variant: str) -> Scenario:
    if variant == "unfixed":
        threads = {
            1: [("write", "state", 1)],
            2: [("read", "state", "observed")],
            3: [("bc",)],
            4: [("bc",)],
        }
        unborn = frozenset()
    elif variant == "fixed":
        threads = {
            1: [("write", "state", 1), ("fork", 2)],
            2: [("read", "state", "observed")],
            3: [("bc",)],
            4: [("bc",)],
        }
        unborn = frozenset({2})
    else:
        raise ValueError(variant)
    return Scenario(
        name="ordering_d1",
        variant=variant,
        threads=threads,
        unborn=unborn,
        cells={"state": 0, "observed": -1},
        invariant_id="ordering_d1",
        holds=lambda world: world.cells["observed"] == 1,
    )


def _lost(variant: str) -> Scenario:
    if variant == "unfixed":
        body = [("load", "count"), ("add", 1), ("store", "count")]
    elif variant == "locked":
        body = [("acq", "inc"), ("load", "count"), ("add", 1), ("store", "count"), ("rel", "inc")]
    else:
        raise ValueError(variant)
    return Scenario(
        name="lost_update",
        variant=variant,
        threads={1: list(body), 2: list(body)},
        cells={"count": 0},
        invariant_id="lost_update",
        holds=lambda world: world.cells["count"] == 2,
    )


def _cancel(variant: str) -> Scenario:
    if variant not in ("unfixed", "fixed"):
        raise ValueError(variant)
    port = load_port()
    return Scenario(
        name="cancel_port",
        variant=variant,
        fuse_flag=variant == "fixed",
        threads={
            1: [("register",), ("take_tasks",), ("process",), ("process",), ("confirm",)],
            2: [("cancel_begin",), ("set_flag",), ("cancel_rest",)],
        },
        invariant_id="cancel_atomic",
        extra={
            "port": {
                "name": port["port"],
                "registered": False,
                "flag": False,
                "stuck": False,
                "handled": [],
                "queue": [],
                "tasks": port["tasks"],
            }
        },
        holds=_cancel_holds,
    )


def _cancel_holds(world) -> bool:
    port = world.extra["port"]
    if port.get("stuck"):
        return False
    if any(task["cancelled"] for task in port["tasks"]) and not port["flag"]:
        return False
    return True


def _log_deadlock(variant: str) -> Scenario:
    if variant != "unfixed":
        raise ValueError(variant)
    # Configuration takes the module lock, then the handler lock.
    # The re-entering emit holds the handler lock and then wants the module lock.
    return Scenario(
        name="log_deadlock",
        variant=variant,
        threads={
            1: [("acq", "handler"), ("acq", "module"), ("rel", "module"), ("rel", "handler")],
            2: [("acq", "module"), ("acq", "handler"), ("rel", "handler"), ("rel", "module")],
        },
        invariant_id="log_lock_order",
        holds=lambda world: True,
    )


def _log_queue(variant: str) -> Scenario:
    if variant != "fixed":
        raise ValueError(variant)
    batch = load_levels()
    emitter = [("enq", record["message"], record["level"]) for record in batch["records"]]
    listener = [("deq",) for _ in batch["records"]]
    return Scenario(
        name="log_queue",
        variant=variant,
        threads={1: emitter, 2: listener},
        respect_handler_level=batch["respect_handler_level"],
        handler_level=batch["handler_level"],
        invariant_id="log_queue",
        holds=_queue_holds,
    )


def _queue_holds(world) -> bool:
    delivered = [record["message"] for record in world.extra["delivered"]]
    skipped = [record["message"] for record in world.extra["skipped"]]
    return delivered == ["tick-high"] and skipped == ["tick-low"]


def _finalizer(variant: str) -> Scenario:
    if variant not in ("unfixed", "fixed"):
        raise ValueError(variant)
    return Scenario(
        name="finalizer_stw",
        variant=variant,
        finalize_in_stw=variant == "unfixed",
        finalizer_tid=9,
        threads={
            1: [("acq", "user"), ("stw",), ("rel", "user")],
            2: [("stw",)],
            9: [("acq", "user"), ("note", "finalized")],
        },
        unborn=frozenset({9}),
        invariant_id="finalizer_stw",
        extra={"headers": {"obj": _header(local=0, finalizer="take_user")}},
        holds=lambda world: "finalized" in world.marks and "finalized_in_stw" not in world.marks,
    )


def _refcount(variant: str) -> Scenario:
    # Threads 1 and 2 each already hold one shared reference. Each takes a
    # temporary reference and drops it; thread 1 then drops its own as well.
    # Every serial order leaves thread 2's reference: shared == 1.
    if variant == "unfixed":
        incref = [("incref_shared_read", "obj"), ("incref_shared_write", "obj")]
    elif variant == "fixed":
        incref = [("incref_shared_atomic", "obj")]
    else:
        raise ValueError(variant)
    threads = {
        1: [*incref, ("decref_shared", "obj"), ("decref_shared", "obj")],
        2: [*incref, ("decref_shared", "obj"), ("touch", "obj")],
    }
    return Scenario(
        name="refcount",
        variant=variant,
        threads=threads,
        invariant_id="refcount",
        extra={"headers": {"obj": _header(owner=3, local=0, shared=2)}},
        holds=_refcount_holds,
    )


def _refcount_holds(world) -> bool:
    header = world.extra["headers"]["obj"]
    return header["alive"] is True and header["shared"] == 1 and header["state"] == "weakrefs"


_BUILDERS = {
    "ordering_d1": _ordering,
    "lost_update": _lost,
    "cancel_port": _cancel,
    "log_deadlock": _log_deadlock,
    "log_queue": _log_queue,
    "finalizer_stw": _finalizer,
    "refcount": _refcount,
}


def build(name: str, variant: str) -> Scenario:
    try:
        builder = _BUILDERS[name]
    except KeyError as exc:
        raise ValueError(f"unknown scenario {name}") from exc
    return builder(variant)


def catalog() -> list[str]:
    return sorted(_BUILDERS)
