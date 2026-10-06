"""Fixture-scale goodput comparison for a token-endpoint brownout.

The model is deliberately small. Each tick, every client whose access token
has expired may send one token-endpoint call. The endpoint serves at most
``capacity`` calls per tick. When more calls arrive than it can serve and
nothing sheds them, the queue pushes every call past the client timeout, so
the work is wasted and no call succeeds. That is the overload shape Bronson
et al. describe, reduced to one rule.

Both arms share the steady state: tokens expire on staggered ticks, so the
offered load sits below capacity and the system is stable. The trigger is a
brownout during which every call times out. Expired clients pile up while
it lasts. The arms differ only after that:

- ``unbounded``: a timed-out client retries on the next tick, forever, and
  nothing sheds. The backlog alone keeps offered load above capacity.
- ``budgeted``: three pre-commit attempts with full jitter between them,
  then a resume delay drawn from [0.5T, 1.5T]; the endpoint serves
  ``capacity`` calls and rejects the excess with a cheap 503.

``shed`` can be set independently of the client policy so the tests can
ablate the two. In this model the shed gate is what lets goodput return;
the budget and phase draw alone do not settle, and their measurable effect
is fewer token-endpoint attempts during recovery.

A small run does not prove a large deployment is free of metastable
failure, because the strength of the feedback loop changes with scale.
"""

from __future__ import annotations

import math
import random

from lotcycle.params import PRECOMMIT_BUDGET
from lotcycle.retry import full_jitter, proactive_delay

CLIENTS = 48
CAPACITY = 8
LIFETIME = 8
TIMEOUT_S = 2.0
BROWNOUT_START = 20
BROWNOUT_END = 28
HORIZON = 120


class ShedGate:
    """Token-endpoint overload response. Two mutations, no stack walk."""

    def __init__(self) -> None:
        self.count = 0
        self.logs: list[dict] = []

    def reject(self) -> dict:
        self.count += 1
        self.logs.append({"event": "shed"})
        return {
            "cache-control": "no-store",
            "pragma": "no-cache",
            "retry_after": "1",
            "status": 503,
        }


def simulate_brownout(
    arm: str,
    seed: int = 3,
    brownout: bool = True,
    shed: bool | None = None,
) -> dict:
    if arm not in ("unbounded", "budgeted"):
        raise ValueError(arm)
    budgeted = arm == "budgeted"
    shedding = budgeted if shed is None else shed
    gate = ShedGate()
    clients = [
        {
            "attempts": 0,
            "phase_rng": random.Random(10_000 + seed * 100 + index),
            "ready": 0,
            "rng": random.Random(seed * 100 + index),
            # Staggered expiry: CLIENTS / LIFETIME tokens expire per tick.
            "token_until": 1 + index % LIFETIME,
        }
        for index in range(CLIENTS)
    ]
    goodput: list[int] = []
    attempts_series: list[int] = []
    latency: list[float] = []
    sheds: list[dict] = []

    for tick in range(HORIZON):
        down = brownout and BROWNOUT_START <= tick < BROWNOUT_END
        arrivals = [
            client
            for client in clients
            if client["token_until"] <= tick and client["ready"] <= tick
        ]
        offered = len(arrivals)
        if down or (offered > CAPACITY and not shedding):
            served, timed_out, shed = [], arrivals, []
        else:
            served, timed_out, shed = arrivals[:CAPACITY], [], arrivals[CAPACITY:]

        for client in served:
            latency.append(0.1)
            client["token_until"] = tick + LIFETIME
            client["attempts"] = 0
        for client in timed_out:
            # Timed-out calls stay in the latency sample (no coordinated omission).
            latency.append(TIMEOUT_S)
            _after_failure(client, tick, budgeted, retry_after=None)
        for client in shed:
            response = gate.reject()
            sheds.append(response)
            _after_failure(client, tick, budgeted, retry_after=int(response["retry_after"]))

        attempts_series.append(offered)
        goodput.append(sum(1 for client in clients if client["token_until"] > tick))

    return {
        "arm": arm,
        "attempts": attempts_series,
        "baseline_goodput": goodput[BROWNOUT_START - 1],
        "brownout": brownout,
        "brownout_end": BROWNOUT_END,
        "brownout_start": BROWNOUT_START,
        "capacity": CAPACITY,
        "clients": CLIENTS,
        "goodput": goodput,
        "latency": latency,
        "shed_count": gate.count,
        "shed_logs": gate.logs,
        "shedding": shedding,
        "sheds": sheds,
        "timeout_s": TIMEOUT_S,
    }


def _after_failure(client: dict, tick: int, budgeted: bool, retry_after: int | None) -> None:
    if not budgeted:
        client["ready"] = tick + 1
        return
    client["attempts"] += 1
    if client["attempts"] >= PRECOMMIT_BUDGET:
        # Budget spent: resume on a fresh phase draw, not on a shared tick.
        delay = proactive_delay(client["phase_rng"], LIFETIME, True)
        client["ready"] = tick + max(1, math.ceil(delay))
        client["attempts"] = 0
        return
    if retry_after is not None:
        client["ready"] = tick + retry_after
        return
    sleep = full_jitter(client["rng"], client["attempts"] - 1, base=1.0, cap=4.0)
    client["ready"] = tick + max(1, math.ceil(sleep))
