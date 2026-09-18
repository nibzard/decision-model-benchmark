"""Metric unit tests against hand-computed cases (SPEC.md T0.3)."""

import math

import pytest

from dmb.metrics import (
    accuracy,
    brier,
    confidence_spread,
    ece,
    flip_rate,
    macro_f1,
    percentile,
    summarize_latencies,
)


def test_accuracy_hand_case():
    # 3 of 4 match.
    assert accuracy([0, 1, 1, 2], [0, 1, 0, 2]) == pytest.approx(0.75)


def test_accuracy_empty_is_nan():
    assert math.isnan(accuracy([], []))


def test_accuracy_length_mismatch_raises():
    with pytest.raises(ValueError):
        accuracy([1], [1, 2])


def test_macro_f1_hand_case():
    # preds [0,0,1,1] vs golds [0,1,1,2]:
    # class 0: tp=1 fp=1 fn=0 -> P=.5 R=1   F1=2/3
    # class 1: tp=1 fp=1 fn=1 -> P=.5 R=.5  F1=.5
    # class 2: tp=0 fp=1 fn=1 -> F1=0
    value = macro_f1([0, 0, 1, 1], [0, 1, 1, 2], num_classes=3)
    assert value == pytest.approx((2 / 3 + 0.5 + 0.0) / 3)


def test_macro_f1_perfect():
    assert macro_f1([0, 1, 2], [0, 1, 2], num_classes=3) == pytest.approx(1.0)


def test_macro_f1_counts_absent_classes():
    # Class 2 never appears but still drags the macro average down.
    value = macro_f1([0, 1], [0, 1], num_classes=3)
    assert value == pytest.approx(2 / 3)


def test_ece_hand_case():
    # conf .95/.85/.75/.45/.15, correct 1/0/1/0/0 - each item alone in a
    # bin, so ECE = mean |acc - conf|.
    confs = [0.95, 0.85, 0.75, 0.45, 0.15]
    corrects = [True, False, True, False, False]
    expected = (0.05 + 0.85 + 0.25 + 0.45 + 0.15) / 5
    assert ece(confs, corrects) == pytest.approx(expected)


def test_ece_perfectly_calibrated_bins():
    # Bin (0.7, 0.8]: four items at conf .75, three correct -> acc .75.
    assert ece([0.75] * 4, [True, True, True, False]) == pytest.approx(0.0)


def test_ece_bin_edges():
    # 0.0 lands in bin 0, 1.0 lands in the last bin; both bins get one
    # perfectly calibrated member -> ECE 0.
    assert ece([0.0, 1.0], [False, True]) == pytest.approx(0.0)


def test_ece_rejects_out_of_range():
    with pytest.raises(ValueError):
        ece([1.5], [True])


def test_brier_hand_case():
    assert brier([0.8, 0.6], [True, False]) == pytest.approx(0.2)


def test_brier_perfect():
    assert brier([0.0, 1.0], [False, True]) == pytest.approx(0.0)


def test_flip_rate_hand_case():
    rate = flip_rate({"a": [0, 0, 1], "b": [2, 2, 2]})
    assert rate == pytest.approx(0.5)


def test_flip_rate_single_observation_never_flips():
    assert flip_rate({"a": [3], "b": [1]}) == pytest.approx(0.0)


def test_percentile_interpolates():
    values = [10.0, 20.0, 30.0, 40.0]
    assert percentile(values, 50) == pytest.approx(25.0)
    assert percentile(values, 100) == pytest.approx(40.0)
    assert math.isnan(percentile([], 50))


def test_summarize_latencies_keys():
    summary = summarize_latencies(list(range(1, 101)))
    assert set(summary) == {"p50_ms", "p95_ms", "p99_ms"}
    assert summary["p50_ms"] <= summary["p95_ms"] <= summary["p99_ms"]


def test_confidence_spread():
    spread = confidence_spread({"a": [0.9, 0.7], "b": [0.5, 0.5]})
    assert spread["mean_range"] == pytest.approx(0.1)
    assert spread["max_range"] == pytest.approx(0.2)
