"""Path bootstrap and small factories for unittest discovery."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

EXAMPLES = ROOT / "examples"

from incremental_crawl_lab.clock import ManualClock  # noqa: E402
from incremental_crawl_lab.config import Config  # noqa: E402
from incremental_crawl_lab.crawl import Crawler  # noqa: E402
from incremental_crawl_lab.fixture import FixtureOrigin, Page  # noqa: E402
from incremental_crawl_lab.store import Store  # noqa: E402


def make_lab(
    pages: list[Page],
    robots: str,
    config: Config | None = None,
    *,
    db_path: str | None = None,
    seed_path: str = "/catalog/",
):
    cfg = config if config is not None else Config()
    origin = FixtureOrigin(cfg.origin, robots, {"Content-Type": "text/plain", "Cache-Control": "max-age=86400"})
    origin.seed_path = seed_path
    for page in pages:
        origin.add_page(page)
    if db_path:
        store = Store.open(db_path, durable=True)
    else:
        store = Store.open(None, durable=False)
    clock = ManualClock()
    crawler = Crawler(cfg, store, origin, clock)
    return crawler, origin, store, clock
