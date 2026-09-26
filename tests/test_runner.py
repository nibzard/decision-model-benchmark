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

    outcome = execute_attempt(contender, item, repeat=0, sleep=fake_sleep, monotonic=fake_monotonic)
    assert clock["slept"] == 4.0  # BACKOFF_BASE_S * 2**1
    assert outcome.elapsed_ms >= 4000.0
    assert outcome.attempts[-1].elapsed_ms < 100.0  # attempt timing excludes backoff


def test_every_attempt_recorded_exactly_once_with_usage():
    """Failed attempts bill their attached usage too."""
    malformed = MalformedReply("junk")
    malformed.attach(10, 2, {"body": "junk"})
    contender = MockContender(
        replies=[
            malformed,
            Decision(choice_index=1, confidence=0.8, input_tokens=100, output_tokens=10),
        ]
    )
    item = _items(1)[0]
    seen: list = []
    outcome = execute_attempt(contender, item, repeat=0, run_id="r1", on_attempt=seen.append)
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
    outcome = execute_attempt(contender, item, repeat=0, stop_check=lambda: "hard cap exceeded")
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
    outcome = execute_attempt(
        contender, item, repeat=0, sleep=lambda _s: None, stop_check=stop_check
    )
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
    assert manifest["protocol_version"] == "v3"
    assert manifest["protocol"]["protocol_version"] == "v3"
    assert manifest["protocol"]["latency_scope"] == "decision"
    rows = [json.loads(line) for line in (run_dir / "results.jsonl").read_text().splitlines()]
    assert len(rows) == 40
    assert all(row["latency_scope"] == "decision" for row in rows)
    assert all(row["choice_index"] == 0 for row in rows)
    raw_files = sorted((run_dir / "raw").glob("*.jsonl"))
    # One decision log plus one attempts log for the one cell.
    assert [f.name for f in raw_files] == [
        "mock.mocksuite.attempts.jsonl",
        "mock.mocksuite.jsonl",
        "negotiation.attempts.jsonl",
    ]
    attempts = [
        json.loads(line)
        for line in (run_dir / "raw" / "mock.mocksuite.attempts.jsonl").read_text().splitlines()
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
        replies=[
            malformed,
            Decision(choice_index=0, confidence=0.9, input_tokens=100, output_tokens=10),
        ],
    )
    spec = _spec(tmp_path, contender, repeats=1, item_limit=1)
    run_dir, manifest = run_grid(spec, repo_root=tmp_path)
    price = price_for("openai:gpt-5.4-nano")
    expected = price.cost_usd(50, 5) + price.cost_usd(100, 10)
    assert manifest["spend_usd"] == pytest.approx(expected)
    attempts = (
        (run_dir / "raw" / "openai__gpt-5.4-nano.mocksuite.attempts.jsonl").read_text().splitlines()
    )
    assert len(attempts) == 2
    rows = (run_dir / "results.jsonl").read_text().splitlines()
    assert len(rows) == 1
    row = json.loads(rows[0])
    assert row["retries"] == 1
    assert row["input_tokens"] == 100  # final decision usage in the row


def test_gold_correctness_in_rows(tmp_path):
    contender = MockContender(
        name="mock", replies=[Decision(choice_index=i % 3, confidence=0.9) for i in range(20)]
    )
    spec = _spec(tmp_path, contender, repeats=1)
    run_dir, _manifest = run_grid(spec, repo_root=tmp_path)
    rows = [json.loads(line) for line in (run_dir / "results.jsonl").read_text().splitlines()]
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
    contender = MockContender(
        name="openai:gpt-5.4-nano",
        replies=[
            Decision(choice_index=0, confidence=0.5, input_tokens=500_000, output_tokens=100_000)
        ],
    )
    spec = _spec(tmp_path, contender, repeats=1, hard_cap_usd=0.01)
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
    assert manifest["budget_overshoot_usd"] == pytest.approx(manifest["spend_usd"] - 0.01)


