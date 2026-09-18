"""Grid runner: contenders x suites, protocol-frozen execution.

Protocol (SPEC.md, protocol version v2):
- temperature 0 or provider minimum; no response caching requested.
- concurrency capped at 4 per provider; each provider runs one cell at a
  time, so at most ``CONCURRENCY`` requests per provider are in flight.
- on rate limit: exponential backoff, one retry, then the item counts as
  failed (not wrong). The same single-retry backoff applies to transport
  errors and 5xx responses.
- one retry on a malformed reply (recorded); a second failure counts as
  malformed, not wrong.
- every attempt - including retries, malformed replies, and failures - is
  recorded with its reported usage, elapsed time, and the provider
  response, in ``raw/<contender>.<suite>.attempts.jsonl``.
- decision latency covers the first attempt through the final outcome,
  including retry backoff (``latency_scope: "decision"`` in each row).
- stop scopes: a run stop (budget exhaustion or an execution failure)
  halts every lane; a provider stop (authentication failure) halts that
  provider's lane; a cell stop (suite wall deadline) ends that cell.
  Stopped cells cancel queued work, then collect and persist every
  request already running, so a stop never discards billed work.
- a hard budget cannot prevent charges for requests already running: the
  runner stops new requests and records the observed overshoot.
- negotiation probe calls are billed separately from scored decisions and
  included in total run spend.

Rows, attempt records, and the manifest are persisted during each cell,
so a crashed or stopped run keeps every completed record. The manifest is
replaced atomically and stays readable; its ``status`` stays ``running``
until a terminal status is known.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contenders.base import (
    AuthError,
    Contender,
    ContenderError,
    Decision,
    MalformedReply,
    ProviderRejected,
    RateLimited,
    TransportError,
)
from .contenders.render import prompt_fingerprint
from .prices import Price, price_for, prices_table_hash, snapshot_payload
from .suites.items import DecisionItem, load_items, sha256_file

# Protocol constants.
CONCURRENCY = 4
SUITE_WALL_LIMIT_S = 2000.0
BACKOFF_BASE_S = 2.0
HARD_CAP_USD = 60.0
SOFT_CAP_USD = 40.0
REQUEST_TIMEOUT_S = 120.0
"""Per-request timeout pinned in every adapter; bounds how long draining a
stopped cell can take after the scheduling deadline."""

POLL_S = 0.5
"""Wait granularity for in-flight requests; makes deadlines detectable
even when no request completes."""

PROTOCOL_VERSION = "v2"
RECORD_FORMAT = "dmb-records-2"

# Command-line exit codes, mirrored in README.md.
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_EXECUTION = 3
EXIT_BUDGET = 4
EXIT_AUTH = 5
EXIT_DEADLINE = 6

# Attempt outcomes that get the single protocol retry.
_RETRYABLE = {"malformed", "rate_limited", "transport", "error"}


class RunIdError(ValueError):
    """A run ID is invalid or its directory already exists."""


def validate_run_id(run_id: str) -> str:
    """Reject run IDs that are not one safe directory name."""
    if not run_id or not run_id.strip():
        raise RunIdError("run ID must not be empty")
    if run_id != run_id.strip():
        raise RunIdError(f"run ID {run_id!r} has leading or trailing whitespace")
    if run_id in (".", ".."):
        raise RunIdError(f"run ID {run_id!r} is not a usable directory name")
    if "/" in run_id or "\\" in run_id or os.sep in run_id:
        raise RunIdError(
            f"run ID {run_id!r} must be a single directory name, not a path"
        )
    if Path(run_id).is_absolute() or Path(run_id).name != run_id:
        raise RunIdError(
            f"run ID {run_id!r} must be a single directory name, not a path"
        )
    return run_id


def reserve_run_dir(root: Path, run_id: str) -> Path:
    """Create ``runs/<run_id>`` exclusively; refuse to reuse it.

    Directory creation is atomic, so two competing attempts to reserve one
    run ID cannot both succeed. Existing manifests, results, and raw files
    stay untouched.
    """
    validate_run_id(run_id)
    runs = root / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    run_dir = runs / run_id
    try:
        run_dir.mkdir()
    except FileExistsError as exc:
        raise RunIdError(
            f"run directory {run_dir} already exists; "
            "refusing to overwrite it - use a new run ID"
        ) from exc
    return run_dir


# ---- attempt records --------------------------------------------------------


@dataclass
class AttemptRecord:
    """One provider request: outcome, timing, usage, and response."""

    run_id: str
    contender: str
    provider: str
    suite: str
    item_id: str
    repeat: int
    attempt: int
    """Attempt number within the decision, 1-based."""
    started_at: str
    elapsed_ms: float
    outcome: str
    """ok | malformed | rate_limited | transport | provider_rejected | auth | error."""
    error_category: str | None
    """schema | transport | auth | provider | unknown; None when ok."""
    error: str | None
    input_tokens: int | None
    output_tokens: int | None
    usage_complete: bool
    """True when the attempt reported both token counts."""
    response: Any = None
    """The provider response, when one arrived, for audit. Never headers."""
    request_config: str = PROTOCOL_VERSION

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "contender": self.contender,
            "provider": self.provider,
            "suite": self.suite,
            "item_id": self.item_id,
            "repeat": self.repeat,
            "attempt": self.attempt,
            "started_at": self.started_at,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "outcome": self.outcome,
            "error_category": self.error_category,
            "error": self.error,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "usage_complete": self.usage_complete,
            "response": self.response,
            "request_config": self.request_config,
        }


@dataclass
class AttemptOutcome:
    """What one decision produced: the decision plus its attempts."""

    decision: Decision | None
    """None when a stop signal suppressed the request before it started."""
    attempts: list[AttemptRecord]
    elapsed_ms: float
    """First attempt through final outcome, including retry backoff."""
    skipped_reason: str | None = None

    @property
    def skipped(self) -> bool:
        return self.decision is None


StopCheck = Callable[[], "str | None"]
AttemptSink = Callable[[AttemptRecord], None]


def execute_attempt(
    contender: Contender,
    item: DecisionItem,
    repeat: int,
    *,
    run_id: str = "",
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    stop_check: StopCheck | None = None,
    on_attempt: AttemptSink | None = None,
) -> AttemptOutcome:
    """Run one item through one contender with the frozen retry protocol.

    At most two attempts. Every completed attempt goes to ``on_attempt``
    exactly once, with its own timing and whatever usage the adapter
    reported - including attempts that failed. ``AuthError`` becomes a
    recorded terminal attempt; the caller stops the provider lane.

    ``stop_check`` is consulted before each request and retry. After a
    stop, no new request starts; an already-failed first attempt stands as
    the final outcome.
    """
    started = monotonic()
    attempts: list[AttemptRecord] = []

    def stop_reason() -> str | None:
        return stop_check() if stop_check is not None else None

    def record(
        attempt_no: int,
        outcome: str,
        category: str | None,
        error: str | None,
        attempt_started: tuple[float, float],
        usage: tuple[int | None, int | None],
        response: Any = None,
    ) -> AttemptRecord:
        mono_start, wall_start = attempt_started
        rec = AttemptRecord(
            run_id=run_id,
            contender=contender.name,
            provider=contender.provider,
            suite=item.suite,
            item_id=item.item_id,
            repeat=repeat,
            attempt=attempt_no,
            started_at=_iso(wall_start),
            elapsed_ms=(monotonic() - mono_start) * 1000.0,
            outcome=outcome,
            error_category=category,
            error=_clip(error),
            input_tokens=usage[0],
            output_tokens=usage[1],
            usage_complete=usage[0] is not None and usage[1] is not None,
            response=response,
        )
        attempts.append(rec)
        if on_attempt is not None:
            on_attempt(rec)
        return rec

    def err_usage(exc: ContenderError) -> tuple[int | None, int | None]:
        return exc.input_tokens, exc.output_tokens

    if stop_reason() is not None:
        return AttemptOutcome(
            decision=None,
            attempts=[],
            elapsed_ms=0.0,
            skipped_reason=stop_reason(),
        )

    for attempt_no in (1, 2):
        if attempt_no == 2:
            reason = stop_reason()
            if reason is not None:
                # A stop signal arrived; do not start the retry (and do not
                # sleep first). The failed first attempt stands as the
                # final outcome.
                first = attempts[-1]
                return AttemptOutcome(
                    decision=_failed_decision(first, suppressed=reason),
                    attempts=attempts,
                    elapsed_ms=(monotonic() - started) * 1000.0,
                )
            prior = attempts[-1].outcome if attempts else None
            if prior in ("rate_limited", "transport", "error"):
                sleep(BACKOFF_BASE_S * 2 ** (attempt_no - 1))
        attempt_started = (monotonic(), time.time())
        try:
            decision = contender.decide(item.state, item.options)
        except AuthError as exc:
            record(
                attempt_no, "auth", exc.category, str(exc), attempt_started,
                err_usage(exc), getattr(exc, "response", None),
            )
            return AttemptOutcome(
                decision=_failed_decision(attempts[-1]),
                attempts=attempts,
                elapsed_ms=(monotonic() - started) * 1000.0,
            )
        except MalformedReply as exc:
            record(
                attempt_no, "malformed", exc.category, str(exc), attempt_started,
                err_usage(exc), exc.response if exc.response is not None else exc.raw,
            )
            if attempt_no == 1:
                continue  # one retry on malformed, recorded
            return AttemptOutcome(
                decision=_failed_decision(attempts[-1]),
                attempts=attempts,
                elapsed_ms=(monotonic() - started) * 1000.0,
            )
        except RateLimited as exc:
            record(
                attempt_no, "rate_limited", exc.category, str(exc), attempt_started,
                err_usage(exc), getattr(exc, "response", None),
            )
            if attempt_no == 1:
                continue
            return AttemptOutcome(
                decision=_failed_decision(attempts[-1]),
                attempts=attempts,
                elapsed_ms=(monotonic() - started) * 1000.0,
            )
        except ProviderRejected as exc:
            # A measured outcome (for example jev's 255-choice cap), not a
            # transport fault: no retry, the row records the rejection.
            record(
                attempt_no, "provider_rejected", exc.category, str(exc), attempt_started,
                err_usage(exc), getattr(exc, "response", None),
            )
            return AttemptOutcome(
                decision=_failed_decision(attempts[-1]),
                attempts=attempts,
                elapsed_ms=(monotonic() - started) * 1000.0,
            )
        except (TransportError, TimeoutError, ContenderError) as exc:
            record(
                attempt_no,
                "transport" if isinstance(exc, TransportError) else "error",
                exc.category,
                str(exc),
                attempt_started,
                err_usage(exc),
                getattr(exc, "response", None),
            )
            if attempt_no == 1:
                continue
            return AttemptOutcome(
                decision=_failed_decision(attempts[-1]),
                attempts=attempts,
                elapsed_ms=(monotonic() - started) * 1000.0,
            )
        record(
            attempt_no,
            "ok" if decision.ok else "error",
            None,
            decision.error,
            attempt_started,
            (decision.input_tokens, decision.output_tokens),
            decision.raw.get("response"),
        )
        if attempt_no == 2:
            decision = decision.model_copy(update={"retries": 1})
        return AttemptOutcome(
            decision=decision,
            attempts=attempts,
            elapsed_ms=(monotonic() - started) * 1000.0,
        )

    raise AssertionError("unreachable")


def _failed_decision(record: AttemptRecord, suppressed: str | None = None) -> Decision:
    """Turn a terminal failed attempt into the row's decision."""
    label = {
        "malformed": "malformed",
        "rate_limited": "rate limited",
        "transport": "transport failure",
        "error": "failure",
        "provider_rejected": "provider rejected",
        "auth": "authentication failed",
    }.get(record.outcome, record.outcome)
    if suppressed is None and record.attempt > 1:
        label += " after retry"
    detail = f"{label}: {record.error}" if record.error else label
    if suppressed is not None:
        detail += f" (retry suppressed after stop: {suppressed})"
    return Decision(
        ok=False,
        malformed=record.outcome == "malformed",
        error=_clip(detail),
        retries=max(0, record.attempt - 1),
    )


