"""Level-split logging.

Stdout keeps INFO and WARNING. A filter drops ERROR and above so a
threshold of INFO does not also receive them. Stderr starts at ERROR.
DEBUG stays on the run file.
"""

from __future__ import annotations

import logging
import logging.config
from pathlib import Path

from slip_lab.errors import LogConfigError

_SAVED_LEVEL: int | None = None


class MaxLevelFilter(logging.Filter):
    """Keep records at or below a ceiling. This is the cookbook upper bound."""

    def __init__(self, level: int | str = logging.WARNING, name: str = "") -> None:
        super().__init__(name)
        if isinstance(level, str):
            level = getattr(logging, level)
        self.max_level = int(level)

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self.max_level


def _clear_root() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        handler.flush()
        handler.close()
        root.removeHandler(handler)


def configure(log_dir: Path, run_id: str) -> Path:
    global _SAVED_LEVEL
    directory = Path(log_dir)
    if directory.exists() and not directory.is_dir():
        raise LogConfigError(f"log directory is not a directory: {directory}")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise LogConfigError(f"log directory is not writable: {directory}") from exc
    path = directory / f"slip-{run_id}.log"
    _SAVED_LEVEL = logging.getLogger().level
    _clear_root()
    config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "plain": {"format": "%(levelname)-8s %(name)s %(message)s"},
        },
        "filters": {
            "warnings_and_below": {
                "()": "slip_lab.logging_setup.MaxLevelFilter",
                "level": "WARNING",
            }
        },
        "handlers": {
            "file": {
                "class": "logging.FileHandler",
                "level": "DEBUG",
                "formatter": "plain",
                "filename": str(path),
                "mode": "w",
                "encoding": "utf-8",
            },
            "stdout": {
                "class": "logging.StreamHandler",
                "level": "INFO",
                "formatter": "plain",
                "stream": "ext://sys.stdout",
                "filters": ["warnings_and_below"],
            },
            "stderr": {
                "class": "logging.StreamHandler",
                "level": "ERROR",
                "formatter": "plain",
                "stream": "ext://sys.stderr",
            },
        },
        "root": {
            "level": "DEBUG",
            "handlers": ["file", "stdout", "stderr"],
        },
    }
    logging.config.dictConfig(config)
    return path


def teardown() -> None:
    global _SAVED_LEVEL
    _clear_root()
    if _SAVED_LEVEL is not None:
        logging.getLogger().setLevel(_SAVED_LEVEL)
        _SAVED_LEVEL = None


def log_case(
    logger: logging.Logger,
    *,
    case_id: str,
    outcome: str,
    side: str | None,
    disposition: str | None,
    byte_length: int,
    digest: str,
    parent_ids: list[str],
    truncated: bool,
    exc_type: str | None = None,
) -> None:
    side_text = side or "-"
    label = disposition or "-"
    logger.info(
        "case=%s outcome=%s side=%s disposition=%s",
        case_id,
        outcome,
        side_text,
        label,
    )
    logger.debug(
        "bytes=%s sha256=%s parents=%s truncated=%s",
        byte_length,
        digest,
        ",".join(parent_ids),
        int(bool(truncated)),
    )
    if exc_type:
        logger.error("exc_type=%s side=%s", exc_type, side_text)
