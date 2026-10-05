"""File DEBUG trace and console INFO summary.

Named loggers propagate to handlers installed for the campaign. The console
handler's INFO threshold is what keeps per-step lines off the console.
Each run gets its own file, so a later seed does not truncate the failing trace.
The artifact directory is caller-supplied. This module has no default log path.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional, TextIO

SCHEDULE_LOGGER = "schedlab.schedule"
CAMPAIGN_LOGGER = "schedlab.campaign"

_FILE_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"
_CONSOLE_FORMAT = "%(name)s %(levelname)s %(message)s"


class CampaignLogs:
    """Install the campaign handlers and remove them on close."""

    def __init__(self, console_stream: Optional[TextIO] = None) -> None:
        self.console_stream: TextIO = console_stream if console_stream is not None else sys.stderr
        self._root = logging.getLogger()
        self._saved_level = self._root.level
        self._saved_loggers: dict[str, tuple[int, bool]] = {}
        self._console: Optional[logging.Handler] = None
        self._install()

    def _install(self) -> None:
        self._root.setLevel(logging.DEBUG)
        for name in (SCHEDULE_LOGGER, CAMPAIGN_LOGGER, "schedlab"):
            logger = logging.getLogger(name)
            self._saved_loggers[name] = (logger.level, logger.propagate)
            logger.setLevel(logging.DEBUG)
            logger.propagate = True
        handler = logging.StreamHandler(self.console_stream)
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
        handler._schedlab = True  # type: ignore[attr-defined]
        self._root.addHandler(handler)
        self._console = handler

    def open_run(self, log_path: Path) -> logging.Handler:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter(_FILE_FORMAT))
        handler._schedlab = True  # type: ignore[attr-defined]
        self._root.addHandler(handler)
        return handler

    def close_run(self, handler: logging.Handler) -> None:
        self._root.removeHandler(handler)
        handler.flush()
        handler.close()

    def emit(self, log_path: Path, notes: list[str], summary: str) -> None:
        handler = self.open_run(log_path)
        try:
            schedule = logging.getLogger(SCHEDULE_LOGGER)
            campaign = logging.getLogger(CAMPAIGN_LOGGER)
            for note in notes:
                schedule.debug(note)
            campaign.info(summary)
        finally:
            self.close_run(handler)

    def close(self) -> None:
        if self._console is not None:
            self._root.removeHandler(self._console)
            self._console.flush()
            self._console.close()
            self._console = None
        self._root.setLevel(self._saved_level)
        for name, (level, propagate) in self._saved_loggers.items():
            logger = logging.getLogger(name)
            logger.setLevel(level)
            logger.propagate = propagate
        self._saved_loggers = {}


def summary_line(
    *,
    policy: str,
    seed: int,
    terminal: str,
    steps: int,
    schedules_used: int,
) -> str:
    return (
        f"policy={policy} seed={seed} terminal={terminal} "
        f"steps={steps} schedules_used={schedules_used}"
    )


def step_line(*, step: int, tid: int, enabled: list[int], reason: str) -> str:
    rendered = ",".join(str(item) for item in enabled) if enabled else "-"
    return f"step={step} tid={tid} enabled={rendered} reason={reason}"