def test_auth_failure_stops_provider_lane(tmp_path):
    """An auth failure stops that provider's lane; later cells are skipped."""
    broken = MockContender(name="openai:gpt-5.4-nano", replies=[AuthError("invalid key")])
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
    assert all(c["expected_rows"] == 20 for c in manifest["cells"])
    assert manifest["status"] == "stopped_auth"
    assert exit_code_for(manifest) == EXIT_AUTH
    # Failed decisions already in flight when the lane stopped are kept;
    # none is counted as a wrong answer.
    rows = [json.loads(line) for line in (run_dir / "results.jsonl").read_text().splitlines()]
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


def test_stop_arriving_during_backoff_suppresses_retry():
    contender = MockContender(replies=[RateLimited("retry"), Decision(choice_index=0)])
    stopped = False

    def sleep(_seconds):
        nonlocal stopped
        stopped = True

    outcome = execute_attempt(
        contender,
        _items(1)[0],
        0,
        sleep=sleep,
        stop_check=lambda: "budget stop" if stopped else None,
    )
    assert len(contender.calls) == 1
    assert len(outcome.attempts) == 1
    assert "retry suppressed" in outcome.decision.error


def test_builtin_timeout_is_recorded_and_retried():
    class TimedOut(MockContender):
        def _decide(self, state, options):
            raise TimeoutError("test timeout")

    seen = []
    outcome = execute_attempt(
        TimedOut(),
        _items(1)[0],
        0,
        sleep=lambda _s: None,
        on_attempt=seen.append,
    )
    assert [attempt.outcome for attempt in seen] == ["transport", "transport"]
    assert not outcome.decision.ok
    assert outcome.decision.retries == 1


@pytest.mark.parametrize("interrupt_kind", ["signal", "exception"])
def test_interrupt_stops_new_calls_and_finalizes_after_drain(tmp_path, monkeypatch, interrupt_kind):
    import signal
    import threading

    import dmb.runner as runner

    started, release = threading.Event(), threading.Event()

    class Gated(MockContender):
        def _decide(self, state, options):
            started.set()
            assert release.wait(5)
            return super()._decide(state, options)

    contender = Gated(
        name="openai:gpt-5.4-nano",
        replies=[
            Decision(choice_index=0, confidence=0.8, input_tokens=1000, output_tokens=10),
        ],
    )
    spec = _spec(tmp_path, contender, concurrency=1)
    save_items(_items(), tmp_path / "data" / "suites" / "second.jsonl")
    spec.suites.append("second")
    old_handler = signal.getsignal(signal.SIGINT)
    original_wait = runner.wait
    interrupted = False

    def interrupt_wait(*args, **kwargs):
        nonlocal interrupted
        if threading.current_thread() is threading.main_thread() and not interrupted:
            assert started.wait(5)
            interrupted = True
            if interrupt_kind == "signal":
                signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
                release.set()
            else:
                release.set()
                raise KeyboardInterrupt
        return original_wait(*args, **kwargs)

    monkeypatch.setattr(runner, "wait", interrupt_wait)
    run_dir, manifest = run_grid(spec, tmp_path)
    assert len(contender.calls) == 1
    assert manifest["status"] == "interrupted"
    assert exit_code_for(manifest) == runner.EXIT_INTERRUPTED
    assert manifest["finished_at"]
    assert manifest["spend_usd"] > 0
    assert len((run_dir / "results.jsonl").read_text().splitlines()) == 1
    assert sum(cell["rows"] for cell in manifest["cells"]) == 1
    assert next(c for c in manifest["cells"] if c["suite"] == "second")["status"] == "skipped"
    assert signal.getsignal(signal.SIGINT) == old_handler


