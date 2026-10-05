"""One-row OCC simulation used to compare backoff, re-run locally.

The structure follows Marc Brooker's archived simulator: one row, one version,
read-modify-write clients, and a normal network delay. Heap entries carry a
sequence number so equal timestamps do not compare bound methods. Call counts
come from the run; the blog's unpublished totals are not embedded.
"""

from __future__ import annotations

import heapq
import random
from dataclasses import dataclass


@dataclass
class Stats:
    failures: int = 0
    calls: int = 0


class Net:
    def __init__(self, mean: float, sd: float, rng: random.Random) -> None:
        self.mean = mean
        self.sd = sd
        self.rng = rng

    def delay(self) -> float:
        return abs(self.rng.normalvariate(self.mean, self.sd))


class Backoff:
    def __init__(self, base: float, cap: float, rng: random.Random) -> None:
        self.base = base
        self.cap = cap
        self.rng = rng

    def expo(self, n: int) -> float:
        return min(self.cap, (2**n) * self.base)

    def backoff(self, n: int) -> float:
        raise NotImplementedError


class NoBackoff(Backoff):
    def backoff(self, n: int) -> float:
        return 0


class ExpoBackoff(Backoff):
    def backoff(self, n: int) -> float:
        return self.expo(n)


class ExpoBackoffEqualJitter(Backoff):
    def backoff(self, n: int) -> float:
        value = self.expo(n)
        return value / 2 + self.rng.uniform(0, value / 2)


class ExpoBackoffFullJitter(Backoff):
    def backoff(self, n: int) -> float:
        value = self.expo(n)
        return self.rng.uniform(0, value)


class ExpoBackoffDecorr(Backoff):
    def __init__(self, base: float, cap: float, rng: random.Random) -> None:
        super().__init__(base, cap, rng)
        self.sleep = self.base

    def backoff(self, n: int) -> float:
        self.sleep = min(self.cap, self.rng.uniform(self.base, self.sleep * 3))
        return self.sleep


class OccServer:
    def __init__(self, net: Net, stats: Stats, seq: list[int]) -> None:
        self.version = 0
        self.net = net
        self.stats = stats
        self.seq = seq

    def _msg(self, tm: float, send_to, reply_to, payload) -> tuple:
        self.seq[0] += 1
        return (tm, self.seq[0], send_to, reply_to, payload)

    def write(self, tm: float, request: tuple) -> tuple:
        success = False
        self.stats.calls += 1
        if request[4] == self.version:
            self.version += 1
            success = True
        else:
            self.stats.failures += 1
        return self._msg(tm + self.net.delay(), request[3], None, success)

    def read(self, tm: float, request: tuple) -> tuple:
        return self._msg(tm + self.net.delay(), request[3], None, self.version)


class OccClient:
    def __init__(self, server: OccServer, net: Net, backoff: Backoff, seq: list[int]) -> None:
        self.server = server
        self.net = net
        self.attempt = 0
        self.backoff = backoff
        self.seq = seq

    def _msg(self, tm: float, send_to, reply_to, payload) -> tuple:
        self.seq[0] += 1
        return (tm, self.seq[0], send_to, reply_to, payload)

    def start(self, tm: float) -> tuple:
        return self._msg(tm + self.net.delay(), self.server.read, self.read_rsp, None)

    def read_rsp(self, tm: float, request: tuple) -> tuple:
        return self._msg(tm + self.net.delay(), self.server.write, self.write_rsp, request[4])

    def write_rsp(self, tm: float, request: tuple) -> tuple | None:
        if not request[4]:
            self.attempt += 1
            delay = self.net.delay() + self.backoff.backoff(self.attempt)
            return self._msg(tm + delay, self.server.read, self.read_rsp, None)
        return None


def run_sim(queue: list[tuple]) -> float:
    tm = 0.0
    while queue:
        message = heapq.heappop(queue)
        if message[0] < tm:
            raise RuntimeError("simulation time moved backwards")
        tm = message[0]
        nxt = message[2](tm, message)
        if nxt is not None:
            heapq.heappush(queue, nxt)
    return tm


def setup_sim(
    clients: int,
    backoff_cls: type[Backoff],
    rng: random.Random,
    stats: Stats,
) -> list[tuple]:
    net = Net(10, 2, rng)
    seq = [0]
    server = OccServer(net, stats, seq)
    queue: list[tuple] = []
    for _ in range(clients):
        client = OccClient(server, net, backoff_cls(5, 2000, rng), seq)
        heapq.heappush(queue, client.start(0))
    return queue


ALGORITHMS: dict[str, type[Backoff]] = {
    "no_backoff": NoBackoff,
    "exponential": ExpoBackoff,
    "equal_jitter": ExpoBackoffEqualJitter,
    "full_jitter": ExpoBackoffFullJitter,
    "decorrelated": ExpoBackoffDecorr,
}


def compare_occ(clients: int, trials: int, seed: int) -> dict[str, int]:
    """Total write calls for each algorithm on the one-row OCC setup."""
    totals: dict[str, int] = {}
    for name, cls in ALGORITHMS.items():
        rng = random.Random(seed)
        stats = Stats()
        for _ in range(trials):
            queue = setup_sim(clients, cls, rng, stats)
            run_sim(queue)
        totals[name] = stats.calls
    return totals
