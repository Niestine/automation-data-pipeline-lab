"""Path bootstrap and desk factories. Synthetic notices only."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
EXAMPLES = ROOT / "examples"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bay_notice.budget import SharedBudget  # noqa: E402
from bay_notice.client import BUILDS, Desk  # noqa: E402
from bay_notice.inject import Gateway, Job, Step  # noqa: E402
from bay_notice.log import LOGGER  # noqa: E402
from bay_notice.policy import RetryPolicy  # noqa: E402
from bay_notice.timer import RtoEstimator  # noqa: E402


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def capture_logs() -> Capture:
    LOGGER.handlers.clear()
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    handler = Capture()
    LOGGER.addHandler(handler)
    return handler


def release_logs() -> None:
    LOGGER.handlers.clear()
    LOGGER.addHandler(logging.NullHandler())
    LOGGER.setLevel(logging.NOTSET)
    LOGGER.propagate = True


def sample_job(operation_id: str = "BAY-1001") -> Job:
    return Job(operation_id, "C-14", "HOLD C-14 40min", "18.00")


def errors(count: int, name: str = "transient", partial: str = "") -> list[Step]:
    return [Step(kind="error", error_name=name, partial=partial) for _ in range(count)]


def make_desk(
    build: str = "repaired",
    steps: list[Step] | None = None,
    max_retries: int = 3,
    floor: float = 1.0,
    ceiling: float = 6.0,
    granularity: float = 0.01,
    budget: SharedBudget | None = None,
    sampler: object | None = None,
    template: str | None = None,
    jitter: str = "fixed",
    seed: int = 0,
    spread: float = 0.25,
    reset_after: int | None = None,
) -> Desk:
    policy = RetryPolicy(
        max_retries=max_retries,
        floor_s=floor,
        ceiling_s=ceiling,
        granularity_s=granularity,
        jitter=jitter,
        seed=seed,
        spread=spread,
        reset_after=reset_after,
    )
    desk = Desk(
        policy=policy,
        gateway=Gateway(steps),
        timer=RtoEstimator.from_policy(policy),
        budget=budget,
        defects=BUILDS[build],
        sampler=sampler,
    )
    if template is not None:
        desk.template = template
    return desk
