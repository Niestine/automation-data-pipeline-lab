"""In-process capacity cliff from the SRE worked example.

Healthy at C, collapse at 1.1 C, still crashing at 0.9 C, stable again near 0.1 C
when about a tenth of the instances can serve. Ratios are that example, not a fitted curve.
"""

from __future__ import annotations


class CapacityModel:
    def __init__(self, capacity: int = 10000, instances: int = 100) -> None:
        if capacity <= 0 or instances <= 0:
            raise ValueError("capacity and instances must be positive")
        self.capacity = int(capacity)
        self.instances = int(instances)
        self.healthy_fraction = 1.0
        self.crash_loop = False
        self.avoid_errors = False
        self.removed = 0

    def collapse_load(self) -> float:
        usable = self.instances - (self.removed if self.avoid_errors else 0)
        if usable < 0:
            usable = 0
        effective = self.capacity * (usable / self.instances)
        return 1.1 * effective

    def highest_stable_load(self) -> int:
        line = self.collapse_load()
        if line <= 0:
            return 0
        if line == int(line):
            return int(line) - 1
        return int(line)

    def note_injected_errors(self, count: int) -> None:
        if count < 0:
            raise ValueError("count must be >= 0")
        self.removed += count

    def offer(self, load: int) -> str:
        offered = int(load)
        if self.crash_loop:
            if offered <= self.capacity // 10:
                self.crash_loop = False
                self.healthy_fraction = 1.0
                return "recovered"
            return "crash-loop"
        if offered >= self.collapse_load():
            self.crash_loop = True
            self.healthy_fraction = 0.1
            return "collapse"
        return "stable"


def retry_offered_load(base_load: int, raf: float) -> int:
    return int(round(base_load * raf))