def _clip(text: str | None, limit: int = 500) -> str | None:
    if text is None:
        return None
    return text if len(text) <= limit else text[:limit] + "..."


# ---- spend accounting -------------------------------------------------------


@dataclass
class SpendTracker:
    """Live spend accounting from provider usage fields.

    Attempts with unknown usage are counted as unknown, never as a
    measured zero. Recording never raises; the caller checks
    :meth:`over_hard_cap` and sets the stop signal, because a hard budget
    cannot prevent charges for requests already running.
    """

    prices: dict[str, Price]
    hard_cap_usd: float = HARD_CAP_USD
    lock: threading.Lock = field(default_factory=threading.Lock)
    decision_spent_usd: float = 0.0
    negotiation_spent_usd: float = 0.0
    by_contender: dict[str, float] = field(default_factory=dict)
    tokens: dict[str, tuple[int, int]] = field(default_factory=dict)
    unknown_usage: dict[str, int] = field(default_factory=dict)
    known_attempts: int = 0

    @property
    def spent_usd(self) -> float:
        return self.decision_spent_usd + self.negotiation_spent_usd

    def record(
        self,
        contender: str,
        input_tokens: int | None,
        output_tokens: int | None,
        *,
        negotiation: bool = False,
    ) -> None:
        """Add one attempt's usage to the running total."""
        if input_tokens is None or output_tokens is None:
            with self.lock:
                self.unknown_usage[contender] = (
                    self.unknown_usage.get(contender, 0) + 1
                )
            return
        with self.lock:
            price = self.prices.get(contender)
            if price is None:
                return
            cost = price.cost_usd(input_tokens, output_tokens)
            if negotiation:
                self.negotiation_spent_usd += cost
            else:
                self.decision_spent_usd += cost
            self.by_contender[contender] = self.by_contender.get(contender, 0.0) + cost
            in_tok, out_tok = self.tokens.get(contender, (0, 0))
            self.tokens[contender] = (in_tok + input_tokens, out_tok + output_tokens)
            self.known_attempts += 1

    def over_hard_cap(self) -> bool:
        with self.lock:
            return self.spent_usd > self.hard_cap_usd


