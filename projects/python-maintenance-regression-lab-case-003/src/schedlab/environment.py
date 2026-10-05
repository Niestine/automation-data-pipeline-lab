"""Interpreter stamp recorded on every schedule artifact.

PEP 703 makes the GIL a build and runtime choice. The lab stores the raw
probe values and does not branch on mode codes.
"""

from __future__ import annotations

import os
import sys
import sysconfig
from typing import Any, Optional

_CACHE: Optional[dict[str, Any]] = None


def _read() -> dict[str, Any]:
    stamp: dict[str, Any] = {
        "Py_GIL_DISABLED": sysconfig.get_config_var("Py_GIL_DISABLED"),
        "PYTHON_GIL": os.environ.get("PYTHON_GIL"),
    }
    probe = getattr(sys, "_is_gil_enabled", None)
    if probe is None:
        stamp["gil_enabled"] = None
        stamp["gil_probe"] = "unavailable"
    else:
        stamp["gil_enabled"] = bool(probe())
    return stamp


def probe() -> dict[str, Any]:
    """Return the process GIL stamp. The first call reads; later calls copy it."""

    global _CACHE
    if _CACHE is None:
        _CACHE = _read()
    return dict(_CACHE)


def python_version() -> str:
    return sys.version
