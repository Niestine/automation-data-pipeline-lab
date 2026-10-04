"""UTC ISO-week windows and timestamp parsing.

Naive ``datetime.fromtimestamp`` uses the host timezone and can shift a
Sunday UTC instant into Monday locally (BUG-004). Every conversion in this
module is timezone-aware UTC and the week is ISO-8601 Monday 00:00Z
half-open ``[start, end)``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re

from .errors import ValidationError
from .models import ISO_DATE, ISO_Z, WEEK_ID_PATTERN

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_WEEK_RE = re.compile(WEEK_ID_PATTERN)
_DATE_RE = re.compile(ISO_DATE)
_Z_RE = re.compile(ISO_Z)


def utc_from_ms(ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=int(ms))


def ms_from_utc(dt: datetime) -> int:
    if dt.tzinfo is None:
        raise ValidationError("naive datetime is not allowed")
    aware = dt.astimezone(timezone.utc)
    delta = aware - _EPOCH
    return delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000


def iso_week_id(ms: int) -> str:
    iso = utc_from_ms(ms).isocalendar()
    return f"{iso.year:04d}-W{iso.week:02d}"


def iso_week_bounds(week_id: str) -> tuple[int, int]:
    if _WEEK_RE.fullmatch(week_id) is None:
        raise ValidationError(f"invalid week id {week_id!r}")
    year = int(week_id[:4])
    week = int(week_id[6:])
    start = datetime.fromisocalendar(year, week, 1).replace(tzinfo=timezone.utc)
    end = start + timedelta(days=7)
    return ms_from_utc(start), ms_from_utc(end)


def contains(week_id: str, ms: int) -> bool:
    start, end = iso_week_bounds(week_id)
    return start <= int(ms) < end


def parse_iso_datetime(raw: str) -> int:
    """Parse ISO-8601 dates only. Slash dates are rejected (BUG-015)."""
    text = raw.strip()
    if not text:
        raise ValidationError("empty updated_at", field="updated_at")
    if "/" in text:
        raise ValidationError(
            "updated_at must be ISO-8601 (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ)",
            field="updated_at",
        )
    if _Z_RE.fullmatch(text):
        dt = datetime(
            int(text[0:4]),
            int(text[5:7]),
            int(text[8:10]),
            int(text[11:13]),
            int(text[14:16]),
            int(text[17:19]),
            tzinfo=timezone.utc,
        )
        return ms_from_utc(dt)
    if _DATE_RE.fullmatch(text):
        dt = datetime(int(text[0:4]), int(text[5:7]), int(text[8:10]), tzinfo=timezone.utc)
        return ms_from_utc(dt)
    raise ValidationError(
        "updated_at must be ISO-8601 (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ)",
        field="updated_at",
    )
