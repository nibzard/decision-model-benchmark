"""Runner tests with MockContender (SPEC.md T0.4) and retry protocol.

Covers the run-ID reservation (finding 4), attempt records with per-attempt
usage (finding 2), stop scopes, draining, and exit codes (findings 3, 6, 7).
"""

import json

import pytest

from dmb.contenders.base import (
    AuthError,
    Decision,
    MalformedReply,
    MockContender,
    RateLimited,
)
from dmb.prices import price_for
from dmb.runner import (
    EXIT_AUTH,
    EXIT_BUDGET,
    EXIT_DEADLINE,
    EXIT_EXECUTION,
    EXIT_USAGE,
    AttemptOutcome,
    RunIdError,
    RunSpec,
    execute_attempt,
    exit_code_for,
    reserve_run_dir,
    run_grid,
    validate_run_id,
)
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


# ---- run-ID reservation (finding 4) ----------------------------------------


@pytest.mark.parametrize("bad", ["", "  ", " v1", "v1 ", ".", "..", "a/b", "/etc", "a\\b"])
def test_validate_run_id_rejects_unsafe_names(bad):
    with pytest.raises(RunIdError):
        validate_run_id(bad)


def test_validate_run_id_accepts_plain_names():
    assert validate_run_id("v2-correction-01") == "v2-correction-01"


def test_reserve_run_dir_refuses_existing_directory(tmp_path):
    first = reserve_run_dir(tmp_path, "once")
    (first / "manifest.json").write_text("{}")
    with pytest.raises(RunIdError, match="already exists"):
        reserve_run_dir(tmp_path, "once")
    # The existing run stays untouched.
    assert (first / "manifest.json").read_text() == "{}"


def test_run_grid_refuses_existing_run_directory(tmp_path):
    """A second run with the same ID must not append to the first run."""
    spec = _spec(tmp_path, MockContender.oracle(name="mock"))
    run_dir, _manifest = run_grid(spec, repo_root=tmp_path)
    rows_before = len((run_dir / "results.jsonl").read_text().splitlines())
    with pytest.raises(RunIdError, match="already exists"):
        run_grid(spec, repo_root=tmp_path)
    assert len((run_dir / "results.jsonl").read_text().splitlines()) == rows_before


# ---- attempt records (finding 2) --------------------------------------------


def test_malformed_retry_then_success():
    contender = MockContender(
        replies=[
            MalformedReply("first reply is junk"),
            Decision(choice_index=1, confidence=0.8),
        ]
    )
    item = _items(1)[0]
    outcome = execute_attempt(contender, item, repeat=0)
    assert isinstance(outcome, AttemptOutcome)
    assert outcome.decision.ok and outcome.decision.choice_index == 1
    assert outcome.decision.retries == 1
    assert [a.outcome for a in outcome.attempts] == ["malformed", "ok"]


def test_malformed_twice_counts_as_malformed_not_wrong():
    contender = MockContender(replies=[MalformedReply("junk")])
    item = _items(1)[0]
    outcome = execute_attempt(contender, item, repeat=0)
    assert not outcome.decision.ok
    assert outcome.decision.malformed
    assert outcome.decision.choice_index is None
    assert len(outcome.attempts) == 2


def test_rate_limit_backoff_retry_then_fail():
    contender = MockContender(replies=[RateLimited("429")])
    item = _items(1)[0]
    outcome = execute_attempt(contender, item, repeat=0, sleep=lambda _s: None)
    assert not outcome.decision.ok and not outcome.decision.malformed
    assert "rate limited" in outcome.decision.error
    assert len(outcome.attempts) == 2


def test_rate_limit_then_success():
    contender = MockContender(
        replies=[RateLimited("429"), Decision(choice_index=2, confidence=0.5)]
    )
    item = _items(1)[0]
    outcome = execute_attempt(contender, item, repeat=0, sleep=lambda _s: None)
    assert outcome.decision.ok and outcome.decision.choice_index == 2
    assert [a.outcome for a in outcome.attempts] == ["rate_limited", "ok"]


