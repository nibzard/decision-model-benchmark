"""Grid runner: contenders x suites, protocol-frozen execution.

Protocol (SPEC.md, frozen before the full run):
- temperature 0 or provider minimum; no response caching requested.
- concurrency capped at 4 per provider.
- on rate limit: exponential backoff, one retry, then the item counts as
  failed (not wrong). The same single-retry backoff applies to transport
  errors and 5xx responses.
- one retry on a malformed reply (recorded); a second failure counts as
  malformed, not wrong.
- a contender-suite cell exceeding the wall-time limit is DNF, partial
  results kept.
- spend is tracked live from usage fields; the run aborts at the hard cap.

Rows and the manifest are flushed after every cell, so a crashed or aborted
run keeps its partial results.
"""

from __future__ import annotations

import json
import platform
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .contenders.base import (
    AuthError,
    Contender,
    ContenderError,
    Decision,
    MalformedReply,
    RateLimited,
    TransportError,
)
from .prices import Price, price_for, prices_table_hash
from .suites.items import DecisionItem, load_items, sha256_file

# Protocol constants.
CONCURRENCY = 4
SUITE_WALL_LIMIT_S = 2000.0
BACKOFF_BASE_S = 2.0
HARD_CAP_USD = 60.0
SOFT_CAP_USD = 40.0


class BudgetExceeded(RuntimeError):
    """Raised when live spend crosses the hard cap. Partials are kept."""


@dataclass
class SpendTracker:
    """Live spend accounting from provider usage fields."""

    prices: dict[str, Price]
    hard_cap_usd: float = HARD_CAP_USD
    lock: threading.Lock = field(default_factory=threading.Lock)
    spent_usd: float = 0.0
    by_contender: dict[str, float] = field(default_factory=dict)
    tokens: dict[str, tuple[int, int]] = field(default_factory=dict)

    def record(self, contender: str, input_tokens: int | None, output_tokens: int | None) -> None:
        """Add one call's usage to the running total and enforce the cap."""
        if input_tokens is None or output_tokens is None:
            return
        with self.lock:
            price = self.prices.get(contender)
            if price is None:
                return
            cost = price.cost_usd(input_tokens, output_tokens)
            self.spent_usd += cost
            self.by_contender[contender] = self.by_contender.get(contender, 0.0) + cost
            in_tok, out_tok = self.tokens.get(contender, (0, 0))
            self.tokens[contender] = (in_tok + input_tokens, out_tok + output_tokens)
            if self.spent_usd > self.hard_cap_usd:
                raise BudgetExceeded(
                    f"hard cap exceeded: ${self.spent_usd:.2f} > ${self.hard_cap_usd:.2f}"
                )


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


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def execute_attempt(
    contender: Contender,
    item: DecisionItem,
    repeat: int,
) -> tuple[Decision, dict]:
    """Run one item through one contender with the frozen retry protocol.

    Returns ``(decision, raw_record)``. The decision is successful, malformed
    (after the one recorded retry), or errored (after the one backoff retry).
    Raises AuthError only: a dead key dooms every later call, so the caller
    aborts the whole contender.
    """
    attempts: list[dict] = []

    for attempt in (1, 2):
        if attempt == 2 and attempts and attempts[-1]["outcome"] in (
            "rate_limited", "transport",
        ):
            time.sleep(BACKOFF_BASE_S * 2 ** (attempt - 1))
        try:
            decision = contender.decide(item.state, item.options)
        except MalformedReply as exc:
            attempts.append({"attempt": attempt, "outcome": "malformed", "detail": str(exc)})
            if attempt == 1:
                continue  # one retry on malformed, recorded
            return _finish(item, repeat, attempts, Decision(
                choice_index=None, confidence=None, ok=False, malformed=True,
                error=f"malformed after retry: {exc}", retries=1,
            ))
        except RateLimited as exc:
            attempts.append({"attempt": attempt, "outcome": "rate_limited", "detail": str(exc)})
            if attempt == 1:
                continue
            return _finish(item, repeat, attempts, Decision(
                ok=False, error=f"rate limited after retry: {exc}", retries=1,
            ))
        except AuthError:
            raise
        except (TransportError, TimeoutError, ContenderError) as exc:
            attempts.append({"attempt": attempt, "outcome": "transport", "detail": str(exc)})
            if attempt == 1:
                continue
            return _finish(item, repeat, attempts, Decision(
                ok=False, error=f"transport failure after retry: {exc}", retries=1,
            ))
        if attempt == 2:
            decision = decision.model_copy(update={"retries": 1})
        attempts.append({
            "attempt": attempt,
            "outcome": "ok" if decision.ok else "error",
            "latency_ms": round(decision.latency_ms, 3),
        })
        return _finish(item, repeat, attempts, decision)

    raise AssertionError("unreachable")