# ---- stop control -----------------------------------------------------------


@dataclass
class StopController:
    """Run- and provider-scoped stop signals with recorded reasons."""

    hard_cap_usd: float
    lock: threading.Lock = field(default_factory=threading.Lock)
    run_stop: threading.Event = field(default_factory=threading.Event)
    provider_stops: dict[str, threading.Event] = field(default_factory=dict)
    reasons: list[dict] = field(default_factory=list)

    def provider_event(self, provider: str) -> threading.Event:
        with self.lock:
            return self.provider_stops.setdefault(provider, threading.Event())

    def stop_run(self, scope: str, reason: str) -> None:
        """Idempotent: the first run stop reason is the one kept."""
        with self.lock:
            if not self.run_stop.is_set():
                self.reasons.append({"scope": scope, "reason": reason})
            self.run_stop.set()

    def stop_provider(self, provider: str, reason: str) -> None:
        event = self.provider_event(provider)
        with self.lock:
            if not event.is_set():
                self.reasons.append(
                    {"scope": "provider", "provider": provider, "reason": reason}
                )
            event.set()

    def run_reason(self) -> str | None:
        if not self.run_stop.is_set():
            return None
        with self.lock:
            run_scopes = {"budget", "execution"}
            for entry in self.reasons:
                if entry["scope"] in run_scopes:
                    return entry["reason"]
            return self.reasons[0]["reason"] if self.reasons else "run stopped"

    def provider_reason(self, provider: str) -> str | None:
        event = self.provider_stops.get(provider)
        if event is None or not event.is_set():
            return None
        with self.lock:
            for entry in self.reasons:
                if entry["scope"] == "provider" and entry.get("provider") == provider:
                    return entry["reason"]
        return "provider stopped"

    def budget_stopped(self) -> bool:
        with self.lock:
            return any(entry["scope"] == "budget" for entry in self.reasons)

    def budget_stop(self, spent: float) -> None:
        self.stop_run(
            "budget",
            f"hard cap exceeded: ${spent:.2f} > ${self.hard_cap_usd:.2f}",
        )


