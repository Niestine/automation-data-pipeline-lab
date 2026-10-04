"""Path bootstrap and factories for unittest modules."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

EXAMPLES = ROOT / "examples"

from automation_job_lab.checkpoint import FileCheckpointStore, MemoryCheckpointStore  # noqa: E402
from automation_job_lab.handlers import Fault, FaultInjector  # noqa: E402
from automation_job_lab.ledger import ExecutionLedger  # noqa: E402
from automation_job_lab.lease import LeaseStore  # noqa: E402
from automation_job_lab.models import LAB_EPOCH_MS, Catalog  # noqa: E402
from automation_job_lab.runner import JobRunner  # noqa: E402
from automation_job_lab.schema import load_catalog  # noqa: E402
from automation_job_lab.seed import DEFAULT_RETRY, build_catalog, build_inbox  # noqa: E402
from automation_job_lab.store import Workspace  # noqa: E402
from automation_job_lab.telemetry import JsonLogger, ManualClock, RecordingSleeper  # noqa: E402


def retry_dict() -> dict[str, Any]:
    return dict(DEFAULT_RETRY)


def job_dict(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "heartbeat-log",
        "handler": "heartbeat",
        "schedule": {},
        "retry": retry_dict(),
        "timeout_ms": 1000,
        "depends_on": [],
        "idempotency": {"mode": "window"},
        "params": {},
    }
    payload.update(overrides)
    return payload


def catalog_dict(*jobs: dict[str, Any], pipeline: str = "hourly-ops", window_ms: int = 3_600_000) -> dict[str, Any]:
    return {"pipeline": pipeline, "window_ms": window_ms, "jobs": list(jobs)}


def make_runner(
    *,
    catalog: Optional[Catalog | dict[str, Any]] = None,
    inbox: Optional[list[dict[str, Any]]] = None,
    faults: Optional[list[dict[str, Any] | Fault]] = None,
    crash_after_jobs: Optional[int] = None,
    crash_at: str = "post_handler",
    fail_fast: bool = False,
    state_dir: Optional[str | Path] = None,
    now_ms: int = LAB_EPOCH_MS,
    seed_inbox: bool = True,
):
    if isinstance(catalog, Catalog):
        catalog_obj = catalog
    else:
        catalog_obj = load_catalog(catalog if catalog is not None else build_catalog())
    clock = ManualClock(now_ms)
    logger = JsonLogger(clock=clock)
    sleeper = RecordingSleeper(clock)
    if state_dir is not None:
        root = Path(state_dir)
        workspace = Workspace(root / "workspace.json")
        ledger = ExecutionLedger(root / "ledger.json")
        leases = LeaseStore(root / "leases.json")
        store: Any = FileCheckpointStore(root / "checkpoints")
    else:
        workspace = Workspace()
        ledger = ExecutionLedger()
        leases = LeaseStore()
        store = MemoryCheckpointStore()
    if seed_inbox:
        rows = inbox if inbox is not None else build_inbox(window=0)
        for row in rows:
            workspace.upsert("inbox", row)
    built_faults: list[Fault] = []
    for item in faults or []:
        if isinstance(item, Fault):
            built_faults.append(item)
        else:
            built_faults.append(Fault.from_dict(item))
    runner = JobRunner(
        catalog_obj,
        workspace=workspace,
        ledger=ledger,
        store=store,
        leases=leases,
        logger=logger,
        clock=clock,
        sleeper=sleeper,
        faults=FaultInjector(built_faults),
        seed=7,
        crash_after_jobs=crash_after_jobs,
        crash_at=crash_at,
        fail_fast=fail_fast,
    )
    return runner, workspace, sleeper, logger, clock, ledger, leases
