"""One signature line per decode or profile failure. Payloads are not logged."""

from __future__ import annotations

import logging

LOGGER = logging.getLogger("netunicode_lab")
LOGGER.addHandler(logging.NullHandler())


def log_boundary(
    boundary: str,
    codec: str,
    *,
    bad_byte: int | None = None,
    unidata_version: str | None = None,
) -> None:
    parts = [f"boundary={boundary}", f"codec={codec}"]
    if bad_byte is not None:
        parts.append(f"byte=0x{bad_byte:02x}")
    if unidata_version is not None:
        parts.append(f"unidata_version={unidata_version}")
    LOGGER.warning(" ".join(parts))
