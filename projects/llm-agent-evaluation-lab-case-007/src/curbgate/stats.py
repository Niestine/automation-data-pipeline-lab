"""Paired intervals, cluster-robust standard errors, power, and pass^k.

The paired SE is the ordinary standard error of item-level score differences.
The clustered SE follows Miller's paired cluster formula: one over n times the
square root of the sum of squared within-cluster sums of centered differences.
Intervals use a Student-t critical value. The normal 1.96 factor is reported
beside it as the large-sample approximation.
"""

from __future__ import annotations

import math
from typing import Mapping, Sequence

_FPMIN = 1.0e-30
_BETACF_EPS = 3.0e-14
_BETACF_ITERS = 200


def _betacf(a: float, b: float, x: float) -> float:
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _FPMIN:
        d = _FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, _BETACF_ITERS + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + aa / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + aa / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _BETACF_EPS:
            return h
    return h


def regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    """I_x(a, b). Used only to invert the Student-t CDF."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_beta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    bt = math.exp(a * math.log(x) + b * math.log(1.0 - x) - ln_beta)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def normal_quantile(p: float) -> float:
    """Standard-normal quantile. p is a probability in (0, 1)."""
    if not 0.0 < p < 1.0:
        raise ValueError("curbgate: normal quantile requires 0 < p < 1")
    lo, hi = -40.0, 40.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if normal_cdf(mid) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def student_t_cdf(t: float, df: int) -> float:
    if df < 1:
        raise ValueError("curbgate: Student-t df must be >= 1")
    x = df / (df + t * t)
    ib = regularized_incomplete_beta(df / 2.0, 0.5, x)
    if t >= 0.0:
        return 1.0 - 0.5 * ib
    return 0.5 * ib


def student_t_quantile(df: int, p: float) -> float:
    """Quantile of a Student-t with `df` degrees of freedom."""
    if df < 1:
        raise ValueError("curbgate: Student-t df must be >= 1")
    if not 0.0 < p < 1.0:
        raise ValueError("curbgate: t quantile requires 0 < p < 1")
    if p == 0.5:
        return 0.0
    if p < 0.5:
        return -student_t_quantile(df, 1.0 - p)
    lo, hi = 0.0, 1.0
    while student_t_cdf(hi, df) < p:
        hi *= 2.0
        if hi > 1.0e8:
            break
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if student_t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def sample_variance(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return sum((v - mean) ** 2 for v in values) / (n - 1)


def stderr_of_mean(values: Sequence[float]) -> float:
    n = len(values)
    if n < 1:
        raise ValueError("curbgate: empty sample")
    if n == 1:
        return 0.0
    return math.sqrt(sample_variance(list(values)) / n)


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    n = len(xs)
    if n != len(ys) or n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0.0 or dy == 0.0:
        return None
    return num / (dx * dy)


def paired_standard_error(diffs: Sequence[float]) -> float:
    """sqrt(sample_variance(diff) / n)."""
    return stderr_of_mean(diffs)


def clustered_paired_standard_error(diffs: Sequence[float], clusters: Sequence[str]) -> float:
    """(1/n) * sqrt(sum_c (sum_{i in c} (d_i - dbar))^2)."""
    n = len(diffs)
    if n < 1 or len(clusters) != n:
        raise ValueError("curbgate: clustered SE needs aligned non-empty rows")
    center = sum(diffs) / n
    buckets: dict[str, float] = {}
    for diff, cluster in zip(diffs, clusters):
        buckets[str(cluster)] = buckets.get(str(cluster), 0.0) + (diff - center)
    total = sum(value * value for value in buckets.values())
    return math.sqrt(total) / n


def repeats_cluster(clusters: Sequence[str]) -> bool:
    return len(set(clusters)) < len(clusters)


def interval_side(low: float, high: float) -> str:
    if low > 0.0:
        return "above"
    if high < 0.0:
        return "below"
    return "covers"


def significance_label(side: str, underpowered: bool) -> str:
    """Withhold significant_* labels when the precheck is underpowered."""
    if underpowered:
        return "underpowered"
    if side == "above":
        return "significant_improvement"
    if side == "below":
        return "significant_regression"
    return "inconclusive"


def required_n(
    omega2: float,
    sigma_a2: float,
    sigma_b2: float,
    k_a: int,
    k_b: int,
    delta: float,
    alpha: float = 0.05,
    power: float = 0.80,
) -> float:
    """Sample size for a paired test at the given delta, alpha, and power."""
    if delta <= 0.0:
        raise ValueError("curbgate: delta must be positive")
    if k_a < 1 or k_b < 1:
        raise ValueError("curbgate: K must be >= 1")
    if not 0.0 < alpha < 1.0 or not 0.0 < power < 1.0:
        raise ValueError("curbgate: alpha and power must lie in (0, 1)")
    z = normal_quantile(1.0 - alpha / 2.0) + normal_quantile(power)
    variance = omega2 + sigma_a2 / k_a + sigma_b2 / k_b
    if variance < 0.0:
        raise ValueError("curbgate: variance component is negative")
    return (z * z) * variance / (delta * delta)


def minimum_detectable_effect(
    n: int,
    omega2: float,
    sigma_a2: float,
    sigma_b2: float,
    k_a: int,
    k_b: int,
    alpha: float = 0.05,
    power: float = 0.80,
) -> float:
    if n < 1:
        raise ValueError("curbgate: n must be >= 1")
    z = normal_quantile(1.0 - alpha / 2.0) + normal_quantile(power)
    variance = omega2 + sigma_a2 / k_a + sigma_b2 / k_b
    return z * math.sqrt(variance / n)


def power_check(
    n: int,
    delta: float,
    omega2: float,
    sigma_a2: float = 0.0,
    sigma_b2: float = 0.0,
    k_a: int = 1,
    k_b: int = 1,
    alpha: float = 0.05,
    power: float = 0.80,
) -> dict[str, float | bool | int]:
    req = required_n(omega2, sigma_a2, sigma_b2, k_a, k_b, delta, alpha, power)
    mde = minimum_detectable_effect(n, omega2, sigma_a2, sigma_b2, k_a, k_b, alpha, power)
    underpowered = (n + 1e-8) < req
    return {
        "n": n,
        "delta": delta,
        "alpha": alpha,
        "power": power,
        "omega2": omega2,
        "sigma_a2": sigma_a2,
        "sigma_b2": sigma_b2,
        "k_a": k_a,
        "k_b": k_b,
        "required_n": req,
        "minimum_detectable_effect": mde,
        "underpowered": underpowered,
    }


def item_means(epoch_rows: Sequence[Sequence[float]]) -> list[float]:
    means = []
    for row in epoch_rows:
        if not row:
            raise ValueError("curbgate: an item has no epochs")
        means.append(sum(row) / len(row))
    return means


def item_mean_stderr(epoch_rows: Sequence[Sequence[float]]) -> float:
    """SE across item means. Epochs of one item are not extra samples."""
    return stderr_of_mean(item_means(epoch_rows))


def compare_scores(
    baseline: Sequence[float],
    candidate: Sequence[float],
    clusters: Sequence[str] | None = None,
    alpha: float = 0.05,
    delta: float = 0.03,
    power: float = 0.80,
    omega2: float | None = None,
    sigma_a2: float = 0.0,
    sigma_b2: float = 0.0,
    k_a: int = 1,
    k_b: int = 1,
) -> dict[str, object]:
    """Paired comparison. `omega2` overrides the variance used by the power precheck."""
    n = len(baseline)
    if n != len(candidate) or n < 1:
        raise ValueError("curbgate: paired scores must be non-empty and aligned")
    if clusters is not None and len(clusters) != n:
        raise ValueError("curbgate: cluster ids must align with scores")
    diffs = [float(c) - float(b) for b, c in zip(baseline, candidate)]
    mean = sum(diffs) / n
    use_cluster = clusters is not None and repeats_cluster(clusters)
    naive = paired_standard_error(diffs)
    if use_cluster:
        se = clustered_paired_standard_error(diffs, clusters or [])
        df = len(set(clusters or [])) - 1
        cluster_count = len(set(clusters or []))
    else:
        se = naive
        df = n - 1
        cluster_count = n
    if df >= 1:
        tcrit = student_t_quantile(df, 1.0 - alpha / 2.0)
    else:
        tcrit = float("inf")
    zcrit = normal_quantile(1.0 - alpha / 2.0)
    half_t = tcrit * se
    half_z = zcrit * se
    low, high = mean - half_t, mean + half_t
    if df < 1:
        side = "covers"
    else:
        side = interval_side(low, high)
    if omega2 is None:
        # se**2 * n is the variance the interval used, including cluster adjustment.
        stored = (se ** 2) * n
        check = power_check(n, delta, stored, 0.0, 0.0, 1, 1, alpha, power)
    else:
        stored = omega2
        check = power_check(n, delta, omega2, sigma_a2, sigma_b2, k_a, k_b, alpha, power)
    underpowered = bool(check["underpowered"]) or df < 1
    fail_count = sum(1 for diff in diffs if diff < 0.0)
    return {
        "n": n,
        "mean_diff": mean,
        "se": se,
        "naive_se": naive,
        "clustered": use_cluster,
        "cluster_count": cluster_count,
        "df": df,
        "t_critical": tcrit,
        "ci_low": low,
        "ci_high": high,
        "miller_low": mean - half_z,
        "miller_high": mean + half_z,
        "side": side,
        "label": significance_label(side, underpowered),
        "correlation": pearson(list(baseline), list(candidate)),
        "fail_count": fail_count,
        "improved_count": sum(1 for diff in diffs if diff > 0.0),
        "tie_count": sum(1 for diff in diffs if diff == 0.0),
        "underpowered": underpowered,
        "power": check,
        "omega2_used": stored,
        "baseline_mean": sum(baseline) / n,
        "candidate_mean": sum(candidate) / n,
    }


def pass_hat_k(successes: int, trials: int, k: int) -> float:
    """Unbiased P(all k trials succeed) = C(c, k) / C(n, k). NaN when n < k."""
    if k < 1:
        raise ValueError("curbgate: k must be >= 1")
    if trials < 0 or successes < 0 or successes > trials:
        raise ValueError("curbgate: invalid pass^k counts")
    if trials < k:
        return float("nan")
    if successes < k:
        return 0.0
    return math.comb(successes, k) / math.comb(trials, k)


def pass_at_k(successes: int, trials: int, k: int) -> float:
    """P(at least one success). Diagnostic only; not a reliability headline."""
    if k < 1:
        raise ValueError("curbgate: k must be >= 1")
    if trials < k:
        return float("nan")
    misses = trials - successes
    if misses < k:
        return 1.0
    return 1.0 - math.comb(misses, k) / math.comb(trials, k)


def mean_pass_hat_k(tasks: Sequence[tuple[int, int]], k: int) -> float:
    if not tasks:
        return float("nan")
    values = [pass_hat_k(c, n, k) for c, n in tasks]
    if any(math.isnan(v) for v in values):
        return float("nan")
    return sum(values) / len(values)


def resolve_headline(
    metrics: Sequence[tuple[str, float]],
    headline_metric: str,
) -> tuple[str, float]:
    """Pick the declared metric even when stderr is listed first."""
    for name, value in metrics:
        if name == headline_metric:
            return name, value
    known = ", ".join(name for name, _ in metrics)
    raise KeyError(f"curbgate: headline {headline_metric!r} is not among {known}")


def probability_summary(probabilities: Sequence[float]) -> dict[str, float]:
    if not probabilities:
        raise ValueError("curbgate: empty probability sample")
    for value in probabilities:
        if not 0.0 <= float(value) <= 1.0:
            raise ValueError("curbgate: token probability must lie in [0, 1]")
    probs = [float(v) for v in probabilities]
    return {"mean": sum(probs) / len(probs), "se": stderr_of_mean(probs)}


def majority(values: Sequence[float]) -> float | None:
    """Binary majority. A tie is unscored (None), not a forced zero."""
    if not values:
        return None
    ones = sum(1 for v in values if v >= 0.5)
    zeros = len(values) - ones
    if ones == zeros:
        return None
    return 1.0 if ones > zeros else 0.0


def aggregate_key(
    rows: Sequence[Mapping[str, object]],
    key: str,
    on_missing: str = "skip",
) -> float:
    """Mean of one score key. NaN is unscored. Missing follows on_missing."""
    if on_missing not in {"skip", "zero", "error"}:
        raise ValueError("curbgate: on_missing must be skip, zero, or error")
    kept: list[float] = []
    for row in rows:
        if key not in row or row[key] is None:
            if on_missing == "error":
                raise KeyError(f"curbgate: missing score key {key}")
            if on_missing == "zero":
                kept.append(0.0)
            continue
        value = row[key]
        if isinstance(value, float) and math.isnan(value):
            continue
        kept.append(float(value))  # type: ignore[arg-type]
    if not kept:
        return float("nan")
    return sum(kept) / len(kept)
