"""Logger for the lab. Recovery does not read this stream back."""

from __future__ import annotations

import logging
import sys


def configure(level: int = logging.INFO, stream=None) -> logging.Logger:
    log = logging.getLogger("prefixlab")
    log.setLevel(level)
    if not any(isinstance(handler, logging.StreamHandler) for handler in log.handlers):
        handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        log.addHandler(handler)
    return log
