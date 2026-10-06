"""Deterministic fake provider. Retries stay inside one trial."""

from __future__ import annotations

from typing import Any, Callable, Mapping


class ProviderError(Exception):
    def __init__(self, error_type: str, partial: str | None = None) -> None:
        super().__init__(error_type)
        self.error_type = error_type
        self.partial = partial


class ScriptedProvider:
    """Epoch scripts keyed by case id, side, and epoch index."""

    def __init__(self, scripts: Mapping[str, Mapping[str, list[Mapping[str, Any]]]]) -> None:
        self.scripts = scripts
        self.attempts: dict[tuple[str, int, str], int] = {}

    def complete(self, case_id: str, epoch: int, side: str) -> Mapping[str, Any]:
        try:
            spec = self.scripts[case_id][side][epoch]
        except (KeyError, IndexError) as exc:
            raise ProviderError("script_missing") from exc
        key = (case_id, epoch, side)
        seen = self.attempts.get(key, 0)
        self.attempts[key] = seen + 1
        fail_times = int(spec.get("fail_times") or 0)
        if seen < fail_times:
            partial = spec.get("partial_text")
            raise ProviderError(str(spec.get("error") or "timeout"), partial if partial else None)
        return spec


def invoke(
    provider: ScriptedProvider,
    case_id: str,
    epoch: int,
    side: str,
    max_retries: int,
    sleeper: Callable[[float], None],
    initial_delay: float = 0.05,
) -> dict[str, Any]:
    """Call the provider. Exponential backoff is recorded; the sleeper may be a no-op."""
    backoffs: list[float] = []
    delay = initial_delay
    for attempt in range(max_retries + 1):
        try:
            spec = provider.complete(case_id, epoch, side)
        except ProviderError as exc:
            if attempt >= max_retries:
                return {
                    "ok": False,
                    "spec": None,
                    "attempts": attempt,
                    "backoffs": backoffs,
                    "error_type": exc.error_type,
                    "partial": exc.partial,
                }
            backoffs.append(delay)
            sleeper(delay)
            delay *= 2.0
            continue
        return {
            "ok": True,
            "spec": spec,
            "attempts": attempt,
            "backoffs": backoffs,
            "error_type": None,
            "partial": None,
        }
    raise RuntimeError("curbgate: retry loop exited without a result")
