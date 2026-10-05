"""Robots Exclusion Protocol matching for one product token.

Longest path match wins. An equal-length Allow wins over Disallow. A 4xx
robots response is allow-all; a 5xx, timeout, or unfinished redirect chain is
disallow-all for the run. Crawl-delay is not a rule in this protocol.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from datetime import timezone


_UNRESERVED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


@dataclass(frozen=True)
class Group:
    agents: tuple[str, ...]
    rules: tuple[tuple[str, str], ...]


def decode_unreserved(value: str) -> str:
    """Decode percent-encoded unreserved octets. Reserved octets stay encoded."""

    def replace(match: re.Match[str]) -> str:
        octet = int(match.group(1), 16)
        if octet > 127:
            return match.group(0).upper()
        char = chr(octet)
        if char in _UNRESERVED:
            return char
        return "%" + match.group(1).upper()

    return re.sub(r"%([0-9A-Fa-f]{2})", replace, value)


def parse_robots(text: str, limit: int = 500 * 1024) -> list[Group]:
    raw = text.encode("utf-8")[:limit]
    text = raw.decode("utf-8", errors="ignore")
    groups: list[Group] = []
    agents: list[str] = []
    rules: list[tuple[str, str]] = []

    def flush() -> None:
        nonlocal agents, rules
        if agents:
            groups.append(Group(tuple(agents), tuple(rules)))
        agents = []
        rules = []

    for line in text.splitlines():
        if "#" in line:
            line = line[: line.index("#")]
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            if rules:
                flush()
            agents.append(value)
        elif key in ("allow", "disallow"):
            if not agents:
                continue
            rules.append((key, value))
    flush()
    return groups


def select_rules(groups: list[Group], product_token: str) -> list[tuple[str, str]]:
    """Merge every group whose product token matches. Fall back to '*' only when none do."""
    token = product_token.casefold()
    matched = [
        group
        for group in groups
        if any(agent.split("/")[0].strip().casefold() == token for agent in group.agents)
    ]
    if not matched:
        matched = [
            group for group in groups if any(agent.strip() == "*" for agent in group.agents)
        ]
    rules: list[tuple[str, str]] = []
    for group in matched:
        rules.extend(group.rules)
    return rules


def rule_matches(pattern: str, target: str) -> bool:
    anchor = pattern.endswith("$")
    body = pattern[:-1] if anchor else pattern
    prev = [False] * (len(target) + 1)
    prev[0] = True
    for char in body:
        current = [False] * (len(target) + 1)
        if char == "*":
            seen = False
            for index, flag in enumerate(prev):
                if flag:
                    seen = True
                current[index] = seen
        else:
            for index in range(len(target)):
                if prev[index] and target[index] == char:
                    current[index + 1] = True
        prev = current
    if anchor:
        return prev[-1]
    return any(prev)


def specificity(pattern: str) -> int:
    if pattern.endswith("$"):
        return len(pattern) - 1
    return len(pattern)


def is_allowed(path: str, rules: list[tuple[str, str]]) -> bool:
    """Default allow. Empty patterns add no constraint. Equal lengths prefer Allow."""
    target = decode_unreserved(path or "/")
    best = -1
    decision = True
    for kind, pattern in rules:
        normalized = decode_unreserved(pattern)
        if normalized in ("", "$"):
            continue
        if not rule_matches(normalized, target):
            continue
        length = specificity(normalized)
        allow = kind == "allow"
        if length > best:
            best = length
            decision = allow
        elif length == best and allow:
            decision = True
    return decision


def cache_lifetime(headers: dict[str, str], now: float, default: float) -> float:
    """Seconds the robots response may be reused. max-age wins over Expires.

    An Expires value that does not parse as an HTTP-date is treated as already
    expired, so the next run fetches robots.txt again.
    """
    lowered = {key.lower(): value for key, value in headers.items()}
    cache_control = lowered.get("cache-control", "")
    if re.search(r"(?:^|,)\s*no-cache\b", cache_control, re.IGNORECASE) or re.search(
        r"(?:^|,)\s*no-store\b", cache_control, re.IGNORECASE
    ):
        return 0.0
    max_age = re.search(r"max-age=(\d+)", cache_control, re.IGNORECASE)
    if max_age:
        return float(max_age.group(1))
    expires = lowered.get("expires")
    if expires is not None:
        try:
            parsed = parsedate_to_datetime(expires)
        except (TypeError, ValueError):
            # An unparseable Expires, such as "0", means already expired.
            return 0.0
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, parsed.timestamp() - now)
    return float(default)
