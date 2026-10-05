"""Run configuration. Defaults match the lab's stated controls."""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path

from incremental_crawl_lab.canon import host_of
from incremental_crawl_lab.errors import ConfigError

OBJECTIVES = ("freshness", "age", "uniform", "proportional")


@dataclass
class Config:
    user_agent: str = (
        "Mozilla/5.0 (compatible; FrontierBot/0.2; "
        "+https://collection.example.invalid/frontier)"
    )
    product_token: str = "FrontierBot"
    accept: str = "text/html, application/xhtml+xml;q=0.9"
    accept_language: str = "en"
    min_host_interval_seconds: float = 10.0
    page_budget: int = 8
    retry_budget: int = 8
    objective: str = "freshness"
    backoff_base_seconds: float = 1.0
    backoff_cap_seconds: float = 300.0
    retry_after_defer_seconds: float = 3600.0
    collection_capacity: int = 50
    simhash_k: int | None = None
    material_detection: bool = False
    horizon_seconds: float = 7.0 * 24.0 * 3600.0
    robots_default_ttl_seconds: float = 24.0 * 3600.0
    allowlist: tuple[str, ...] = ("catalog.example.invalid",)
    origin: str = "https://catalog.example.invalid"
    max_robots_redirects: int = 5
    max_robots_bytes: int = 500 * 1024
    seed: int = 20261005
    sample_interval_seconds: float | None = None
    soft_sha256: tuple[str, ...] = ()
    soft_simhashes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.objective not in OBJECTIVES:
            raise ConfigError(f"unknown objective {self.objective}")
        if self.page_budget < 1:
            raise ConfigError("page_budget must be >= 1")
        if self.retry_budget < 0:
            raise ConfigError("retry_budget must be >= 0")
        if self.collection_capacity < 1:
            raise ConfigError("collection_capacity must be >= 1")
        if self.min_host_interval_seconds < 0:
            raise ConfigError("min_host_interval_seconds must be >= 0")
        if self.simhash_k is not None and self.simhash_k < 0:
            raise ConfigError("simhash_k must be >= 0")
        if self.sample_interval_seconds is None:
            self.sample_interval_seconds = self.horizon_seconds
        hosts = tuple(h.lower() for h in self.allowlist)
        self.allowlist = hosts
        self.soft_sha256 = tuple(self.soft_sha256)
        self.soft_simhashes = tuple(self.soft_simhashes)
        if host_of(self.origin) not in self.allowlist:
            raise ConfigError("origin host is not in the allowlist")

    def audit_slots(self, budget: int) -> int:
        if budget <= 0:
            return 0
        return min(budget, max(1, budget // 10))


def load_config(path: str | Path) -> Config:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError("config root must be an object")
    known = {item.name for item in fields(Config)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigError("unknown config keys: " + ", ".join(unknown))
    if "allowlist" in raw:
        raw["allowlist"] = tuple(raw["allowlist"])
    return Config(**raw)
