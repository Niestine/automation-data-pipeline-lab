"""Informational OS-thread smoke for the fixed protocols.

This is not the regression gate. It does not write a schedule artifact, and a
caller that treats a smoke miss as a CI failure is using it wrong. The gate is
the recorded schedule.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from schedlab.environment import probe


def native_smoke(iterations: int = 3, artifact_dir: Optional[Path] = None) -> dict[str, object]:
    """Run the fixed lock protocols on OS threads. ``artifact_dir`` is ignored."""

    del artifact_dir
    if iterations < 1:
        raise ValueError("iterations must be >= 1")
    rows = []
    for _ in range(iterations):
        rows.append(
            {
                "ordering": _ordering_once(),
                "atomicity": _atomicity_once(),
                "deadlock": _deadlock_once(),
            }
        )
    ok = all(
        row["ordering"] in (1, "skip")
        and row["atomicity"] == 2
        and row["deadlock"] == "finished"
        for row in rows
    )
    return {
        "informational": True,
        "artifact_written": False,
        "ok": ok,
        "gil": probe(),
        "iterations": rows,
    }


def _ordering_once() -> object:
    lock = threading.Lock()
    state = {"value": 0, "ready": 0}
    observed: dict[str, object] = {"v": None}

    def init() -> None:
        with lock:
            state["value"] = 1
            state["ready"] = 1

    def use() -> None:
        with lock:
            seen = state["ready"]
            if seen == 1:
                observed["v"] = state["value"]
            else:
                observed["v"] = "skip"

    threads = [threading.Thread(target=init), threading.Thread(target=use)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1.0)
    return observed["v"]


def _atomicity_once() -> int:
    lock = threading.Lock()
    cell = {"x": 0}

    def worker() -> None:
        with lock:
            cell["x"] = cell["x"] + 1

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1.0)
    return cell["x"]


def _deadlock_once() -> str:
    first = threading.Lock()
    second = threading.Lock()
    done: list[int] = []

    def worker() -> None:
        with first:
            with second:
                done.append(1)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    alive = False
    for thread in threads:
        thread.join(timeout=1.0)
        alive = alive or thread.is_alive()
    if alive or len(done) != 2:
        return "blocked"
    return "finished"
