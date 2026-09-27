"""The cheap pilot must preserve controlled suite coverage, not truncate files."""

import importlib.util
import json
from collections import Counter
from pathlib import Path

import pytest

from dmb.suites import s3_cardinality, s5_confidence
from dmb.suites.items import DecisionItem

spec = importlib.util.spec_from_file_location(
    "recent_sample", Path(__file__).resolve().parents[1] / "scripts/run_recent_sample.py"
)
pilot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pilot)


def test_s5_has_both_subsets():
    items = s5_confidence.build_items()
    selected = pilot.sample_items("s5_confidence", items)
    assert len(selected) == 40
    assert sum(item.gold_index < 0 for item in selected) == 20
    assert pilot.sample_items("s5_confidence", items) == selected


def test_cardinality_covers_every_n():
    items = s3_cardinality.build_items()
    selected = pilot.sample_items("s3_cardinality", items)
    counts = Counter(len(item.options) for item in selected)
    assert set(counts) == set(s3_cardinality.N_SWEEP)
    assert set(counts.values()) == {4}


def test_every_sampled_s4_base_has_all_three_orders():
    items = [
        DecisionItem(
            item_id=f"b{b}-p{p}",
            suite="s4_order",
            state="x",
            options=["a", "b"],
            gold_index=0,
            base_item_id=f"b{b}",
        )
        for b in range(100)
        for p in range(3)
    ]
    selected = pilot.sample_items("s4_order", items)
    counts = Counter(item.base_item_id for item in selected)
    assert len(counts) == 15 and set(counts.values()) == {3}


def test_banking_sample_keeps_one_item_per_intent():
    options = [f"intent-{i}" for i in range(77)]
    items = [
        DecisionItem(
            item_id=f"s1-{i}-{j}", suite="s1_intent77", state="x", options=options, gold_index=i
        )
        for i in range(77)
        for j in range(3)
    ]
    selected = pilot.sample_items("s1_intent77", items)
    assert len(selected) == 77
    assert {item.gold_index for item in selected} == set(range(77))


def test_spam_sample_preserves_source_prior():
    items = [
        DecisionItem(
            item_id=f"s2-{i}",
            suite="s2_spam",
            state="x",
            options=["ham", "spam"],
            gold_index=int(i < 40),
        )
        for i in range(300)
    ]
    selected = pilot.sample_items("s2_spam", items)
    assert len(selected) == 50
    assert sum(item.gold_index == 1 for item in selected) == 7


def test_pro_pacing_spaces_requests_including_retries(monkeypatch):
    clock = [100.0]
    starts = []
    monkeypatch.setenv("GEMINI_API_KEY", "fake-secret")
    monkeypatch.setattr(pilot.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(pilot.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    monkeypatch.setattr(pilot.GeminiContender, "_post", lambda self, body: starts.append(clock[0]))
    c = pilot.PacedPro()
    try:
        c._post({})
        clock[0] += 1
        c._post({})
        clock[0] += 7
        c._post({})
        assert starts == [100.0, 103.0, 110.0]
    finally:
        c.close()


@pytest.mark.parametrize(
    "status,spent,cap", [("running", 1, 10), ("complete", 10, 10), ("complete", 1, 11)]
)
@pytest.mark.parametrize("extension", ["repair_pro", "add_jev"])
def test_repair_rejects_running_exhausted_or_raised_budget(
    tmp_path, monkeypatch, status, spent, cap, extension
):
    monkeypatch.setattr(pilot, "ROOT", tmp_path)
    run = tmp_path / ".benchmark-studies" / "pilot" / "runs" / "pilot"
    run.mkdir(parents=True)
    (run / "manifest.json").write_text(
        json.dumps({"status": status, "spend_usd": spent, "protocol": {"hard_cap_usd": 10}})
    )
    with pytest.raises(ValueError):
        getattr(pilot, extension)("pilot", cap)
