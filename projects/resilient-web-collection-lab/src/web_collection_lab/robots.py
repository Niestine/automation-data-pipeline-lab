"""robots.txt parser with longest-match Allow/Disallow and Crawl-delay.

The parser is intentionally small: it understands User-agent groups,
Allow, Disallow, Crawl-delay, and comments. Sitemap and unknown fields
are ignored. Matching follows RFC 9309: the crawler's product token is
matched exactly (case-insensitive), a specific User-agent group wins over
`*`, and the longest matching path rule wins, with Allow beating Disallow
on a tie.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
import re

from .models import MAX_CRAWL_DELAY_MS, USER_AGENT


_UA_PRODUCT = re.compile(r"^[A-Za-z0-9._-]+")


@dataclass(frozen=True)
class RobotsRule:
    allow: bool
    path: str


@dataclass
class RobotsGroup:
    agents: list[str] = field(default_factory=list)
    rules: list[RobotsRule] = field(default_factory=list)
    crawl_delay_s: Optional[int] = None


@dataclass
class RobotsPolicy:
    groups: list[RobotsGroup]
    user_agent: str
    matched_group: Optional[RobotsGroup]

    @property
    def crawl_delay_ms(self) -> int:
        if self.matched_group is None or self.matched_group.crawl_delay_s is None:
            return 0
        ms = int(self.matched_group.crawl_delay_s) * 1000
        return min(MAX_CRAWL_DELAY_MS, max(0, ms))

    def allowed(self, path: str) -> bool:
        path = path.split("?", 1)[0] or "/"
        if not path.startswith("/"):
            path = "/" + path
        group = self.matched_group
        if group is None or not group.rules:
            return True
        best: Optional[RobotsRule] = None
        for rule in group.rules:
            if _path_prefix_match(rule.path, path):
                if best is None or len(rule.path) > len(best.path):
                    best = rule
                elif len(rule.path) == len(best.path) and rule.allow and not best.allow:
                    best = rule
        if best is None:
            return True
        return best.allow


def product_token(user_agent: str) -> str:
    text = (user_agent or "").strip()
    match = _UA_PRODUCT.match(text)
    return match.group(0) if match else text


def parse_robots(text: str, user_agent: str = USER_AGENT) -> RobotsPolicy:
    groups = _parse_groups(text)
    matched = _select_group(groups, product_token(user_agent))
    return RobotsPolicy(groups=groups, user_agent=user_agent, matched_group=matched)


def _parse_groups(text: str) -> list[RobotsGroup]:
    groups: list[RobotsGroup] = []
    current = RobotsGroup()
    started = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            continue
        field, value = line.split(":", 1)
        field = field.strip().lower()
        value = value.strip()
        if field == "user-agent":
            agent = value.lower()
            if not agent:
                continue
            if started and current.rules:
                groups.append(current)
                current = RobotsGroup()
                started = False
            current.agents.append(agent)
            started = True
            continue
        if not started:
            continue
        if field == "disallow":
            current.rules.append(RobotsRule(allow=False, path=_normalize_rule_path(value)))
        elif field == "allow":
            current.rules.append(RobotsRule(allow=True, path=_normalize_rule_path(value)))
        elif field == "crawl-delay":
            delay = _parse_delay(value)
            if delay is not None:
                current.crawl_delay_s = delay
    if started:
        groups.append(current)
    return groups


def _normalize_rule_path(value: str) -> str:
    # Empty Disallow means "allow all" and is stored as an empty path that
    # never prefix-matches, so allowed() falls through to True.
    if value == "":
        return ""
    if not value.startswith("/"):
        value = "/" + value
    return value


def _parse_delay(value: str) -> Optional[int]:
    if re.fullmatch(r"[0-9]{1,8}", value.strip()) is None:
        return None
    return int(value.strip())


def _select_group(groups: list[RobotsGroup], token: str) -> Optional[RobotsGroup]:
    # RFC 9309: match the product token case-insensitively and exactly, so a
    # "LabCollectorX" or "L" group does not apply to "LabCollector". Several
    # groups naming the same agent are merged into one.
    token = token.lower()
    specific = [group for group in groups if token and token in group.agents]
    if specific:
        return _merge_groups(specific)
    wildcard = [group for group in groups if "*" in group.agents]
    if wildcard:
        return _merge_groups(wildcard)
    return None


def _merge_groups(groups: list[RobotsGroup]) -> RobotsGroup:
    if len(groups) == 1:
        return groups[0]
    merged = RobotsGroup()
    for group in groups:
        merged.agents.extend(agent for agent in group.agents if agent not in merged.agents)
        merged.rules.extend(group.rules)
        if group.crawl_delay_s is not None:
            current = merged.crawl_delay_s
            merged.crawl_delay_s = group.crawl_delay_s if current is None else max(current, group.crawl_delay_s)
    return merged


def _path_prefix_match(rule_path: str, path: str) -> bool:
    if rule_path == "":
        return False
    if rule_path == "/":
        return True
    if path == rule_path:
        return True
    if path.startswith(rule_path):
        if rule_path.endswith("/"):
            return True
        rest = path[len(rule_path) :]
        return rest.startswith("/")
    return False
