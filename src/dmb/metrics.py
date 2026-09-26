"""Metrics: accuracy, macro-F1, 10-bin ECE, Brier, flip rate, percentiles.

All functions take plain sequences. Errored or malformed items are excluded
by the caller before these functions run; nothing here filters.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np


def accuracy(preds: Sequence[int], golds: Sequence[int]) -> float:
    """Fraction of exact matches. Empty input returns NaN."""
    if len(preds) != len(golds):
        raise ValueError("preds and golds must have equal length")
    if not preds:
        return float("nan")
    hits = sum(p == g for p, g in zip(preds, golds, strict=True))
    return hits / len(preds)


def macro_f1(preds: Sequence[int], golds: Sequence[int], num_classes: int) -> float:
    """Macro-averaged F1 over ``range(num_classes)``.

    Classes absent from both ``preds`` and ``golds`` score 0 and still count
    in the average, so the value only compares contenders run on the same
    suite. Option positions are not classes when options move between
    items; use :func:`macro_f1_labeled` for that case.
    """
    if num_classes <= 0:
        raise ValueError("num_classes must be positive")
    if len(preds) != len(golds):
        raise ValueError("preds and golds must have equal length")
    f1s: list[float] = []
    for c in range(num_classes):
        tp = sum(1 for p, g in zip(preds, golds, strict=True) if p == c and g == c)
        fp = sum(1 for p, g in zip(preds, golds, strict=True) if p == c and g != c)
        fn = sum(1 for p, g in zip(preds, golds, strict=True) if p != c and g == c)
        denom = 2 * tp + fp + fn
        f1s.append((2 * tp / denom) if denom else 0.0)
    return float(np.mean(f1s))


def macro_f1_labeled(
    preds: Sequence[str],
    golds: Sequence[str],
    classes: Sequence[str],
) -> float:
    """Macro-averaged F1 over stable class labels.

    ``classes`` is the fixed class universe (for DMB: the option texts of
    one frozen suite). Labels absent from both ``preds`` and ``golds``
    score 0 and still count in the average. Permuting an item's options
    cannot change the value, because the labels are the option texts, not
    positions.
    """
    if not classes:
        raise ValueError("classes must not be empty")
    if len(preds) != len(golds):
        raise ValueError("preds and golds must have equal length")
    known = set(classes)
    for label in [*preds, *golds]:
        if label not in known:
            raise ValueError(f"label {label!r} is not in the class universe")
    f1s: list[float] = []
    for c in classes:
        tp = sum(1 for p, g in zip(preds, golds, strict=True) if p == c and g == c)
        fp = sum(1 for p, g in zip(preds, golds, strict=True) if p == c and g != c)
        fn = sum(1 for p, g in zip(preds, golds, strict=True) if p != c and g == c)
        denom = 2 * tp + fp + fn
        f1s.append((2 * tp / denom) if denom else 0.0)
    return float(np.mean(f1s))


def ece(confidences: Sequence[float], corrects: Sequence[bool], bins: int = 10) -> float:
    """Equal-width expected calibration error over [0, 1].

    Bin 0 is ``[0, 0.1]``; bin ``b >= 1`` is ``(b/10, (b+1)/10]``. So a
    confidence of exactly 0.9 lands in bin 8, and 1.0 lands in bin 9.
    """
    if len(confidences) != len(corrects):
        raise ValueError("confidences and corrects must have equal length")
    if not confidences:
        return float("nan")
    total = len(confidences)
    conf = np.asarray(confidences, dtype=float)
    corr = np.asarray(corrects, dtype=float)
    if conf.min() < 0 or conf.max() > 1:
        raise ValueError("confidences must lie in [0, 1]")
    edges = np.linspace(0.0, 1.0, bins + 1)
    indices = np.digitize(conf, edges[1:-1], right=True)
    value = 0.0
    for b in range(bins):
        mask = indices == b
        n = int(mask.sum())
        if n == 0:
            continue
        gap = abs(corr[mask].mean() - conf[mask].mean())
        value += (n / total) * gap
    return float(value)


def brier(confidences: Sequence[float], corrects: Sequence[bool]) -> float:
    """Mean squared error of the confidence as a probability of correctness."""
    if len(confidences) != len(corrects):
        raise ValueError("confidences and corrects must have equal length")
    if not confidences:
        return float("nan")
    conf = np.asarray(confidences, dtype=float)
    corr = np.asarray(corrects, dtype=float)
    return float(np.mean((conf - corr) ** 2))


def flip_rate(choices_by_item: Mapping[str, Sequence[int]]) -> float:
    """Fraction of items whose choice is not constant across repeats.

    ``choices_by_item`` maps an item id to the observed choices (repeat or
    permutation order). An item with one observation counts as unflipped.
    """
    if not choices_by_item:
        return float("nan")
    flipped = sum(1 for cs in choices_by_item.values() if len(set(cs)) > 1)
    return flipped / len(choices_by_item)


def percentile(values: Sequence[float], q: float) -> float:
    """Percentile ``q`` in [0, 100] with linear interpolation. NaN for empty."""
    if not values:
        return float("nan")
    return float(np.percentile(np.asarray(values, dtype=float), q))


def summarize_latencies(values: Sequence[float]) -> dict[str, float]:
    """p50 / p95 / p99 latency summary."""
    return {
        "p50_ms": percentile(values, 50),
        "p95_ms": percentile(values, 95),
        "p99_ms": percentile(values, 99),
    }


def confidence_spread(confidences_by_item: Mapping[str, Sequence[float]]) -> dict[str, float]:
    """Per-item confidence spread across repeats or permutations."""
    if not confidences_by_item:
        return {"mean_range": float("nan"), "max_range": float("nan")}
    ranges = [max(cs) - min(cs) for cs in confidences_by_item.values() if cs]
    if not ranges:
        return {"mean_range": float("nan"), "max_range": float("nan")}
    return {"mean_range": float(np.mean(ranges)), "max_range": float(np.max(ranges))}


def cluster_mean_interval(
    values_by_item: Mapping[str, Sequence[float]],
    *,
    resamples: int = 2000,
    seed: int = 20260918,
) -> dict:
    """Descriptive percentile bootstrap of equally weighted item means.

    Repeated observations stay inside their item (base item for S4).
    This describes sampling uncertainty on observed items, not missing
    responses, dataset shift, or uncertainty about the provider population.
    A singleton has no estimable sampling interval.
    """
    means = np.asarray(
        [np.mean(values_by_item[key]) for key in sorted(values_by_item) if values_by_item[key]],
        dtype=float,
    )
    result = {
        "estimate": float(means.mean()) if len(means) else None,
        "lower": None,
        "upper": None,
        "n_clusters": len(means),
        "resamples": resamples,
        "seed": seed,
        "method": "item-cluster percentile bootstrap; equal item weights",
    }
    if len(means) >= 2:
        rng = np.random.default_rng(seed)
        draws = means[rng.integers(0, len(means), size=(resamples, len(means)))].mean(axis=1)
        lower, upper = np.percentile(draws, [2.5, 97.5])
        result.update(lower=float(lower), upper=float(upper))
    return result


def paired_cluster_difference(
    first: Mapping[str, Sequence[float]],
    second: Mapping[str, Sequence[float]],
) -> dict:
    """First minus second, paired on common items; repeats are not new items."""
    common = sorted(k for k in first.keys() & second.keys() if first[k] and second[k])
    differences = {k: [float(np.mean(first[k]) - np.mean(second[k]))] for k in common}
    return {
        **cluster_mean_interval(differences),
        "method": "paired item-cluster percentile bootstrap; equal item weights",
        "n_first_clusters": sum(bool(v) for v in first.values()),
        "n_second_clusters": sum(bool(v) for v in second.values()),
    }


def score_risk_coverage(
    confidences: Sequence[float],
    corrects: Sequence[bool],
    denominator: int,
) -> list[dict]:
    """Observed error among accepted rows at fixed, unfitted score cutoffs.

    Coverage uses all expected decisions, so failed or unscored decisions
    cannot disappear from the denominator. These are descriptive curves;
    selecting deployment cutoffs requires separate held-out validation.
    """
    if len(confidences) != len(corrects):
        raise ValueError("confidences and corrects must have equal length")
    if denominator < len(confidences):
        raise ValueError("coverage denominator is smaller than observed scores")
    out = []
    for threshold in (0.0, 0.25, 0.5, 0.75, 0.9, 0.95, 1.0):
        accepted = [c for score, c in zip(confidences, corrects, strict=True) if score >= threshold]
        out.append(
            {
                "threshold": threshold,
                "accepted": len(accepted),
                "expected_decisions": denominator,
                "coverage": len(accepted) / denominator if denominator else None,
                "risk": 1.0 - sum(accepted) / len(accepted) if accepted else None,
            }
        )
    return out
