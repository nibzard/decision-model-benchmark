"""Runner tests with MockContender (SPEC.md T0.4) and retry protocol."""

import json

from dmb.contenders.base import (
    Decision,
    MalformedReply,
    MockContender,
    RateLimited,
)
from dmb.runner import RunSpec, execute_attempt, run_grid
from dmb.suites.items import DecisionItem, save_items, sha256_file


def _items(n: int = 20) -> list[DecisionItem]:
    return [
        DecisionItem(
            item_id=f"t-{i:03d}",
            suite="mocksuite",
            state=f"state number {i}",
            options=["alpha", "beta", "gamma"],
            gold_index=i % 3,
        )
        for i in range(n)
    ]


def _spec(tmp_path, contender, repeats: int = 1, **kwargs) -> RunSpec:
    items = _items()
    save_items(items, tmp_path / "data" / "suites" / "mocksuite.jsonl")
    return RunSpec(
        run_id="testrun",
        suites=["mocksuite"],
        contenders=[contender],
        repeats=repeats,
        **kwargs,
    )


def test_runner_smoke_writes_manifest_and_hashes(tmp_path):
    """T0.4: MockContender x 20-item suite -> manifest written, hashes right."""
    contender = MockContender.oracle(name="mock")
    spec = _spec(tmp_path, contender, repeats=2)
    run_dir = run_grid(spec, repo_root=tmp_path)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    suite_file = tmp_path / "data" / "suites" / "mocksuite.jsonl"
    assert manifest["item_files"]["mocksuite"] == sha256_file(suite_file)
    assert manifest["cells"][0]["rows"] == 40  # 20 items x 2 repeats
    assert manifest["cells"][0]["status"] == "ok"
    rows = [
        json.loads(line)
        for line in (run_dir / "results.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 40
    assert all(row["choice_index"] == 0 for row in rows)
    raw_files = list((run_dir / "raw").glob("*.jsonl"))
    assert len(raw_files) == 1
    raw_rows = [json.loads(line) for line in raw_files[0].read_text().splitlines()]
    assert len(raw_rows) == 40
    assert raw_rows[0]["attempts"][0]["outcome"] == "ok"
    # Unpriced mock contender: spend 0, note recorded.
    assert manifest["spend_usd"] == 0.0
    assert any("no price entry" in note for note in manifest["notes"])


def test_malformed_retry_then_success():
    contender = MockContender(
        replies=[
            MalformedReply("first reply is junk"),
            Decision(choice_index=1, confidence=0.8),
        ]
    )
    item = _items(1)[0]
    decision, raw = execute_attempt(contender, item, repeat=0)
    assert decision.ok and decision.choice_index == 1
    assert decision.retries == 1
    assert [a["outcome"] for a in raw["attempts"]] == ["malformed", "ok"]


def test_malformed_twice_counts_as_malformed_not_wrong():
    contender = MockContender(replies=[MalformedReply("junk")])
    item = _items(1)[0]
    decision, raw = execute_attempt(contender, item, repeat=0)
    assert not decision.ok
    assert decision.malformed
    assert decision.choice_index is None
    assert len(raw["attempts"]) == 2


def test_rate_limit_backoff_retry_then_fail():
    contender = MockContender(replies=[RateLimited("429")])
    item = _items(1)[0]
    decision, raw = execute_attempt(contender, item, repeat=0)
    assert not decision.ok and not decision.malformed
    assert "rate limited" in decision.error
    assert len(raw["attempts"]) == 2


def test_rate_limit_then_success():
    contender = MockContender(
        replies=[RateLimited("429"), Decision(choice_index=2, confidence=0.5)]
    )
    item = _items(1)[0]
    decision, raw = execute_attempt(contender, item, repeat=0)
    assert decision.ok and decision.choice_index == 2
    assert [a["outcome"] for a in raw["attempts"]] == ["rate_limited", "ok"]


def test_gold_correctness_in_rows(tmp_path):
    contender = MockContender(name="mock", replies=[
        Decision(choice_index=i % 3, confidence=0.9) for i in range(20)
    ])
    spec = _spec(tmp_path, contender, repeats=1)
    run_dir = run_grid(spec, repo_root=tmp_path)
    rows = [
        json.loads(line)
        for line in (run_dir / "results.jsonl").read_text().splitlines()
    ]
    for row in rows:
        assert row["correct"] == (row["choice_index"] == row["gold_index"])


def test_item_limit_smoke(tmp_path):
    contender = MockContender.oracle(name="mock")
    spec = _spec(tmp_path, contender, repeats=1, item_limit=5)
    run_dir = run_grid(spec, repo_root=tmp_path)
    rows = (run_dir / "results.jsonl").read_text().splitlines()
    assert len(rows) == 5
    assert json.loads((run_dir / "manifest.json").read_text())["protocol"]["item_limit"] == 5


def test_hard_budget_abort_keeps_partials(tmp_path):
    contender = MockContender(name="openai:gpt-5.4-nano", replies=[
        Decision(choice_index=0, confidence=0.5, input_tokens=100, output_tokens=10)
    ])
    spec = _spec(tmp_path, contender, repeats=1, hard_cap_usd=0.0)
    run_dir = run_grid(spec, repo_root=tmp_path)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    statuses = {cell["status"] for cell in manifest["cells"]}
    assert "budget" in statuses
    assert (run_dir / "results.jsonl").exists()
    assert manifest["skipped"], "budget abort records the skip"
