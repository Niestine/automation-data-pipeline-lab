"""Diagnostic phase log. Recovery never reads this file."""

from __future__ import annotations

import logging
import os
from pathlib import Path

LOGGER_NAME = "recovery_lab"
FIELDS = (
    "phase",
    "commit_id",
    "lsn",
    "schema_version",
    "element",
    "element_state",
    "path",
    "byte_count",
    "outcome",
)


class AuditHandler(logging.Handler):
    """Append one line per emit and close the file afterward.

    The handler is installed by ``LedgerStore`` or by the fault harness.
    ``recover`` only calls the logger; it does not open ``audit.log``.
    A record tagged with a data directory is written only by the handler
    for that directory, so two open ledgers do not share an audit trail.
    """

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = Path(path)
        self.root = _key(self.path.parent)
        self.users = 0

    def emit(self, record: logging.LogRecord) -> None:
        tagged = getattr(record, "audit_root", None)
        if tagged is not None and tagged != self.root:
            return
        try:
            message = self.format(record)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(message + "\n")
        except Exception:
            self.handleError(record)


def _key(directory: Path | str) -> str:
    return os.path.normcase(str(Path(directory).resolve()))


def logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def attach(directory: Path) -> AuditHandler:
    """Install (or reuse) the audit handler for ``directory``.

    A second ``attach`` for the same directory shares the first handler, so
    two stores on one directory do not write every line twice.
    """
    log = logger()
    path = Path(directory) / "audit.log"
    for existing in log.handlers:
        if isinstance(existing, AuditHandler) and existing.root == _key(directory):
            existing.users += 1
            return existing
    handler = AuditHandler(path)
    handler.users = 1
    handler.setFormatter(logging.Formatter(" ".join(f"{name}=%({name})s" for name in FIELDS)))
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    log.propagate = False
    return handler


def detach(handler: logging.Handler | None) -> None:
    if handler is None:
        return
    if isinstance(handler, AuditHandler):
        handler.users -= 1
        if handler.users > 0:
            return
    log = logger()
    log.removeHandler(handler)
    handler.close()


def emit(phase: str, *, root: Path | str | None = None, **fields: object) -> None:
    """Log one phase. ``root`` is the data directory the phase belongs to."""
    log = logger()
    if not log.handlers:
        return
    extra: dict[str, object] = {name: "-" for name in FIELDS}
    extra["phase"] = phase
    for key, value in fields.items():
        if key in extra and value is not None:
            extra[key] = value
    extra["audit_root"] = None if root is None else _key(root)
    log.info("recovery", extra=extra)