def test_decision_latency_includes_retry_backoff():
    """``latency_scope: decision``: backoff before the retry counts."""
    contender = MockContender(
        replies=[RateLimited("429"), Decision(choice_index=2, confidence=0.5)]
    )
    item = _items(1)[0]
    clock = {"t": 0.0, "slept": 0.0}

    def fake_sleep(seconds: float) -> None:
        clock["slept"] += seconds
        clock["t"] += seconds

    def fake_monotonic() -> float:
        return clock["t"]

    outcome = execute_attempt(
        contender, item, repeat=0, sleep=fake_sleep, monotonic=fake_monotonic
    )
    assert clock["slept"] == 4.0  # BACKOFF_BASE_S * 2**1
    assert outcome.elapsed_ms >= 4000.0
    assert outcome.attempts[-1].elapsed_ms < 100.0  # attempt timing excludes backoff


def test_every_attempt_recorded_exactly_once_with_usage():
    """Failed attempts bill their attached usage too."""
    malformed = MalformedReply("junk")
    malformed.attach(10, 2, {"body": "junk"})
    contender = MockContender(
        replies=[malformed, Decision(choice_index=1, confidence=0.8,
                                     input_tokens=100, output_tokens=10)]
    )
    item = _items(1)[0]
    seen: list = []
    outcome = execute_attempt(
        contender, item, repeat=0, run_id="r1", on_attempt=seen.append
    )
    assert len(seen) == 2
    assert [r.input_tokens for r in seen] == [10, 100]
    assert [r.output_tokens for r in seen] == [2, 10]
    assert seen[0].outcome == "malformed"
    assert seen[0].usage_complete is True
    assert seen[0].attempt == 1 and seen[1].attempt == 2
    assert seen[0].response == {"body": "junk"}
    assert outcome.attempts == seen  # same records, in order


def test_auth_error_is_terminal_attempt():
    contender = MockContender(replies=[AuthError("bad key")])
    item = _items(1)[0]
    outcome = execute_attempt(contender, item, repeat=0)
    assert not outcome.decision.ok
    assert "authentication" in outcome.decision.error
    assert [a.outcome for a in outcome.attempts] == ["auth"]


def test_stop_check_skips_before_first_attempt():
    contender = MockContender.oracle(name="mock")
    item = _items(1)[0]
    outcome = execute_attempt(
        contender, item, repeat=0, stop_check=lambda: "hard cap exceeded"
    )
    assert outcome.skipped
    assert outcome.decision is None
    assert outcome.attempts == []
    assert outcome.skipped_reason == "hard cap exceeded"
    assert contender.calls == []  # no request started


def test_stop_check_suppresses_retry_but_keeps_first_attempt():
    """After a stop, the failed first attempt stands; no second request."""
    contender = MockContender(replies=[RateLimited("429")])
    item = _items(1)[0]
    stopped = {"flag": False}

    def stop_check():
        return "run stopped" if stopped["flag"] else None

    original = contender._decide

    def flip_then_decide(state, options):
        stopped["flag"] = True  # stop arrives with the first failure
        return original(state, options)

    contender._decide = flip_then_decide
    outcome = execute_attempt(contender, item, repeat=0, sleep=lambda _s: None,
                              stop_check=stop_check)
    assert len(outcome.attempts) == 1
    assert not outcome.decision.ok
    assert "retry suppressed after stop: run stopped" in outcome.decision.error


# ---- grid execution ---------------------------------------------------------