def _finish(
    item: DecisionItem, repeat: int, attempts: list[dict], decision: Decision
) -> tuple[Decision, dict]:
    raw_record = {
        "item_id": item.item_id,
        "suite": item.suite,
        "repeat": repeat,
        "attempts": attempts,
        "decision": decision.model_dump(exclude={"raw"}),
        "raw": decision.raw,
    }
    return decision, raw_record


def run_cell(
    contender: Contender,
    suite_id: str,
    items: list[DecisionItem],
    spec: RunSpec,
    tracker: SpendTracker,
    raw_path: Path,
) -> tuple[dict, list[dict]]:
    """Run one contender over one suite. Returns ``(cell_summary, rows)``.

    Writes the raw attempt log to ``raw_path`` (one JSON object per attempt).
    """
    started = time.monotonic()
    deadline = started + spec.suite_wall_limit_s
    rows: list[dict] = []
    raw_records: list[dict] = []
    status = "ok"
    abort_reason: str | None = None
    lock = threading.Lock()
    work = [(item, repeat) for repeat in range(spec.repeats) for item in items]

    def one(task: tuple[DecisionItem, int]) -> tuple[DecisionItem, int, Decision, dict]:
        item, repeat = task
        decision, raw_record = execute_attempt(contender, item, repeat)
        return item, repeat, decision, raw_record

    with ThreadPoolExecutor(max_workers=spec.concurrency) as pool:
        futures = {pool.submit(one, task): task for task in work}
        try:
            for future in as_completed(futures):
                item, repeat, decision, raw_record = future.result()
                tracker.record(
                    contender.name, decision.input_tokens, decision.output_tokens
                )
                row = {
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
                    "error": decision.error,
                    "retries": decision.retries,
                    "latency_ms": decision.latency_ms,
                    "input_tokens": decision.input_tokens,
                    "output_tokens": decision.output_tokens,
                    "gold_index": item.gold_index,
                }
                with lock:
                    rows.append(row)
                    raw_records.append(raw_record)
                if time.monotonic() > deadline:
                    status = "dnf"
                    abort_reason = (
                        f"suite wall limit {spec.suite_wall_limit_s:.0f}s exceeded"
                    )
                    for f in futures:
                        f.cancel()
                    break
        except BudgetExceeded as exc:
            status = "budget"
            abort_reason = str(exc)
            for f in futures:
                f.cancel()

    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with raw_path.open("w", encoding="utf-8") as handle:
        for record in raw_records:
            handle.write(json.dumps(record, default=str) + "\n")

    wall_s = time.monotonic() - started
    cell = {
        "contender": contender.name,
        "suite": suite_id,
        "status": status,
        "abort_reason": abort_reason,
        "items": len(items),
        "repeats": spec.repeats,
        "rows": len(rows),
        "expected_rows": len(items) * spec.repeats,
        "wall_s": round(wall_s, 2),
    }
    return cell, rows