def test_persistence_failure_signals_other_lanes_and_preserves_raw(tmp_path, monkeypatch):
    import threading

    import dmb.runner as runner

    healthy_started, failure_stopped = threading.Event(), threading.Event()

    class Healthy(MockContender):
        def _decide(self, state, options):
            healthy_started.set()
            assert failure_stopped.wait(5)
            return super()._decide(state, options)

    good = Healthy(name="good")
    good.provider = "local"
    bad = MockContender(name="bad")
    spec = _spec(tmp_path, good, concurrency=1)
    spec.contenders.append(bad)
    original_write = runner._CellSinks.write_row
    original_stop = runner.StopController.stop_run

    def broken_row(sinks, payload):
        if payload["contender"] == "bad":
            assert healthy_started.wait(5)
            raise OSError("simulated result-stream failure")
        return original_write(sinks, payload)

    def stop(controller, scope, reason):
        original_stop(controller, scope, reason)
        if scope == "execution":
            failure_stopped.set()

    monkeypatch.setattr(runner._CellSinks, "write_row", broken_row)
    monkeypatch.setattr(runner.StopController, "stop_run", stop)
    run_dir, manifest = run_grid(spec, tmp_path)
    assert len(good.calls) == len(bad.calls) == 1
    assert manifest["status"] == "failed"
    assert exit_code_for(manifest) == EXIT_EXECUTION
    cells = {cell["contender"]: cell for cell in manifest["cells"]}
    assert cells["bad"]["status"] == "failed"
    assert cells["good"]["rows"] == 1
    raw = [
        json.loads(line)
        for line in (run_dir / "raw" / "bad.mocksuite.jsonl").read_text().splitlines()
    ]
    assert len(raw) == 1
    assert len(raw[0]["attempts"]) == 1


class ProbeContender(MockContender):
    def __init__(self, records, run_dir=None):
        super().__init__(
            name="openai:gpt-5.4-nano",
            replies=[
                Decision(choice_index=0, confidence=0.8, input_tokens=10, output_tokens=1),
            ],
        )
        self.provider = "openai"
        self.records = records
        self.probes = 0
        self.run_dir = run_dir

    def negotiate(self, before_attempt=None, on_attempt=None):
        if self.run_dir is not None:
            assert json.loads((self.run_dir / "manifest.json").read_text())["status"] == "running"
        attempts = []
        for record in self.records:
            if before_attempt is not None and not before_attempt():
                break
            self.probes += 1
            attempts.append(record)
            if on_attempt is not None:
                on_attempt(record)
        return {
            "attempts": attempts,
            "effective_configuration": self.effective_configuration(),
            "configuration_fingerprint": self.configuration_fingerprint,
        }


def test_probe_cap_stops_more_setup_and_scoring_after_reservation(tmp_path):
    record = {
        "ok": True,
        "input_tokens": 1_000_000,
        "output_tokens": 0,
        "usage_details": {},
        "effective_configuration": {"temperature": 0},
    }
    contender = ProbeContender([record, record], tmp_path / "runs" / "testrun")
    spec = _spec(tmp_path, contender, negotiate=True, hard_cap_usd=0.1)
    run_dir, manifest = run_grid(spec, tmp_path)
    assert contender.probes == 1
    assert contender.calls == []
    assert manifest["negotiation_spend_usd"] == pytest.approx(0.2)
    assert manifest["status"] == "stopped_budget"
    records = [
        json.loads(line)
        for line in (run_dir / "raw" / "negotiation.attempts.jsonl").read_text().splitlines()
    ]
    assert len(records) == 1
    assert records[0]["phase"] == "negotiation"
    assert records[0]["configuration_fingerprint"]
    assert all(cell["status"] == "skipped" for cell in manifest["cells"])


def test_existing_probe_usage_is_checked_before_scoring(tmp_path):
    contender = MockContender(name="openai:gpt-5.4-nano")
    spec = _spec(
        tmp_path,
        contender,
        hard_cap_usd=0.1,
        negotiation_usage={contender.name: {"input_tokens": 1_000_000, "output_tokens": 0}},
    )
    _run_dir, manifest = run_grid(spec, tmp_path)
    assert contender.calls == []
    assert manifest["status"] == "stopped_budget"
    assert manifest["negotiation_spend_usd"] == pytest.approx(0.2)