# ---- run specification ------------------------------------------------------


@dataclass
class RunSpec:
    """One grid run: contenders x suites x repeats."""

    run_id: str
    suites: list[str]
    contenders: list[Contender]
    repeats: int = 3
    concurrency: int = CONCURRENCY
    suite_wall_limit_s: float = SUITE_WALL_LIMIT_S
    hard_cap_usd: float = HARD_CAP_USD
    soft_cap_usd: float = SOFT_CAP_USD
    item_limit: int | None = None
    """Cap items per suite (smoke runs). Applies to the first N items."""
    deviations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    negotiation_usage: dict[str, dict[str, int]] = field(default_factory=dict)
    """Probe-call usage per contender, billed as negotiation spend."""
    majority_provenance: dict[str, dict] = field(default_factory=dict)
    """Where each majority prior came from (suite, option, prior, hash)."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=UTC).isoformat(
        timespec="milliseconds"
    )


# ---- cell execution ---------------------------------------------------------


@dataclass
class _CellSinks:
    """Where a cell persists its records."""

    results_handle: Any
    attempts_handle: Any
    raw_handle: Any
    state_lock: threading.Lock
    cell_lock: threading.Lock = field(default_factory=threading.Lock)

    def write_attempt(self, payload: dict) -> None:
        with self.cell_lock:
            self.attempts_handle.write(json.dumps(payload, default=str) + "\n")
            self.attempts_handle.flush()

    def write_row(self, payload: dict) -> None:
        with self.state_lock:
            self.results_handle.write(json.dumps(payload, default=str) + "\n")
            self.results_handle.flush()


def _decision_row(
    spec: RunSpec, contender: Contender, suite_id: str,
    item: DecisionItem, repeat: int, outcome: AttemptOutcome,
) -> dict:
    decision = outcome.decision
    assert decision is not None
    return {
        "run_id": spec.run_id,
        "contender": contender.name,
        "provider": contender.provider,
        "suite": suite_id,
        "item_id": item.item_id,
        "repeat": repeat,
        "choice_index": decision.choice_index,
        "confidence": decision.confidence,
        "correct": (
            decision.choice_index == item.gold_index
            if (decision.ok and item.gold_index >= 0)
            else False
        ),
        "ok": decision.ok,
        "malformed": decision.malformed,
        "error": _clip(decision.error),
        "retries": decision.retries,
        "latency_ms": round(outcome.elapsed_ms, 3),
        "latency_scope": "decision",
        "input_tokens": decision.input_tokens,
        "output_tokens": decision.output_tokens,
        "gold_index": item.gold_index,
    }


def _raw_record(
    item: DecisionItem, repeat: int, outcome: AttemptOutcome,
) -> dict:
    decision = outcome.decision
    assert decision is not None
    return {
        "item_id": item.item_id,
        "suite": item.suite,
        "repeat": repeat,
        "attempts": [a.as_dict() for a in outcome.attempts],
        "decision": decision.model_dump(exclude={"raw"}),
        "decision_elapsed_ms": round(outcome.elapsed_ms, 3),
        "raw": decision.raw,
    }


def run_cell(
    contender: Contender,
    suite_id: str,
    items: list[DecisionItem],
    spec: RunSpec,
    tracker: SpendTracker,
    controller: StopController,
    sinks: _CellSinks,
) -> dict:
    """Run one contender over one suite; return the manifest cell summary.

    Submits at most ``spec.concurrency`` requests at a time. Checks run,
    provider, and cell stop signals before each submission and retry. On a
    stop, cancels queued work, then collects and persists every request
    already running; the original stop reason stands. Rows, attempt
    records, and raw records are persisted as they complete.
    """
    started = time.monotonic()
    deadline = started + spec.suite_wall_limit_s
    rows = 0
    drained = 0
    execution_failure: str | None = None
    provider_event = controller.provider_event(contender.provider)

    def stop_reason() -> str | None:
        if controller.run_stop.is_set():
            return controller.run_reason()
        if provider_event.is_set():
            return controller.provider_reason(contender.provider)
        if time.monotonic() > deadline:
            return f"suite wall limit {spec.suite_wall_limit_s:.0f}s exceeded"
        return None

    def on_attempt(record: AttemptRecord) -> None:
        # Account first, then decide on stops: the budget signal reflects
        # every recorded charge, including this one.
        tracker.record(
            record.contender, record.input_tokens, record.output_tokens
        )
        if tracker.over_hard_cap():
            controller.budget_stop(tracker.spent_usd)
        if record.outcome == "auth":
            controller.stop_provider(
                record.provider,
                f"authentication failed for {record.contender}: {record.error}",
            )
        sinks.write_attempt(record.as_dict())

    def one(task: tuple[DecisionItem, int]) -> AttemptOutcome:
        item, repeat = task
        return execute_attempt(
            contender,
            item,
            repeat,
            run_id=spec.run_id,
            stop_check=stop_reason,
            on_attempt=on_attempt,
        )

    def collect(outcome: AttemptOutcome, item: DecisionItem, repeat: int) -> None:
        nonlocal rows
        if outcome.skipped:
            return
        sinks.write_row(
            _decision_row(spec, contender, suite_id, item, repeat, outcome)
        )
        with sinks.cell_lock:
            sinks.raw_handle.write(
                json.dumps(_raw_record(item, repeat, outcome), default=str) + "\n"
            )
            sinks.raw_handle.flush()
        rows += 1

    work: deque[tuple[DecisionItem, int]] = deque(
        (item, repeat) for repeat in range(spec.repeats) for item in items
    )

    def failure_from_future(future: Future, task: tuple[DecisionItem, int]) -> str:
        item, repeat = task
        exc = future.exception()
        detail = f"{type(exc).__name__}: {exc}" if exc is not None else "unknown"
        return (
            f"unexpected failure for {contender.name} {suite_id} "
            f"{item.item_id} repeat {repeat}: {detail}"
        )

    with ThreadPoolExecutor(max_workers=spec.concurrency) as pool:
        pending: dict[Future, tuple[DecisionItem, int]] = {}

        def fill() -> None:
            while len(pending) < spec.concurrency and work and stop_reason() is None:
                task = work.popleft()
                pending[pool.submit(one, task)] = task

        fill()
        while pending:
            done, _ = wait(pending, timeout=POLL_S, return_when=FIRST_COMPLETED)
            if not done:
                if stop_reason() is not None:
                    break
                continue
            for future in done:
                task = pending.pop(future)
                if future.exception() is not None:
                    execution_failure = failure_from_future(future, task)
                    controller.stop_run("execution", execution_failure)
                    break
                collect(future.result(), *task)
            if execution_failure is not None or stop_reason() is not None:
                break
            fill()

        # Drain: cancel work that has not started, then collect every
        # request already running. Their charges are already recorded.
        for future in pending:
            future.cancel()
        for future, task in pending.items():
            if future.cancelled():
                continue
            try:
                outcome = future.result()
            except Exception:  # noqa: BLE001 - recorded as execution failure
                if execution_failure is None:
                    execution_failure = failure_from_future(future, task)
                    controller.stop_run("execution", execution_failure)
                continue
            drained += 1
            collect(outcome, *task)

    wall_s = time.monotonic() - started
    expected = len(items) * spec.repeats
    if execution_failure is not None:
        status, abort_reason = "failed", execution_failure
    elif controller.run_stop.is_set():
        reason = controller.run_reason() or "run stopped"
        if controller.budget_stopped():
            status, abort_reason = "budget", reason
        else:
            status, abort_reason = "stopped", reason
    elif provider_event.is_set():
        status = "auth_error"
        abort_reason = controller.provider_reason(contender.provider)
    elif rows < expected:
        status, abort_reason = "dnf", (
            f"suite wall limit {spec.suite_wall_limit_s:.0f}s exceeded"
        )
    else:
        status, abort_reason = "ok", None
    return {
        "contender": contender.name,
        "provider": contender.provider,
        "suite": suite_id,
        "status": status,
        "abort_reason": abort_reason,
        "items": len(items),
        "repeats": spec.repeats,
        "rows": rows,
        "expected_rows": expected,
        "drained_rows": drained,
        "wall_s": round(wall_s, 2),
    }


# ---- grid execution ---------------------------------------------------------


@dataclass
class GridState:
    """Shared, lock-guarded grid bookkeeping."""

    manifest: dict
    run_dir: Path
    cells: list[dict] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def write_manifest(self) -> None:
        """Replace the manifest atomically; it stays readable at all times."""
        payload = json.dumps(self.manifest, indent=2, default=str) + "\n"
        tmp = self.run_dir / "manifest.json.tmp"
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.run_dir / "manifest.json")

    def record_cell(self, cell: dict) -> None:
        with self.lock:
            self.cells.append(cell)
            self.manifest["cells"] = sorted(
                self.cells, key=lambda c: (c["contender"], c["suite"])
            )
            self.write_manifest()


def run_grid(spec: RunSpec, repo_root: Path | None = None) -> tuple[Path, dict]:
    """Execute the full grid and write ``runs/<run_id>/`` with a manifest.

    Contenders of one provider run sequentially in a lane; lanes run in
    parallel. At most ``spec.concurrency`` requests are in flight per
    provider at any time. Returns ``(run_dir, manifest)``; the manifest's
    ``status`` and ``exit_code`` fields give the terminal outcome.
    """
    root = repo_root or Path(__file__).resolve().parents[2]
    run_dir = reserve_run_dir(root, spec.run_id)
    results_path = run_dir / "results.jsonl"

    prices: dict[str, Price] = {}
    for contender in spec.contenders:
        try:
            prices[contender.name] = price_for(contender.name)
        except KeyError:
            spec.notes.append(
                f"no price entry for {contender.name}; spend not tracked for it"
            )
    tracker = SpendTracker(prices=prices, hard_cap_usd=spec.hard_cap_usd)
    controller = StopController(hard_cap_usd=spec.hard_cap_usd)

    item_files: dict[str, str] = {}
    for suite_id in spec.suites:
        item_files[suite_id] = sha256_file(root / "data" / "suites" / f"{suite_id}.jsonl")

    manifest = {
        "run_id": spec.run_id,
        "status": "running",
        "created_at": _now_iso(),
        "protocol_version": PROTOCOL_VERSION,
        "record_format": RECORD_FORMAT,
        "protocol": {
            "temperature": 0,
            "concurrency_per_provider": spec.concurrency,
            "repeats": spec.repeats,
            "malformed_retry": 1,
            "rate_limit_backoff": f"exponential from {BACKOFF_BASE_S}s, 1 retry",
            "transport_retry": 1,
            "suite_wall_limit_s": spec.suite_wall_limit_s,
            "request_timeout_s": REQUEST_TIMEOUT_S,
            "hard_cap_usd": spec.hard_cap_usd,
            "soft_cap_usd": spec.soft_cap_usd,
            "item_limit": spec.item_limit,
            "latency_scope": "decision",
            "prompt_sha256": prompt_fingerprint(),
        },
        "confidence_definition": (
            "Confidence is the contender's probability that its chosen option "
            "is correct. Language-model contenders receive this definition in "
            "the prompt; jev's native confidence field is provider-defined "
            "and not documented as that probability."
        ),
        "contenders": [
            {
                "name": c.name,
                "provider": c.provider,
                "notes": list(getattr(c, "notes", [])),
            }
            for c in spec.contenders
        ],
        "suites": spec.suites,
        "item_files": item_files,
        "majority_prior": spec.majority_provenance,
        "price_table_sha256": prices_table_hash(),
        "library_versions": _library_versions(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "deviations": spec.deviations,
        "notes": list(spec.notes),
        "cells": [],
        "skipped": [],
        "stops": [],
    }
    (run_dir / "prices.snapshot.json").write_text(
        json.dumps(snapshot_payload(), indent=2) + "\n", encoding="utf-8"
    )
    state = GridState(manifest=manifest, run_dir=run_dir)
    state.write_manifest()

    for name, usage in spec.negotiation_usage.items():
        tracker.record(
            name, usage.get("input_tokens"), usage.get("output_tokens"),
            negotiation=True,
        )

    lanes: dict[str, list[Contender]] = {}
    for contender in spec.contenders:
        lanes.setdefault(contender.provider, []).append(contender)

    def skipped_cell(
        contender: Contender, suite_id: str, reason: str, kind: str
    ) -> dict:
        items = load_items(root / "data" / "suites" / f"{suite_id}.jsonl")
        if spec.item_limit is not None:
            items = items[: spec.item_limit]
        return {
            "contender": contender.name,
            "provider": contender.provider,
            "suite": suite_id,
            "status": "skipped",
            "skip_kind": kind,
            "abort_reason": reason,
            "items": len(items),
            "repeats": spec.repeats,
            "rows": 0,
            "expected_rows": len(items) * spec.repeats,
            "drained_rows": 0,
            "wall_s": 0.0,
        }

    def run_lane(provider: str, members: list[Contender]) -> None:
        provider_event = controller.provider_event(provider)
        for contender in members:
            for suite_id in spec.suites:
                if controller.run_stop.is_set():
                    state.record_cell(skipped_cell(
                        contender, suite_id,
                        controller.run_reason() or "run stopped", "run_stop",
                    ))
                    continue
                if provider_event.is_set():
                    state.record_cell(skipped_cell(
                        contender, suite_id,
                        controller.provider_reason(provider) or "provider stopped",
                        "provider_stop",
                    ))
                    continue
                items = load_items(root / "data" / "suites" / f"{suite_id}.jsonl")
                if spec.item_limit is not None:
                    items = items[: spec.item_limit]
                raw_dir = run_dir / "raw"
                raw_dir.mkdir(parents=True, exist_ok=True)
                safe = contender.name.replace(":", "__")
                with (raw_dir / f"{safe}.{suite_id}.jsonl").open(
                    "a", encoding="utf-8"
                ) as raw_handle, (raw_dir / f"{safe}.{suite_id}.attempts.jsonl").open(
                    "a", encoding="utf-8"
                ) as attempts_handle, results_path.open("a", encoding="utf-8") as rh:
                    sinks = _CellSinks(
                        results_handle=rh,
                        attempts_handle=attempts_handle,
                        raw_handle=raw_handle,
                        state_lock=state.lock,
                    )
                    cell = run_cell(
                        contender, suite_id, items, spec, tracker, controller, sinks
                    )
                state.record_cell(cell)

    lane_errors: list[dict] = []
    with results_path.open("w", encoding="utf-8"), ThreadPoolExecutor(
        max_workers=max(1, len(lanes))
    ) as lane_pool:
        lane_futures = {
            lane_pool.submit(run_lane, provider, members): provider
            for provider, members in lanes.items()
        }
        for future in lane_futures:
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001 - lane failure stops the run
                provider = lane_futures[future]
                detail = f"lane {provider} failed: {type(exc).__name__}: {exc}"
                lane_errors.append({"provider": provider, "error": detail})
                controller.stop_run("execution", detail)

    manifest["finished_at"] = _now_iso()
    manifest["stops"] = controller.reasons
    manifest["execution_errors"] = lane_errors
    manifest["spend_usd"] = round(tracker.spent_usd, 4)
    manifest["decision_spend_usd"] = round(tracker.decision_spent_usd, 4)
    manifest["negotiation_spend_usd"] = round(tracker.negotiation_spent_usd, 4)
    manifest["spend_by_contender_usd"] = {
        k: round(v, 4) for k, v in tracker.by_contender.items()
    }
    manifest["tokens_by_contender"] = {
        k: {"input": v[0], "output": v[1]} for k, v in tracker.tokens.items()
    }
    manifest["unknown_usage_attempts"] = dict(tracker.unknown_usage)
    manifest["total_cell_wall_s"] = round(
        sum(c.get("wall_s", 0.0) for c in state.cells), 2
    )
    if controller.budget_stopped():
        manifest["budget_overshoot_usd"] = round(
            tracker.spent_usd - spec.hard_cap_usd, 4
        )

    status, exit_code = _terminal_status(manifest)
    manifest["status"] = status
    manifest["exit_code"] = exit_code
    state.write_manifest()
    return run_dir, manifest


def _terminal_status(manifest: dict) -> tuple[str, int]:
    """One terminal status by precedence, plus its command-line exit code."""
    if manifest.get("execution_errors") or any(
        c.get("status") == "failed" for c in manifest.get("cells", [])
    ):
        return "failed", EXIT_EXECUTION
    reasons = manifest.get("stops", [])
    if any(r["scope"] == "budget" for r in reasons):
        return "stopped_budget", EXIT_BUDGET
    if any(
        c.get("status") == "auth_error" or c.get("skip_kind") == "provider_stop"
        for c in manifest.get("cells", [])
    ):
        return "stopped_auth", EXIT_AUTH
    if any(c.get("status") == "dnf" for c in manifest.get("cells", [])):
        return "stopped_deadline", EXIT_DEADLINE
    return "complete", EXIT_OK


def exit_code_for(manifest: dict) -> int:
    """Exit code recorded in a terminal manifest (0 for a running one)."""
    return int(manifest.get("exit_code", EXIT_OK))


def _library_versions() -> dict[str, str]:
    import httpx
    import numpy
    import pydantic

    from . import __version__

    return {
        "dmb": __version__,
        "httpx": httpx.__version__,
        "pydantic": pydantic.VERSION,
        "numpy": numpy.__version__,
    }