def run_grid(spec: RunSpec, repo_root: Path | None = None) -> Path:
    """Execute the full grid and write ``runs/<run_id>/`` with a manifest.

    Contenders of one provider run sequentially in a lane; lanes run in
    parallel. At most ``spec.concurrency`` requests are in flight per
    provider at any time (the frozen cap), so lanes never overlap requests
    for the same provider.
    """
    root = repo_root or Path(__file__).resolve().parents[2]
    run_dir = root / "runs" / spec.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
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

    item_files: dict[str, str] = {}
    for suite_id in spec.suites:
        item_files[suite_id] = sha256_file(root / "data" / "suites" / f"{suite_id}.jsonl")

    manifest = {
        "run_id": spec.run_id,
        "created_at": _now_iso(),
        "protocol": {
            "temperature": 0,
            "concurrency_per_provider": spec.concurrency,
            "repeats": spec.repeats,
            "malformed_retry": 1,
            "rate_limit_backoff": f"exponential from {BACKOFF_BASE_S}s, 1 retry",
            "transport_retry": 1,
            "suite_wall_limit_s": spec.suite_wall_limit_s,
            "hard_cap_usd": spec.hard_cap_usd,
            "soft_cap_usd": spec.soft_cap_usd,
            "item_limit": spec.item_limit,
        },
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
        "price_table_sha256": prices_table_hash(),
        "library_versions": _library_versions(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "deviations": spec.deviations,
        "notes": list(spec.notes),
        "cells": [],
        "skipped": [],
    }

    lanes: dict[str, list[Contender]] = {}
    for contender in spec.contenders:
        lanes.setdefault(contender.provider, []).append(contender)

    cells: list[dict] = []
    state_lock = threading.Lock()
    stop_all = threading.Event()

    def flush(cell: dict, rows: list[dict]) -> None:
        """Record one finished cell and rewrite the manifest (under lock)."""
        with state_lock:
            cells.append(cell)
            for row in rows:
                results_handle.write(json.dumps(row, default=str) + "\n")
            results_handle.flush()
            manifest["cells"] = sorted(cells, key=lambda c: (c["contender"], c["suite"]))
            manifest["contenders"] = [
                {
                    "name": c.name,
                    "provider": c.provider,
                    "notes": list(getattr(c, "notes", [])),
                }
                for c in spec.contenders
            ]
            manifest["spend_usd"] = round(tracker.spent_usd, 4)
            manifest["spend_by_contender_usd"] = {
                k: round(v, 4) for k, v in tracker.by_contender.items()
            }
            manifest["tokens_by_contender"] = {
                k: {"input": v[0], "output": v[1]} for k, v in tracker.tokens.items()
            }
            (run_dir / "manifest.json").write_text(
                json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8"
            )

    def run_lane(members: list[Contender]) -> None:
        for contender in members:
            for suite_id in spec.suites:
                if stop_all.is_set():
                    return
                items = load_items(root / "data" / "suites" / f"{suite_id}.jsonl")
                if spec.item_limit is not None:
                    items = items[: spec.item_limit]
                raw_path = (
                    run_dir / "raw" / f"{contender.name.replace(':', '__')}.{suite_id}.jsonl"
                )
                try:
                    cell, rows = run_cell(contender, suite_id, items, spec, tracker, raw_path)
                except AuthError as exc:
                    cell = {
                        "contender": contender.name,
                        "suite": suite_id,
                        "status": "auth_error",
                        "abort_reason": str(exc),
                        "items": len(items),
                        "repeats": spec.repeats,
                        "rows": 0,
                        "expected_rows": len(items) * spec.repeats,
                        "wall_s": 0.0,
                    }
                    rows = []
                flush(cell, rows)
                if cell["status"] == "budget":
                    # Hard cap: the shared tracker now rejects every lane.
                    with state_lock:
                        manifest["skipped"].append({
                            "contender": contender.name,
                            "reason": cell["abort_reason"],
                        })
                    stop_all.set()
                    return
                if cell["status"] == "auth_error":
                    # Lane members share one credential; the key is dead.
                    with state_lock:
                        manifest["skipped"].append({
                            "contender": contender.name,
                            "reason": cell["abort_reason"],
                        })
                    return
                # DNF keeps partials and moves on to the next suite.

    with results_path.open("w", encoding="utf-8") as results_handle:
        threads = [
            threading.Thread(target=run_lane, args=(members,), name=f"dmb-{provider}")
            for provider, members in lanes.items()
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    manifest["finished_at"] = _now_iso()
    total_wall = sum(c["wall_s"] for c in cells)
    manifest["total_cell_wall_s"] = round(total_wall, 2)
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8"
    )
    return run_dir


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