def test_zero_cap_performs_no_probe_or_decision(tmp_path):
    contender = ProbeContender([{"input_tokens": 100, "output_tokens": 1}])
    spec = _spec(tmp_path, contender, hard_cap_usd=0, negotiate=True)
    _run_dir, manifest = run_grid(spec, tmp_path)
    assert contender.calls == []
    assert contender.probes == 0
    assert manifest["spend_usd"] == 0
    assert manifest["status"] == "stopped_budget"


def test_unknown_probe_and_auth_preserve_provider_scope(tmp_path):
    contender = ProbeContender(
        [
            {
                "ok": False,
                "category": "auth",
                "error": "invalid credentials",
                "input_tokens": None,
                "output_tokens": None,
                "usage_details": {},
            }
        ]
    )
    healthy = MockContender(name="healthy-local")
    spec = _spec(tmp_path, contender, negotiate=True)
    spec.contenders.append(healthy)
    run_dir, manifest = run_grid(spec, tmp_path)
    assert manifest["status"] == "stopped_auth"
    assert contender.calls == []
    assert len(healthy.calls) == 20
    assert manifest["unknown_usage_attempts"][contender.name] == 1
    assert manifest["cost_complete"] is False
    probe = json.loads((run_dir / "raw" / "negotiation.attempts.jsonl").read_text())
    assert probe["category"] == "auth"
    assert probe["usage_complete"] is False


def test_unpriced_paid_model_fails_before_probe_and_reservation(tmp_path):
    from dmb.runner import RunConfigurationError

    contender = ProbeContender([])
    contender.name = "cerebras:unpriced-model"
    contender.provider = "cerebras"
    spec = _spec(tmp_path, contender, negotiate=True)
    with pytest.raises(RunConfigurationError, match="no price entry"):
        run_grid(spec, tmp_path)
    assert contender.probes == 0
    assert contender.calls == []
    assert not (tmp_path / "runs" / "testrun").exists()


def test_paid_cache_category_without_rate_stops_before_more_calls(tmp_path, monkeypatch):
    import dmb.runner as runner
    from dmb.prices import Price

    contender = MockContender(
        name="paid:test",
        replies=[
            Decision(
                choice_index=0,
                confidence=0.5,
                input_tokens=10,
                output_tokens=1,
                usage_details={
                    "cache_read_input_tokens": 5,
                    "cache_write_input_tokens": 0,
                    "input_tokens_include_cache": True,
                },
            )
        ],
    )
    contender.provider = "paid"
    monkeypatch.setattr(
        runner,
        "price_for",
        lambda name: Price(
            name,
            "test",
            1.0,
            1.0,
            "2026-09-26",
            "test fixture",
        ),
    )
    spec = _spec(tmp_path, contender, concurrency=1)
    run_dir, manifest = run_grid(spec, tmp_path)
    assert len(contender.calls) == 1
    assert manifest["status"] == "stopped_accounting"
    assert manifest["cost_complete"] is False
    assert manifest["spend_usd"] == pytest.approx(0.000006)
    attempts = [
        json.loads(line)
        for line in (run_dir / "raw" / "paid__test.mocksuite.attempts.jsonl")
        .read_text()
        .splitlines()
    ]
    assert attempts[0]["usage_details"]["cache_read_input_tokens"] == 5
    assert attempts[0]["configuration_fingerprint"] == contender.configuration_fingerprint


@pytest.mark.parametrize(
    "options",
    [
        ["--repeats", "0"],
        ["--repeats", "-1"],
        ["--item-limit", "0"],
        ["--item-limit", "-1"],
        ["--hard-cap", "nan"],
        ["--hard-cap", "inf"],
        ["--hard-cap", "-1"],
        ["--soft-cap", "nan"],
        ["--suites", "s2_spam,s2_spam"],
        ["--suites", "unknown"],
        ["--suites", ""],
        ["--contenders", ""],
        ["--contenders", "openai,"],
        ["--only-jev", "--contenders", "openai"],
    ],
)
def test_cli_rejects_invalid_inputs_before_construction(tmp_path, monkeypatch, options):
    import dmb.cli as cli

    def forbidden(**_kwargs):
        pytest.fail("invalid options must fail before constructing providers")

    monkeypatch.setattr(cli, "build_contenders", forbidden)
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)
    assert cli.main(["run", "--run-id", "invalid", *options]) == EXIT_USAGE
    assert not (tmp_path / "runs").exists()


