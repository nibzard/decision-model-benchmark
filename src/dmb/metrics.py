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