def test_runner_smoke_writes_manifest_and_hashes(tmp_path):
    """T0.4: MockContender x 20-item suite -> manifest written, hashes right."""
    contender = MockContender.oracle(name="mock")
    spec = _spec(tmp_path, contender, repeats=2)
    run_dir, manifest = run_grid(spec, repo_root=tmp_path)
    suite_file = tmp_path / "data" / "suites" / "mocksuite.jsonl"
    assert manifest["item_files"]["mocksuite"] == sha256_file(suite_file)
    assert manifest["cells"][0]["rows"] == 40  # 20 items x 2 repeats
    assert manifest["cells"][0]["status"] == "ok"
    assert manifest["protocol_version"] == "v2"
    assert manifest["protocol"]["latency_scope"] == "decision"
    rows = [
        json.loads(line)
        for line in (run_dir / "results.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 40
    assert all(row["latency_scope"] == "decision" for row in rows)
    assert all(row["choice_index"] == 0 for row in rows)
    raw_files = sorted((run_dir / "raw").glob("*.jsonl"))
    # One decision log plus one attempts log for the one cell.
    assert [f.name for f in raw_files] == [
        "mock.mocksuite.attempts.jsonl", "mock.mocksuite.jsonl",
    ]
    attempts = [
        json.loads(line)
        for line in (run_dir / "raw" / "mock.mocksuite.attempts.jsonl")
        .read_text().splitlines()
    ]
    assert len(attempts) == 40  # one attempt per decision, each recorded once
    assert all(a["outcome"] == "ok" for a in attempts)
    assert attempts[0]["run_id"] == "testrun"
    raw_rows = [
        json.loads(line)
        for line in (run_dir / "raw" / "mock.mocksuite.jsonl").read_text().splitlines()
    ]
    assert raw_rows[0]["attempts"][0]["outcome"] == "ok"
    assert manifest["spend_usd"] == 0.0  # unpriced mock contender
    assert any("no price entry" in note for note in manifest["notes"])
    assert (run_dir / "prices.snapshot.json").exists()
    assert exit_code_for(manifest) == 0


def test_failed_attempts_billed_and_recorded(tmp_path):
    """A malformed-then-ok decision bills both attempts (finding 2)."""
    malformed = MalformedReply("junk")
    malformed.attach(50, 5, "junk-body")
    contender = MockContender(
        name="openai:gpt-5.4-nano",
        replies=[malformed, Decision(choice_index=0, confidence=0.9,
                                     input_tokens=100, output_tokens=10)],
    )
    spec = _spec(tmp_path, contender, repeats=1, item_limit=1)
    run_dir, manifest = run_grid(spec, repo_root=tmp_path)
    price = price_for("openai:gpt-5.4-nano")
    expected = price.cost_usd(50, 5) + price.cost_usd(100, 10)
    assert manifest["spend_usd"] == round(expected, 4)
    attempts = (run_dir / "raw" / "openai__gpt-5.4-nano.mocksuite.attempts.jsonl"
                ).read_text().splitlines()
    assert len(attempts) == 2
    rows = (run_dir / "results.jsonl").read_text().splitlines()
    assert len(rows) == 1
    row = json.loads(rows[0])
    assert row["retries"] == 1
    assert row["input_tokens"] == 100  # final decision usage in the row


def test_gold_correctness_in_rows(tmp_path):
    contender = MockContender(name="mock", replies=[
        Decision(choice_index=i % 3, confidence=0.9) for i in range(20)
    ])
    spec = _spec(tmp_path, contender, repeats=1)
    run_dir, _manifest = run_grid(spec, repo_root=tmp_path)
    rows = [
        json.loads(line)
        for line in (run_dir / "results.jsonl").read_text().splitlines()
    ]
    for row in rows:
        assert row["correct"] == (row["choice_index"] == row["gold_index"])


def test_item_limit_smoke(tmp_path):
    contender = MockContender.oracle(name="mock")
    spec = _spec(tmp_path, contender, repeats=1, item_limit=5)
    run_dir, _manifest = run_grid(spec, repo_root=tmp_path)
    rows = (run_dir / "results.jsonl").read_text().splitlines()
    assert len(rows) == 5
    assert json.loads((run_dir / "manifest.json").read_text())["protocol"]["item_limit"] == 5


def test_hard_budget_abort_keeps_partials(tmp_path):
    contender = MockContender(name="openai:gpt-5.4-nano", replies=[
        Decision(choice_index=0, confidence=0.5,
                 input_tokens=500_000, output_tokens=100_000)
    ])
    spec = _spec(tmp_path, contender, repeats=1, hard_cap_usd=0.0)
    run_dir, manifest = run_grid(spec, repo_root=tmp_path)
    statuses = {cell["status"] for cell in manifest["cells"]}
    assert "budget" in statuses
    assert (run_dir / "results.jsonl").exists()
    # Stopped cells are listed with rows 0 and the expected denominator.
    stopped = [c for c in manifest["cells"] if c["status"] != "ok"]
    assert stopped and all(c["expected_rows"] > 0 for c in stopped)
    assert manifest["status"] == "stopped_budget"
    assert exit_code_for(manifest) == EXIT_BUDGET
    # The cap cannot prevent charges for requests already recorded.
    assert manifest["spend_usd"] > 0.0
    assert manifest["budget_overshoot_usd"] == round(manifest["spend_usd"], 4)


def test_auth_failure_stops_provider_lane(tmp_path):
    """An auth failure stops that provider's lane; later cells are skipped."""
    broken = MockContender(
        name="openai:gpt-5.4-nano", replies=[AuthError("invalid key")]
    )
    healthy = MockContender.oracle(name="openai:gpt-5.4-mini")
    items = _items()
    save_items(items, tmp_path / "data" / "suites" / "mocksuite.jsonl")
    save_items(items, tmp_path / "data" / "suites" / "mocksuite2.jsonl")
    spec = RunSpec(
        run_id="authrun",
        suites=["mocksuite", "mocksuite2"],
        contenders=[broken, healthy],
        repeats=1,
    )
    run_dir, manifest = run_grid(spec, repo_root=tmp_path)
    # The provider lane stops: every cell after the first auth error is
    # skipped with its expected denominator, not silently missing.
    cells = {(c["contender"], c["suite"]): c for c in manifest["cells"]}
    assert cells[("openai:gpt-5.4-nano", "mocksuite")]["status"] == "auth_error"
    assert cells[("openai:gpt-5.4-nano", "mocksuite2")]["status"] == "skipped"
    assert cells[("openai:gpt-5.4-mini", "mocksuite")]["status"] == "skipped"
    assert all(
        c["expected_rows"] == 20 for c in manifest["cells"]
    )
    assert manifest["status"] == "stopped_auth"
    assert exit_code_for(manifest) == EXIT_AUTH
    # Failed decisions already in flight when the lane stopped are kept;
    # none is counted as a wrong answer.
    rows = [
        json.loads(line)
        for line in (run_dir / "results.jsonl").read_text().splitlines()
    ]
    assert 1 <= len(rows) <= 4  # at most one in-flight batch
    assert all(not row["ok"] for row in rows)


def test_worker_failure_fails_the_run(tmp_path):
    """An unexpected exception in a worker fails the run (exit 3)."""

    class Exploder(MockContender):
        def _decide(self, state, options):
            raise ValueError("boom")

    spec = _spec(tmp_path, Exploder(name="mock"), repeats=1)
    _run_dir, manifest = run_grid(spec, repo_root=tmp_path)
    assert manifest["cells"][0]["status"] == "failed"
    assert manifest["status"] == "failed"
    assert exit_code_for(manifest) == EXIT_EXECUTION
    assert "boom" in manifest["cells"][0]["abort_reason"]


def test_deadline_stops_cell_and_drains_running_requests(tmp_path):
    """A cell deadline cancels queued work and keeps collected rows."""
    contender = MockContender.oracle(name="mock")
    contender.latency_ms = 40.0
    items = _items(40)
    save_items(items, tmp_path / "data" / "suites" / "mocksuite.jsonl")
    spec = RunSpec(
        run_id="deadlinerun",
        suites=["mocksuite"],
        contenders=[contender],
        repeats=1,
        suite_wall_limit_s=0.12,
    )
    run_dir, manifest = run_grid(spec, repo_root=tmp_path)
    cell = manifest["cells"][0]
    assert cell["status"] == "dnf"
    assert 0 < cell["rows"] < 40
    assert cell["expected_rows"] == 40
    assert cell["drained_rows"] >= 1  # in-flight requests were collected
    rows = (run_dir / "results.jsonl").read_text().splitlines()
    assert len(rows) == cell["rows"]
    assert manifest["status"] == "stopped_deadline"
    assert exit_code_for(manifest) == EXIT_DEADLINE


def test_exit_usage_constant_is_two():
    assert EXIT_USAGE == 2