def test_cli_moves_negotiation_inside_reserved_run(tmp_path, monkeypatch):
    import dmb.cli as cli

    save_items(_items(1), tmp_path / "data" / "suites" / "s2_spam.jsonl")
    contender = ProbeContender(
        [
            {
                "ok": True,
                "input_tokens": 10,
                "output_tokens": 1,
                "usage_details": {},
            }
        ],
        tmp_path / "runs" / "cli-run",
    )

    def construct(**kwargs):
        assert kwargs["negotiate"] is False
        return [contender], [], {}

    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "_majority_setup", lambda suites: ({}, {}))
    monkeypatch.setattr(cli, "build_contenders", construct)
    assert cli.main(["run", "--run-id", "cli-run", "--suites", "s2_spam", "--repeats", "1"]) == 0
    assert contender.probes == 1
    assert len(contender.calls) == 1


def test_billed_malformed_cache_usage_stops_retry_without_known_rate(tmp_path, monkeypatch):
    import dmb.runner as runner
    from dmb.prices import Price

    malformed = MalformedReply("bad decision")
    malformed.attach(
        10,
        1,
        {"body": "bad decision"},
        {
            "cache_read_input_tokens": 5,
            "cache_write_input_tokens": 0,
            "input_tokens_include_cache": True,
        },
    )
    contender = MockContender(name="paid:test", replies=[malformed])
    contender.provider = "paid"
    monkeypatch.setattr(
        runner,
        "price_for",
        lambda name: Price(
            name,
            "test",
            1.0,
            1.0,
            "2026-09-26",
            "test fixture",
        ),
    )
    spec = _spec(tmp_path, contender, concurrency=1)
    run_dir, manifest = run_grid(spec, tmp_path)
    assert len(contender.calls) == 1
    assert manifest["status"] == "stopped_accounting"
    row = json.loads((run_dir / "results.jsonl").read_text())
    assert row["malformed"] is True
    assert row["retries"] == 0
    assert "retry suppressed" in row["error"]


def test_billed_malformed_probe_cache_usage_stops_adaptation(tmp_path, monkeypatch):
    import dmb.runner as runner
    from dmb.prices import Price

    record = {
        "ok": False,
        "category": "schema",
        "input_tokens": 10,
        "output_tokens": 1,
        "usage_details": {
            "cache_read_input_tokens": 5,
            "cache_write_input_tokens": 0,
            "input_tokens_include_cache": True,
        },
    }
    contender = ProbeContender([record, record])
    monkeypatch.setattr(
        runner,
        "price_for",
        lambda name: Price(
            name,
            "test",
            1.0,
            1.0,
            "2026-09-26",
            "test fixture",
        ),
    )
    spec = _spec(tmp_path, contender, negotiate=True)
    _run_dir, manifest = run_grid(spec, tmp_path)
    assert contender.probes == 1
    assert contender.calls == []
    assert manifest["status"] == "stopped_accounting"
    assert manifest["negotiation_spend_usd"] == pytest.approx(0.000006)


def test_interrupt_during_probe_preserves_its_usage_and_stops_scoring(tmp_path):
    import signal

    class InterruptedProbe(ProbeContender):
        def negotiate(self, before_attempt=None, on_attempt=None):
            assert before_attempt()
            self.probes += 1
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            record = {"ok": True, "input_tokens": 1000, "output_tokens": 10}
            on_attempt(record)
            return {"attempts": [record]}

    contender = InterruptedProbe([])
    spec = _spec(tmp_path, contender, negotiate=True)
    run_dir, manifest = run_grid(spec, tmp_path)
    assert contender.probes == 1
    assert contender.calls == []
    assert manifest["status"] == "interrupted"
    assert manifest["negotiation_spend_usd"] > 0
    assert len((run_dir / "raw" / "negotiation.attempts.jsonl").read_text().splitlines()) == 1


