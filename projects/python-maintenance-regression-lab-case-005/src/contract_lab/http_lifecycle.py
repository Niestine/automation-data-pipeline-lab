"""Deprecation and Sunset parsers.

``Deprecation`` is only an Item structured-field date: ``@`` plus an integer
Unix second, UTC (RFC 9745, citing RFC 9651 section 3.3.7). ``Sunset`` is an
IMF-fixdate with the ``GMT`` token, plus the ``UTC`` token used in the RFC 9745
combined example. A value that parses under one grammar fails the other.

RFC 8594's example names Saturday for 31 December 2018, which was a Monday.
The calendar date is kept and the weekday token is not "corrected".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from contract_lab.errors import HeaderGrammarError

# RFC 9651 sf-integer: optional minus and at most 15 digits.
_DEPRECATION = re.compile(r"^@-?[0-9]{1,15}$")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_SUNSET = re.compile(
    r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun), "
    r"([0-9]{2}) "
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"([0-9]{4}) "
    r"([0-9]{2}):([0-9]{2}):([0-9]{2}) "
    r"(GMT|UTC)$"
)
_MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}


def format_instant(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_instant(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError(f"instant requires a timezone: {value}")
    return parsed.astimezone(timezone.utc)


def parse_deprecation(value: str) -> datetime:
    if not isinstance(value, str) or _DEPRECATION.fullmatch(value) is None:
        raise HeaderGrammarError("Deprecation", "" if value is None else str(value))
    # Epoch arithmetic instead of fromtimestamp: negative seconds work on every
    # platform, and an instant outside datetime's range is a grammar failure,
    # not a raw OverflowError.
    try:
        return _EPOCH + timedelta(seconds=int(value[1:]))
    except OverflowError as exc:
        raise HeaderGrammarError("Deprecation", value) from exc


def deprecation_phase(instant: datetime, now: datetime) -> str:
    if instant > now:
        return "scheduled"
    return "already_deprecated"


def parse_sunset(value: str) -> datetime:
    if not isinstance(value, str):
        raise HeaderGrammarError("Sunset", str(value))
    match = _SUNSET.fullmatch(value)
    if match is None:
        raise HeaderGrammarError("Sunset", value)
    _day_name, day, month, year, hour, minute, second, _zone = match.groups()
    try:
        return datetime(
            int(year),
            _MONTHS[month],
            int(day),
            int(hour),
            int(minute),
            int(second),
            tzinfo=timezone.utc,
        )
    except ValueError as exc:
        raise HeaderGrammarError("Sunset", value) from exc


def sunset_phase(instant: datetime, now: datetime) -> str:
    """A past instant is treated as the present. An equal instant has arrived."""

    if instant > now:
        return "announced"
    return "elapsed"


def clock_order_fault(deprecation_at: datetime | None, sunset_at: datetime | None) -> bool:
    if deprecation_at is None or sunset_at is None:
        return False
    return sunset_at < deprecation_at


def _split_outside(value: str, separator: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    angle = 0
    quote = False
    for char in value:
        if char == '"' and angle == 0:
            quote = not quote
            buf.append(char)
            continue
        if not quote and char == "<":
            angle += 1
            buf.append(char)
            continue
        if not quote and char == ">" and angle:
            angle -= 1
            buf.append(char)
            continue
        if char == separator and not quote and angle == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(char)
    parts.append("".join(buf))
    return parts


def parse_link(value: str) -> list[dict[str, str | None]]:
    """Read ``rel`` and the target. The target is not fetched."""

    if not isinstance(value, str) or not value.strip():
        raise HeaderGrammarError("Link", "" if value is None else str(value))
    links: list[dict[str, str | None]] = []
    for raw in _split_outside(value, ","):
        part = raw.strip()
        if not part:
            continue
        if not part.startswith("<") or ">" not in part:
            raise HeaderGrammarError("Link", value)
        end = part.find(">")
        target = part[1:end]
        params: dict[str, str] = {}
        for param in _split_outside(part[end + 1 :], ";"):
            token = param.strip()
            if not token:
                continue
            if "=" not in token:
                raise HeaderGrammarError("Link", value)
            key, raw_val = token.split("=", 1)
            key = key.strip().lower()
            raw_val = raw_val.strip()
            if len(raw_val) >= 2 and raw_val[0] == '"' and raw_val[-1] == '"':
                raw_val = raw_val[1:-1]
            params[key] = raw_val
        links.append(
            {"target": target, "rel": params.get("rel"), "type": params.get("type")}
        )
    if not links:
        raise HeaderGrammarError("Link", value)
    return links


def deprecation_targets(links: list[dict[str, str | None]]) -> list[str]:
    targets: list[str] = []
    for link in links:
        rel = link.get("rel") or ""
        if "deprecation" in str(rel).split():
            target = link.get("target")
            if target:
                targets.append(target)
    return targets


def header_value(headers: dict[str, str] | None, name: str) -> str | None:
    if not headers:
        return None
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


@dataclass
class HeaderFacts:
    deprecation_at: datetime | None = None
    sunset_at: datetime | None = None
    phase: str = "not_deprecated"
    sunset_phase: str | None = None
    policy_targets: list[str] = field(default_factory=list)
    clock_order_error: bool = False


def interpret_headers(headers: dict[str, str] | None, now: datetime) -> HeaderFacts:
    """Parse headers that are present. Absence is not an error."""

    facts = HeaderFacts()
    raw_deprecation = header_value(headers, "Deprecation")
    raw_sunset = header_value(headers, "Sunset")
    raw_link = header_value(headers, "Link")
    if raw_deprecation is not None:
        facts.deprecation_at = parse_deprecation(raw_deprecation)
        facts.phase = deprecation_phase(facts.deprecation_at, now)
    if raw_sunset is not None:
        facts.sunset_at = parse_sunset(raw_sunset)
        facts.sunset_phase = sunset_phase(facts.sunset_at, now)
    if raw_link is not None:
        facts.policy_targets = deprecation_targets(parse_link(raw_link))
    facts.clock_order_error = clock_order_fault(facts.deprecation_at, facts.sunset_at)
    return facts
