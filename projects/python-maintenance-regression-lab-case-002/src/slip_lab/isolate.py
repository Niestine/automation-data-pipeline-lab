"""Run one parser in a child process with a wall-clock timeout.

The child imports the target, then sends ``ready``. The timeout starts
after ``ready``, so interpreter startup and imports are not recorded as
a hang. A child that misses the timeout is terminated. A child that
exits without a result is a crash. Truncated payloads are flagged and
are not decoded.
"""

from __future__ import annotations

import importlib
import json
import multiprocessing
import os
from pathlib import Path

from slip_lab.model import SideResult

_STARTUP_BUDGET_S = 10.0


def _src_path() -> str:
    return str(Path(__file__).resolve().parents[1])


def _ensure_child_import_path() -> None:
    src = _src_path()
    current = os.environ.get("PYTHONPATH", "")
    parts = [part for part in current.split(os.pathsep) if part]
    if src not in parts:
        os.environ["PYTHONPATH"] = src + (os.pathsep + current if current else "")


def _retuple(value: object) -> object:
    if isinstance(value, list):
        return tuple(_retuple(item) for item in value)
    return value


def _isolated_worker(conn, src: str, target: str, blob: bytes, max_output_bytes: int) -> None:
    try:
        import sys

        if src not in sys.path:
            sys.path.insert(0, src)
        module_name, func_name = target.split(":")
        fn = getattr(importlib.import_module(module_name), func_name)
        conn.send({"status": "ready"})
        value = fn(blob)
        payload = json.dumps(value).encode("utf-8")
        truncated = len(payload) > max_output_bytes
        if truncated:
            payload = payload[:max_output_bytes]
        conn.send({"status": "ok", "truncated": truncated, "payload": payload})
    except Exception as exc:
        try:
            conn.send({"status": "crash", "exc_type": type(exc).__name__, "truncated": False})
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _kill(proc) -> None:
    if not proc.is_alive():
        return
    proc.terminate()
    proc.join(1.0)
    if proc.is_alive():
        proc.kill()
        proc.join(1.0)


_EXITED = {"status": "exited"}


def _recv(conn, timeout: float):
    """Return a message, ``None`` on timeout, or ``_EXITED`` on a closed pipe."""

    try:
        if conn.poll(timeout):
            return conn.recv()
    except (EOFError, OSError):
        return _EXITED
    return None


def _finish(proc, conn, timeout: float, startup_budget: float) -> SideResult:
    ready = _recv(conn, startup_budget)
    if ready is None:
        alive = proc.is_alive()
        _kill(proc)
        if alive:
            return SideResult("hang", None, None, False)
        return SideResult("crash", None, "ProcessExit", False)
    if ready is _EXITED:
        _kill(proc)
        return SideResult("crash", None, "ProcessExit", False)
    if ready.get("status") == "crash":
        proc.join(1.0)
        return SideResult("crash", None, ready.get("exc_type") or "Exception", False)
    if ready.get("status") != "ready":
        _kill(proc)
        return SideResult("crash", None, "ProtocolError", False)
    message = _recv(conn, timeout)
    if message is None:
        _kill(proc)
        return SideResult("hang", None, None, False)
    proc.join(1.0)
    if message is _EXITED:
        return SideResult("crash", None, "ProcessExit", False)
    if message.get("status") == "crash":
        return SideResult("crash", None, message.get("exc_type") or "Exception", False)
    if message.get("status") != "ok":
        return SideResult("crash", None, "ProtocolError", False)
    truncated = bool(message.get("truncated"))
    if truncated:
        return SideResult("ok", None, None, True)
    payload = message.get("payload") or b""
    value = _retuple(json.loads(payload.decode("utf-8")))
    return SideResult("ok", value, None, False)


def run_targets(
    legacy_target: str,
    hardened_target: str,
    blob: bytes,
    timeout: float,
    max_output_bytes: int,
    startup_budget: float = _STARTUP_BUDGET_S,
) -> tuple[SideResult, SideResult]:
    """Run both targets in parallel. Return ``(legacy, hardened)``."""

    _ensure_child_import_path()
    ctx = multiprocessing.get_context("spawn")
    src = _src_path()
    procs = []
    results: list[SideResult] = []
    try:
        for target in (legacy_target, hardened_target):
            parent, child = ctx.Pipe(duplex=False)
            proc = ctx.Process(
                target=_isolated_worker,
                args=(child, src, target, bytes(blob), max_output_bytes),
            )
            proc.start()
            child.close()
            procs.append((proc, parent))
        for proc, parent in procs:
            try:
                results.append(_finish(proc, parent, timeout, startup_budget))
            finally:
                parent.close()
                _kill(proc)
    finally:
        for proc, parent in procs:
            _kill(proc)
            try:
                parent.close()
            except Exception:
                pass
    return results[0], results[1]