def test_lane_exception_stops_peer_before_main_wait_finishes(tmp_path, monkeypatch):
    import threading

    import dmb.runner as runner

    healthy_started, failure_stopped = threading.Event(), threading.Event()

    class Healthy(MockContender):
        def _decide(self, state, options):
            healthy_started.set()
            assert failure_stopped.wait(5)
            return super()._decide(state, options)

    healthy = Healthy(name="healthy")
    healthy.provider = "local"
    broken = MockContender(name="broken")
    spec = _spec(tmp_path, healthy, concurrency=1)
    spec.contenders.append(broken)
    original_cell, original_stop = runner.run_cell, runner.StopController.stop_run

    def run_cell(contender, *args, **kwargs):
        if contender is broken:
            assert healthy_started.wait(5)
            raise OSError("simulated lane persistence failure")
        return original_cell(contender, *args, **kwargs)

    def stop(controller, scope, reason):
        original_stop(controller, scope, reason)
        if scope == "execution":
            failure_stopped.set()

    monkeypatch.setattr(runner, "run_cell", run_cell)
    monkeypatch.setattr(runner.StopController, "stop_run", stop)
    _run_dir, manifest = run_grid(spec, tmp_path)
    assert len(healthy.calls) == 1
    assert manifest["status"] == "failed"
    assert manifest["execution_errors"]
    cell = next(cell for cell in manifest["cells"] if cell["contender"] == broken.name)
    assert cell["status"] == "failed"
    assert cell["expected_rows"] == 20


@pytest.mark.parametrize("input_tokens,output_tokens", [(None, None), (10, None), (None, 1)])
def test_successful_paid_missing_usage_stops_next_decision(tmp_path, input_tokens, output_tokens):
    contender = MockContender(
        name="openai:gpt-5.4-nano",
        replies=[
            Decision(
                choice_index=0,
                confidence=0.8,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        ],
    )
    contender.provider = "openai"
    spec = _spec(tmp_path, contender, concurrency=1)
    run_dir, manifest = run_grid(spec, tmp_path)
    assert len(contender.calls) == 1
    assert manifest["status"] == "stopped_accounting"
    assert manifest["cost_complete"] is False
    assert manifest["unknown_usage_attempts"][contender.name] == 1
    row = json.loads((run_dir / "results.jsonl").read_text())
    assert row["ok"] is True
    assert row["input_tokens"] == input_tokens
    assert row["output_tokens"] == output_tokens
    assert any(stop["scope"] == "accounting" for stop in manifest["stops"])


@pytest.mark.parametrize("input_tokens,output_tokens", [(None, None), (10, None), (None, 1)])
def test_successful_paid_missing_probe_usage_stops_setup(tmp_path, input_tokens, output_tokens):
    record = {
        "ok": True,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "usage_details": {},
    }
    contender = ProbeContender([record, record])
    spec = _spec(tmp_path, contender, negotiate=True)
    run_dir, manifest = run_grid(spec, tmp_path)
    assert contender.probes == 1
    assert contender.calls == []
    assert manifest["status"] == "stopped_accounting"
    assert manifest["cost_complete"] is False
    assert manifest["unknown_usage_attempts"][contender.name] == 1
    probe = json.loads((run_dir / "raw" / "negotiation.attempts.jsonl").read_text())
    assert probe["ok"] is True
    assert probe["usage_complete"] is False
    assert probe["input_tokens"] == input_tokens
    assert probe["output_tokens"] == output_tokens


def test_usage_less_paid_rejection_remains_a_measured_outcome(tmp_path):
    from dmb.contenders.base import ProviderRejected

    contender = MockContender(name="typesafe:jev", replies=[ProviderRejected("choice-count cap")])
    contender.provider = "typesafe"
    spec = _spec(tmp_path, contender, concurrency=1)
    _run_dir, manifest = run_grid(spec, tmp_path)
    assert len(contender.calls) == 20
    assert manifest["status"] == "complete"
    assert manifest["cost_complete"] is False
    assert manifest["unknown_usage_attempts"][contender.name] == 20
    assert not manifest["stops"]
