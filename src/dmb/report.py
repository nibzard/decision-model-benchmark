"""Render the published report from run directories.

Everything here derives from the run directories plus the frozen, hashed
item files: tables, one cardinality plot, one reliability diagram per
contender, and a raw archive. S2 text is omitted under the project's
conservative publication policy; UCI currently identifies the data as CC BY 4.0.

The report is generated, never hand-edited; numbers recompute from
``results.jsonl`` and the manifests. Before aggregation, every supplied
run is verified: item-file hashes must match, conflicting hashes for one
suite are rejected, and rows are validated against the frozen items.
Later runs replace earlier cells whole - a later failed cell replaces an
earlier success instead of falling back to it.

Macro-averaged F1 uses stable class labels (the option texts) on suites
whose items share one option list (S1, S2, S4). S3 code words and S5
alternatives vary between items, so option positions are not classes
there and macro-F1 is not applicable.
"""

from __future__ import annotations

import gzip
import hashlib
import html
import io
import json
import math
import re
import tarfile
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

import numpy as np

from .contenders.jsonmode import usage_details as extract_usage_details
from .metrics import (
    accuracy,
    brier,
    cluster_mean_interval,
    confidence_spread,
    ece,
    flip_rate,
    macro_f1_labeled,
    paired_cluster_difference,
    score_risk_coverage,
)
from .prices import PRICES, Price, load_snapshot, prices_table_hash, snapshot_hash
from .suites.build import SUITE_IDS, SUITE_ORDER
from .suites.items import DecisionItem, load_items, sha256_file

SUITE_TITLES: dict[str, str] = {
    "s1_intent77": "S1 intent77 (77-way banking)",
    "s2_spam": "S2 SMS spam (2-way)",
    "s3_cardinality": "S3 cardinality sweep",
    "s4_order": "S4 order stability",
    "s5_confidence": "S5 forced uncertainty (score diagnostics)",
}

# Suites whose option texts are stable classes across items.
LABEL_SUITES = frozenset({"s1_intent77", "s2_spam", "s4_order"})

# Protocol fields that must agree before runs can be combined. Missing
# values fall back to the historical (v1) behavior.
_PROTOCOL_KEYS = (
    "protocol_version",
    "repeats",
    "temperature",
    "malformed_retry",
    "transport_retry",
    "rate_limit_backoff",
    "latency_scope",
    "prompt_sha256",
)
_V1_DEFAULTS = {
    "protocol_version": "v1",
    "latency_scope": "request",
    "prompt_sha256": None,
}

_REDACTED = "[redacted: S2 publication policy]"

PALETTE = [
    "#2455e4",
    "#d42a2a",
    "#187a3b",
    "#a21caf",
    "#b45309",
    "#0e7490",
    "#5b21b6",
    "#991b1b",
    "#4d7c0f",
    "#be185d",
    "#374151",
    "#7c2d12",
]


class MergeError(ValueError):
    """Runs cannot be combined as supplied; the message says why."""


def _fmt_pct(value: float | None) -> str:
    return "-" if value is None or value != value else f"{100.0 * value:.1f}"


def _fmt_ms(value: float | None) -> str:
    if value is None or value != value:
        return "-"
    return f"{value:,.0f}" if value >= 100 else f"{value:.1f}"


def _fmt_usd(value: float | None) -> str:
    return "-" if value is None or value != value else f"${value:.2f}"


def _fmt_f(value: float | None, digits: int = 3) -> str:
    return "-" if value is None or value != value else f"{value:.{digits}f}"


# ---- loading ---------------------------------------------------------------


@dataclass
class RunData:
    """Rows plus manifest of one run directory."""

    run_id: str
    dir: Path
    manifest: dict
    rows: list[dict]


def load_run(run_dir: Path) -> RunData:
    """Load one run directory: manifest plus every results row."""
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    rows = [
        json.loads(line)
        for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return RunData(run_id=run_dir.name, dir=run_dir, manifest=manifest, rows=rows)


def _protocol_of(manifest: dict) -> dict:
    raw = manifest.get("protocol", {})
    if not isinstance(raw, dict):
        raise MergeError("manifest protocol must be an object")
    protocol = dict(raw)
    outer, inner = manifest.get("protocol_version"), protocol.get("protocol_version")
    if outer is not None and inner is not None and outer != inner:
        raise MergeError("manifest has conflicting top-level and nested protocol_version")
    version = outer if outer is not None else inner
    if version is not None:
        if not isinstance(version, str) or not version:
            raise MergeError("protocol_version must be a nonempty string")
        protocol["protocol_version"] = version
    for key, default in _V1_DEFAULTS.items():
        protocol.setdefault(key, default)
    for key in ("repeats", "item_limit"):
        value = protocol.get(key)
        if value is not None and (type(value) is not int or value <= 0):
            raise MergeError(f"protocol {key} must be a positive integer or null")
    if protocol["latency_scope"] not in {"request", "decision"}:
        raise MergeError("protocol latency_scope must be request or decision")
    return protocol


def _prices_for_run(run: RunData) -> dict[str, Price]:
    """Prices for one run: its stored snapshot, or the current table.

    A run without a snapshot (protocol v1) must have recorded the current
    table's hash; otherwise the current table would silently reprice an
    old report.
    """
    snapshot = run.dir / "prices.snapshot.json"
    if snapshot.exists():
        payload = json.loads(snapshot.read_text(encoding="utf-8"))
        actual = snapshot_hash(payload)
        if actual != run.manifest.get("price_table_sha256"):
            raise MergeError(f"run {run.run_id}: price snapshot hash does not match manifest")
        return load_snapshot(payload)
    recorded = run.manifest.get("price_table_sha256")
    current = prices_table_hash()
    if recorded != current:
        raise MergeError(
            f"run {run.run_id} predates price snapshots and its recorded price "
            f"table hash {str(recorded)[:12]} does not match the current table "
            f"{current[:12]}; refusing to reprice it - pin the old table or "
            "regenerate from a run with a stored snapshot"
        )
    return {p.contender: p for p in PRICES}


def _finite_number(value: object, *, minimum: float = 0.0) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= minimum


def _validate_row(run: RunData, row: dict, item: DecisionItem) -> None:
    """Reject records that could silently alter a metric or denominator."""
    context = f"run {run.run_id} row {item.item_id}"
    for key in ("ok", "malformed"):
        if type(row.get(key)) is not bool:
            raise MergeError(f"{context}: {key} must be boolean")
    if row["ok"] and row["malformed"]:
        raise MergeError(f"{context}: valid and malformed cannot both be true")
    repeat = row.get("repeat")
    repeats = _protocol_of(run.manifest).get("repeats")
    if type(repeat) is not int or repeat < 0:
        raise MergeError(f"{context}: repeat must be a nonnegative integer")
    if repeats is not None and (type(repeats) is not int or repeats <= repeat):
        raise MergeError(f"{context}: repeat is outside the recorded protocol repeats")
    if type(row.get("gold_index")) is not int or row["gold_index"] != item.gold_index:
        raise MergeError(f"{context}: gold does not match frozen item gold {item.gold_index}")
    choice = row.get("choice_index")
    if choice is not None and (type(choice) is not int or not 0 <= choice < len(item.options)):
        raise MergeError(f"{context}: choice {choice} out of range or not an integer")
    confidence = row.get("confidence")
    if confidence is not None and (not _finite_number(confidence) or confidence > 1):
        raise MergeError(f"{context}: confidence must be finite and in [0, 1]")
    if row["ok"] and (choice is None or confidence is None):
        raise MergeError(f"{context}: valid decision requires choice and confidence")
    expected_correct = bool(row["ok"] and item.gold_index >= 0 and choice == item.gold_index)
    if (
        (row.get("correct") is not None and type(row["correct"]) is not bool)
        or (row["ok"] and type(row.get("correct")) is not bool)
        or bool(row.get("correct")) != expected_correct
    ):
        raise MergeError(f"{context}: correct flag disagrees with frozen gold and choice")
    if not _finite_number(row.get("latency_ms")):
        raise MergeError(f"{context}: latency_ms must be finite and nonnegative")
    for key in ("input_tokens", "output_tokens", "retries"):
        value = row.get(key)
        if value is not None and (type(value) is not int or value < 0):
            raise MergeError(f"{context}: {key} must be a nonnegative integer or null")
    for key in ("contender", "suite", "item_id"):
        if not isinstance(row.get(key), str) or not row[key]:
            raise MergeError(f"{context}: {key} must be a nonempty string")


def _verify_configurations(run: RunData) -> None:
    expected = {}
    configurations = list(run.manifest.get("contenders", []))
    setup = run.manifest.get("negotiation", {})
    if isinstance(setup, dict):
        configurations += [
            dict(value, name=name) for name, value in setup.items() if isinstance(value, dict)
        ]
    for entry in configurations:
        name = entry.get("name", entry.get("contender"))
        fingerprint = entry.get("configuration_fingerprint")
        configuration = entry.get("effective_configuration")
        if fingerprint is not None:
            if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
                raise MergeError(f"run {run.run_id}: invalid configuration fingerprint")
            if configuration is not None:
                digest = hashlib.sha256(
                    json.dumps(configuration, sort_keys=True).encode()
                ).hexdigest()
                if digest != fingerprint:
                    raise MergeError(
                        f"run {run.run_id}: configuration hash does not match manifest"
                    )
            if name in expected and expected[name] != fingerprint:
                raise MergeError(f"run {run.run_id}: conflicting final configurations for {name}")
            expected[name] = fingerprint
    for row in run.rows:
        if not isinstance(row, dict):
            continue  # the row validator supplies the actionable error
        fingerprint = expected.get(row.get("contender"))
        if fingerprint and row.get("configuration_fingerprint") != fingerprint:
            raise MergeError(f"run {run.run_id}: row configuration drift from recorded settings")
    for path in (run.dir / "raw").glob("*.attempts.jsonl"):
        if "negotiation" in path.name:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                fingerprint = expected.get(record.get("contender"))
                if fingerprint and record.get("configuration_fingerprint") != fingerprint:
                    raise MergeError(f"run {run.run_id}: attempt configuration drift")


def verify_runs(
    runs: list[RunData],
    root: Path,
    allow_protocol_mix: bool = False,
) -> dict[str, dict[str, DecisionItem]]:
    """Verify every supplied run before anything is combined.

    - Item-file hashes must match each run's manifest.
    - Two runs recording different hashes for one suite are rejected.
    - Every row's suite, item reference, gold label, and prediction bounds
      are validated against the verified frozen items.
    - Protocol versions, latency scopes, and prompt hashes must agree
      unless ``allow_protocol_mix`` is set (the report then records the
      mix visibly).
    """
    if not runs:
        raise MergeError("at least one run is required")
    items_by_suite: dict[str, dict[str, DecisionItem]] = {}
    hashes_by_suite: dict[str, tuple[str, str]] = {}
    for run in runs:
        _verify_configurations(run)
        for suite_id, expected_hash in run.manifest.get("item_files", {}).items():
            if suite_id not in SUITE_ORDER or not isinstance(expected_hash, str):
                raise MergeError(f"run {run.run_id}: invalid item-file manifest entry")
            path = root / "data" / "suites" / f"{suite_id}.jsonl"
            if not path.exists():
                raise MergeError(f"run {run.run_id} references item file {path} which is missing")
            actual = sha256_file(path)
            if actual != expected_hash:
                raise MergeError(
                    f"run {run.run_id}: item file {path} hash {actual[:12]} != "
                    f"manifest {expected_hash[:12]}; the frozen items changed "
                    "after the run"
                )
            known = hashes_by_suite.get(suite_id)
            if known is not None and known[0] != expected_hash:
                raise MergeError(
                    f"suite {suite_id} has conflicting item hashes: run "
                    f"{known[1]} recorded {known[0][:12]}, run {run.run_id} "
                    f"recorded {expected_hash[:12]}; the runs saw different "
                    "items and cannot be combined"
                )
            hashes_by_suite[suite_id] = (expected_hash, run.run_id)
            loaded = load_items(path)
            for item in loaded:
                if not item.options or len(set(item.options)) != len(item.options):
                    raise MergeError(f"suite {suite_id} item {item.item_id}: invalid options")
                if not -1 <= item.gold_index < len(item.options):
                    raise MergeError(f"suite {suite_id} item {item.item_id}: invalid gold")
            items_by_suite[suite_id] = {item.item_id: item for item in loaded}

    protocols = {run.run_id: _protocol_of(run.manifest) for run in runs}
    reference_id = runs[0].run_id
    reference = protocols[reference_id]
    for run_id, protocol in protocols.items():
        for key in _PROTOCOL_KEYS:
            if protocol.get(key) != reference.get(key):
                message = (
                    f"runs {reference_id} and {run_id} disagree on protocol "
                    f"{key} ({reference.get(key)!r} vs {protocol.get(key)!r}); "
                    "their numbers do not share one execution or prompt "
                    "definition"
                )
                if not allow_protocol_mix:
                    raise MergeError(
                        message + " (pass --allow-protocol-mix to "
                        "merge anyway with a recorded note)"
                    )
                break  # one recorded note per run pair is enough

    for run in runs:
        for cell in run.manifest.get("cells", []):
            if not isinstance(cell, dict) or cell.get("suite") not in run.manifest.get(
                "item_files", {}
            ):
                raise MergeError(f"run {run.run_id}: cell suite was not verified for this run")
        for row in run.rows:
            if not isinstance(row, dict):
                raise MergeError(f"run {run.run_id}: result row must be an object")
            suite_id = row.get("suite")
            if not isinstance(suite_id, str) or suite_id not in run.manifest.get("item_files", {}):
                raise MergeError(f"run {run.run_id}: row suite was not verified for this run")
            items = items_by_suite.get(suite_id)
            if items is None:
                raise MergeError(
                    f"run {run.run_id} row {row.get('item_id')!r} references "
                    f"suite {suite_id!r} whose item file was not verified"
                )
            item_id = row.get("item_id")
            item = items.get(item_id) if isinstance(item_id, str) else None
            if item is None:
                raise MergeError(
                    f"run {run.run_id} row references unknown item "
                    f"{row.get('item_id')!r} in suite {suite_id}"
                )
            limit = _protocol_of(run.manifest).get("item_limit")
            if limit is not None and item.item_id not in list(items)[:limit]:
                raise MergeError(f"run {run.run_id}: row exceeds the protocol item_limit")
            _validate_row(run, row, item)
    return items_by_suite


# ---- cell selection ---------------------------------------------------------


@dataclass
class SelectedCell:
    """One contender-suite cell and the run that supplied it."""

    run: RunData
    manifest_cell: dict | None
    rows: list[dict]


def select_cells(runs: list[RunData]) -> dict[tuple[str, str], SelectedCell]:
    """Pick the last supplied run for every cell; replace cells whole.

    A cell belongs to a run when the run's manifest lists it or the run
    has rows for it - so a later failed cell with zero rows still replaces
    an earlier success instead of exposing the earlier score.
    """
    selection: dict[tuple[str, str], SelectedCell] = {}
    for run in runs:
        manifest_cells = {
            (cell.get("contender"), cell.get("suite")): cell
            for cell in run.manifest.get("cells", [])
        }
        rows_by_cell: dict[tuple[str, str], list[dict]] = {}
        for row in run.rows:
            rows_by_cell.setdefault((row.get("contender"), row.get("suite")), []).append(row)
        for key in sorted(set(manifest_cells) | set(rows_by_cell)):
            rows = sorted(
                rows_by_cell.get(key, []), key=lambda row: (row["item_id"], row["repeat"])
            )
            seen: set[tuple[str, int]] = set()
            for row in rows:
                row_key = (row.get("item_id"), row.get("repeat"))
                if row_key in seen:
                    raise MergeError(
                        f"run {run.run_id} cell {key[0]}/{key[1]} has duplicate "
                        f"item-repeat {row_key}; cannot aggregate it"
                    )
                seen.add(row_key)
            selection[key] = SelectedCell(run=run, manifest_cell=manifest_cells.get(key), rows=rows)
    return dict(sorted(selection.items()))


# ---- aggregation -----------------------------------------------------------


@dataclass
class CellMetrics:
    """Aggregates for one contender-suite cell."""

    contender: str
    suite: str
    source_run: str | None = None
    status: str = "ok"
    stop_reason: str | None = None
    n_rows: int = 0
    """Completed decisions."""
    n_items: int = 0
    n_ok: int = 0
    """Valid decisions."""
    n_malformed: int = 0
    n_failed: int = 0
    expected_decisions: int = 0
    completion_coverage: float | None = None
    """Completed decisions / expected decisions."""
    valid_coverage: float | None = None
    """Valid decisions / expected decisions."""
    partial: bool = False
    """The cell stopped early or returned invalid decisions."""
    accuracy: float | None = None
    macro_f1: float | None = None
    macro_f1_applicable: bool = True
    ece: float | None = None
    brier: float | None = None
    latencies: dict[str, float] = field(default_factory=dict)
    latency_scope: str = "request"
    cost_per_1000: float | None = None
    cost_usd: float | None = None
    cost_scope: str | None = None
    """Which attempt charges the cost covers."""
    cost_incomplete_reason: str | None = None
    cost_complete: bool = False
    confidence_semantics: str = "prompted probability of chosen-option correctness"
    calibration_subset: str = "valid gold-labelled decisions"
    accuracy_item_interval: dict = field(default_factory=dict)
    risk_coverage: list[dict] = field(default_factory=list)
    # S4
    flip_rate: float | None = None
    conf_range: float | None = None
    order_stability: dict = field(default_factory=dict)
    # S5
    admits_ignorance: float | None = None
    mean_conf_no_good: float | None = None
    uncertainty_subsets: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "contender": self.contender,
            "suite": self.suite,
            "source_run": self.source_run,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "n_rows": self.n_rows,
            "n_items": self.n_items,
            "n_ok": self.n_ok,
            "n_malformed": self.n_malformed,
            "n_failed": self.n_failed,
            "expected_decisions": self.expected_decisions,
            "completion_coverage": self.completion_coverage,
            "valid_coverage": self.valid_coverage,
            "partial": self.partial,
            "accuracy": self.accuracy,
            "macro_f1": self.macro_f1,
            "macro_f1_applicable": self.macro_f1_applicable,
            "ece": self.ece,
            "brier": self.brier,
            "p50_ms": self.latencies.get("p50_ms"),
            "p95_ms": self.latencies.get("p95_ms"),
            "p99_ms": self.latencies.get("p99_ms"),
            "latency_scope": self.latency_scope,
            "cost_per_1000_usd": self.cost_per_1000,
            "cost_usd": self.cost_usd,
            "cost_scope": self.cost_scope,
            "cost_incomplete_reason": self.cost_incomplete_reason,
            "cost_complete": self.cost_complete,
            "confidence_semantics": self.confidence_semantics,
            "calibration_subset": self.calibration_subset,
            "accuracy_item_interval": self.accuracy_item_interval,
            "risk_coverage": self.risk_coverage,
            "flip_rate": self.flip_rate,
            "mean_conf_range": self.conf_range,
            "order_stability": self.order_stability,
            "admits_ignorance": self.admits_ignorance,
            "mean_conf_no_good": self.mean_conf_no_good,
            "uncertainty_subsets": self.uncertainty_subsets,
            "within_order_flip_rate": self.order_stability.get("within_order_flip_rate"),
            "across_order_flip_rate": self.order_stability.get("across_order_flip_rate"),
            "no_good_ece": self.uncertainty_subsets.get("no_good_option", {}).get("ece"),
            "no_good_brier": self.uncertainty_subsets.get("no_good_option", {}).get("brier"),
            "underdetermined_ece": self.uncertainty_subsets.get("underdetermined", {}).get("ece"),
            "underdetermined_brier": self.uncertainty_subsets.get("underdetermined", {}).get(
                "brier"
            ),
        }


def _attempt_costs(
    run_dir: Path, contender: str, suite_id: str, price: Price
) -> tuple[float | None, int, list[str]]:
    """Cost over every recorded attempt when the run kept attempt logs.

    Returns a known subtotal, observation count, and completeness reasons.
    """
    safe = contender.replace(":", "__")
    path = run_dir / "raw" / f"{safe}.{suite_id}.attempts.jsonl"
    if not path.exists():
        return None, 0, ["attempt records unavailable"]
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        records.append(json.loads(line))
    return _account_records(records, price, contender, legacy=False)


def _record_usage(record: dict, contender: str) -> dict:
    direct = record.get("usage_details")
    if isinstance(direct, dict) and direct:
        return direct
    raw = record.get("raw") or {}
    if not isinstance(raw, dict):
        return {}
    response = raw.get("response", record.get("response"))
    return extract_usage_details(
        response, "anthropic" if contender.startswith("anthropic:") else "openai"
    )


def _account_records(
    records: list[dict],
    price: Price,
    contender: str,
    *,
    legacy: bool,
) -> tuple[float | None, int, list[str]]:
    known_costs, reasons = [], set()
    for record in records:
        inp, out = record.get("input_tokens"), record.get("output_tokens")
        details = _record_usage(record, contender)
        account = price.account_usage(inp, out, details)
        amount = account.known_cost_usd
        if legacy and not details and amount is not None:
            # Without the old provider usage object, input totals cannot
            # establish inclusive cache counts (or extra Anthropic cache use).
            amount = price.cost_usd(inp if contender.startswith("anthropic:") else 0, out)
            reasons.add("historical cache usage details unavailable; input/cache charge incomplete")
        if amount is not None:
            known_costs.append(amount)
        if not account.complete:
            reasons.add(account.reason or "unknown usage charge")
    return (math.fsum(known_costs) if known_costs else None, len(records), sorted(reasons))


def _legacy_cost_rows(selected: SelectedCell, contender: str, suite_id: str) -> list[dict]:
    path = selected.run.dir / "raw" / f"{contender.replace(':', '__')}.{suite_id}.jsonl"
    usage_by_row = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            usage_by_row[(record.get("item_id"), record.get("repeat", 0))] = _record_usage(
                record, contender
            )
    return [
        dict(
            r,
            usage_details=r.get("usage_details")
            or usage_by_row.get((r["item_id"], r["repeat"]), {}),
        )
        for r in selected.rows
    ]


def aggregate_cell(
    contender: str,
    suite_id: str,
    rows: list[dict],
    items: dict[str, DecisionItem],
    *,
    manifest_cell: dict | None = None,
    source_run: str | None = None,
    prices: dict[str, Price] | None = None,
    latency_scope: str = "request",
    selected: SelectedCell | None = None,
) -> CellMetrics:
    """Compute every metric for one contender-suite cell.

    Accuracy, macro-F1, ECE, and Brier run over valid decisions on items
    with a gold label (gold >= 0). Malformed and failed attempts count in
    their own columns, never as wrong answers. Latency percentiles run
    over valid rows; ``latency_scope`` states whether one number covers
    one request (protocol v1) or the complete decision including retry
    backoff (protocol v2). Cost covers every recorded attempt when the
    run kept attempt logs, and states its scope.
    """
    cell = CellMetrics(
        contender=contender,
        suite=suite_id,
        source_run=source_run,
        latency_scope=latency_scope,
    )
    if contender.startswith("typesafe:"):
        cell.confidence_semantics = "provider-native score; not established as probability"
    elif contender.startswith("baseline:"):
        cell.confidence_semantics = "baseline-defined confidence"
    cell.n_rows = len(rows)
    cell.n_items = len({r["item_id"] for r in rows})
    cell.n_ok = sum(1 for r in rows if r["ok"])
    cell.n_malformed = sum(1 for r in rows if r["malformed"])
    cell.n_failed = sum(1 for r in rows if not r["ok"] and not r["malformed"])

    if manifest_cell is not None:
        cell.status = manifest_cell.get("status", "ok")
        cell.stop_reason = manifest_cell.get("abort_reason")
        if suite_id == "s2_spam" and cell.stop_reason:
            cell.stop_reason = "recorded stop; diagnostic omitted by S2 publication policy"
        expected = manifest_cell.get("expected_rows")
        if expected is not None and (type(expected) is not int or expected < 0):
            raise MergeError("manifest expected_rows must be a nonnegative integer")
        cell.expected_decisions = expected or 0
    if not cell.expected_decisions:
        repeats = (_protocol_of(selected.run.manifest).get("repeats") if selected else None) or max(
            1, len({r["repeat"] for r in rows}) or 1
        )
        cell.expected_decisions = len(items) * repeats if items else cell.n_rows
    if cell.n_rows > cell.expected_decisions:
        raise MergeError("completed decisions exceed the manifest's expected decision count")
    if cell.expected_decisions:
        cell.completion_coverage = cell.n_rows / cell.expected_decisions
        cell.valid_coverage = cell.n_ok / cell.expected_decisions
    cell.partial = (
        cell.status not in ("ok", None)
        or cell.n_rows < cell.expected_decisions
        or cell.n_malformed + cell.n_failed > 0
    )

    scored = [r for r in rows if r["ok"] and r["gold_index"] >= 0]
    if scored:
        preds = [r["choice_index"] for r in scored]
        golds = [r["gold_index"] for r in scored]
        confs = [r["confidence"] for r in scored]
        corrects = [r["correct"] for r in scored]
        cell.accuracy = accuracy(preds, golds)
        if suite_id in LABEL_SUITES and items:
            pred_labels = [items[r["item_id"]].options[r["choice_index"]] for r in scored]
            gold_labels = [items[r["item_id"]].options[r["gold_index"]] for r in scored]
            classes = sorted({o for item in items.values() for o in item.options})
            cell.macro_f1 = macro_f1_labeled(pred_labels, gold_labels, classes)
        cell.ece = ece(confs, corrects)
        cell.brier = brier(confs, corrects)
        cell.accuracy_item_interval = cluster_mean_interval(_correctness_clusters(scored, items))
    if suite_id not in LABEL_SUITES:
        cell.macro_f1_applicable = False
        cell.macro_f1 = None

    ok_rows = [r for r in rows if r["ok"]]
    if ok_rows:
        cell.latencies = {
            "p50_ms": _percentile([r["latency_ms"] for r in ok_rows], 50),
            "p95_ms": _percentile([r["latency_ms"] for r in ok_rows], 95),
            "p99_ms": _percentile([r["latency_ms"] for r in ok_rows], 99),
        }

    price = (prices or {}).get(contender)
    if price is not None:
        if price.input_per_mtok == 0.0 and price.output_per_mtok == 0.0:
            # Free by construction (deterministic baselines): a measured
            # zero, not an unknown.
            cell.cost_usd = 0.0
            cell.cost_per_1000 = 0.0
            cell.cost_scope = "no billable calls"
            cell.cost_complete = True
        elif (
            selected is not None
            and (
                selected.run.dir
                / "raw"
                / f"{contender.replace(':', '__')}.{suite_id}.attempts.jsonl"
            ).exists()
        ):
            cost, _count, reasons = _attempt_costs(selected.run.dir, contender, suite_id, price)
            cell.cost_usd = cost
            cell.cost_per_1000 = (
                cost / cell.n_rows * 1000 if cell.n_rows and cost is not None else None
            )
            cell.cost_scope = "every recorded attempt of this cell"
            cell.cost_complete = not reasons
            cell.cost_incomplete_reason = "; ".join(reasons) or None
        else:
            retried = sum(1 for r in rows if r.get("retries"))
            total, _count, reasons = _account_records(
                _legacy_cost_rows(selected, contender, suite_id) if selected else rows,
                price,
                contender,
                legacy=True,
            )
            cell.cost_usd = total
            cell.cost_per_1000 = (
                total / cell.n_rows * 1000 if cell.n_rows and total is not None else None
            )
            cell.cost_scope = "final-attempt usage of completed decisions"
            if retried:
                reasons.append(
                    f"{retried} decisions retried; the discarded first attempts' "
                    "usage was not recorded, so cost is a lower bound"
                )
            if not rows:
                reasons.append("no billable-attempt observations available")
            cell.cost_complete = not reasons
            cell.cost_incomplete_reason = "; ".join(reasons) or None
    else:
        cell.cost_incomplete_reason = "contender has no verified price in this run's snapshot"

    # S4: choices mapped through each permutation, grouped by base item.
    if suite_id == "s4_order":
        by_base: dict[str, list[int]] = {}
        conf_by_base: dict[str, list[float]] = {}
        for r in ok_rows:
            item = items.get(r["item_id"])
            perm = item.meta.get("perm") if item else None
            if not isinstance(perm, list):
                continue
            mapped = perm[r["choice_index"]]
            base = item.base_item_id or r["item_id"]
            by_base.setdefault(base, []).append(mapped)
            conf_by_base.setdefault(base, []).append(r["confidence"])
        if by_base:
            cell.flip_rate = flip_rate(by_base)
            cell.conf_range = confidence_spread(conf_by_base)["mean_range"]
        order_items = items
        if selected:
            limit = _protocol_of(selected.run.manifest).get("item_limit")
            if limit is not None:
                order_items = dict(list(items.items())[:limit])
        cell.order_stability = _order_stability(
            rows,
            order_items,
            expected_repeats=_protocol_of(selected.run.manifest).get("repeats")
            if selected
            else None,
        )

    # S5: honesty metrics on no-good-option items (gold -1).
    no_good = [r for r in ok_rows if r["gold_index"] < 0]
    if no_good:
        cell.admits_ignorance = sum(1 for r in no_good if r["confidence"] <= 0.5) / len(no_good)
        cell.mean_conf_no_good = math.fsum(r["confidence"] for r in no_good) / len(no_good)

    if suite_id == "s5_confidence":
        cell.calibration_subset = "underdetermined only (legacy ECE/Brier fields)"
        for kind, subset in (("no_good_option", no_good), ("underdetermined", scored)):
            corrects = [False if kind == "no_good_option" else r["correct"] for r in subset]
            confs = [r["confidence"] for r in subset]
            cell.uncertainty_subsets[kind] = {
                "n_valid": len(subset),
                "n_items": len({r["item_id"] for r in subset}),
                "ece": ece(confs, corrects) if subset else None,
                "brier": brier(confs, corrects) if subset else None,
                "mean_confidence": float(np.mean(confs)) if subset else None,
                "observed_correctness": float(np.mean(corrects)) if subset else None,
                "interpretation": cell.confidence_semantics,
            }
    cell.risk_coverage = score_risk_coverage(
        [r["confidence"] for r in ok_rows],
        [r["correct"] if r["gold_index"] >= 0 else False for r in ok_rows],
        cell.expected_decisions,
    )

    return cell


def _correctness_clusters(rows: list[dict], items: dict[str, DecisionItem]) -> dict[str, list]:
    clusters: dict[str, list] = {}
    for row in rows:
        item = items[row["item_id"]]
        clusters.setdefault(item.base_item_id or item.item_id, []).append(float(row["correct"]))
    return clusters


def _order_stability(
    rows: list[dict],
    items: dict[str, DecisionItem],
    expected_repeats: int | None = None,
) -> dict:
    """Use only complete, equally sized repeat/order blocks in comparisons.

    The two rates share the observation count min(repeats, permutations),
    so neither receives extra chances to disagree. Missing blocks are
    counted and excluded, never called stable. These describe variability;
    an across-order disagreement alone does not prove a causal order effect.
    """
    by_base: dict[str, list[str]] = {}
    for item in items.values():
        by_base.setdefault(item.base_item_id or item.item_id, []).append(item.item_id)
    repeats = list(range(expected_repeats or (max((r["repeat"] for r in rows), default=-1) + 1)))
    choices = {}
    for row in rows:
        if row["ok"]:
            item = items[row["item_id"]]
            perm = item.meta.get("perm")
            if isinstance(perm, list) and len(perm) == len(item.options):
                choices[(item.item_id, row["repeat"])] = perm[row["choice_index"]]
    within, across = [], []
    missing_within = missing_across = 0
    within_base_flips = set()
    eligible = {base: ids for base, ids in by_base.items() if len(ids) >= 2 and len(repeats) >= 2}
    matched = min(len(repeats), min(map(len, eligible.values()))) if eligible else 0
    for base, ids in sorted(eligible.items()):
        ids = sorted(ids)
        # All blocks have matched observations; deterministic lexicographic
        # subsampling avoids choosing a convenient response after scoring.
        for item_id in ids:
            keys = [(item_id, repeat) for repeat in repeats[:matched]]
            if all(key in choices for key in keys):
                flipped = len({choices[key] for key in keys}) > 1
                within.append(flipped)
                if flipped:
                    within_base_flips.add(base)
            else:
                missing_within += 1
        for repeat in repeats:
            keys = [(item_id, repeat) for item_id in ids[:matched]]
            if all(key in choices for key in keys):
                across.append(len({choices[key] for key in keys}) > 1)
            else:
                missing_across += 1
    return {
        "within_order_flip_rate": float(np.mean(within)) if within else None,
        "across_order_flip_rate": float(np.mean(across)) if across else None,
        "within_order_complete_blocks": len(within),
        "across_order_complete_blocks": len(across),
        "within_order_missing_blocks": missing_within,
        "across_order_missing_blocks": missing_across,
        "within_order_flipped_bases": len(within_base_flips),
        "total_base_items": len(by_base),
        "eligible_base_items": len(eligible),
        "excluded_base_items": len(by_base) - len(eligible),
        "observations_per_block": matched,
        "policy": "complete blocks only; equal observations per block; fixed repeat pairing",
    }


def _percentile(values: list[float], q: float) -> float:
    return float(np.percentile(np.asarray(values, dtype=float), q))


def contender_order(cells: dict[tuple[str, str], CellMetrics]) -> list[str]:
    """Stable contender order: baselines first, then provider groups."""
    names = {c.contender for c in cells.values()}
    return sorted(names, key=lambda n: (0 if n.startswith("baseline:") else 1, n))


# ---- SVG plots -------------------------------------------------------------


def _polyline(points: list[tuple[float, float]], color: str, width: float = 2.0) -> str:
    coords = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    return (
        f'<polyline points="{coords}" fill="none" stroke="{color}" '
        f'stroke-width="{width}" stroke-linejoin="round" stroke-linecap="round"/>'
    )


def cardinality_svg(
    series: dict[str, dict[int, dict[str, float]]],
    title: str = "S3 cardinality: accuracy and p50 latency versus N",
) -> str:
    """Two-panel SVG: accuracy versus N and p50 latency versus N (log x).

    A legend below the panels maps colors to contenders. A dashed rule in
    the contender's color marks the first N where its line stops; the
    legend names the last N it covers. An N whose entry holds only None
    values (rows exist, no valid decision) does not count as covered -
    the line ends at the last N with a value. Crowded tick labels (254,
    255, 256 sit 0.2 px apart on the log axis) are thinned by priority:
    endpoints and truncation boundaries win.
    """
    ns = sorted({n for points in series.values() for n in points})
    if not ns:
        return "<svg xmlns='http://www.w3.org/2000/svg' width='720' height='60'></svg>"

    def x_of(n: int, left: float, width: float) -> float:
        frac = math.log2(n) / math.log2(ns[-1])
        return left + frac * width

    order = sorted(series.items())
    colors = {name: PALETTE[idx % len(PALETTE)] for idx, (name, _) in enumerate(order)}
    truncations: list[tuple[str, int, int]] = []
    for name, points in order:
        drawn = [
            n
            for n, p in points.items()
            if p.get("accuracy") is not None or p.get("p50_ms") is not None
        ]
        if drawn and max(drawn) < ns[-1]:
            last_n = max(drawn)
            truncations.append((name, last_n, min(n for n in ns if n > last_n)))
    boundaries = {n for _, last, miss in truncations for n in (last, miss)}

    def choose_ticks(left: float, width: float) -> set[int]:
        """Ticks whose labels clear each other; gridlines stay for every N."""

        def priority(n: int) -> int:
            if n in (ns[0], ns[-1]):
                return 100
            if n in boundaries:
                return 80
            if n & (n - 1) == 0:
                return 50  # powers of two
            return 10

        chosen: list[int] = []
        for n in sorted(ns, key=lambda v: (-priority(v), v)):
            x = x_of(n, left, width)
            if all(abs(x - x_of(c, left, width)) >= 22.0 for c in chosen):
                chosen.append(n)
        return set(chosen)

    w, pad = 720.0, 46.0
    panel_w = (w - 2 * pad - 30) / 2
    top, height = 40.0, 230.0
    legend_cols = 2 if len(order) > 8 else 1
    legend_rows = math.ceil(len(order) / legend_cols)
    legend_top = top + height + 34.0
    h = legend_top + legend_rows * 16.0 + 10.0
    out = [
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{w:.0f}' height='{h:.0f}' "
        f"viewBox='0 0 {w:.0f} {h:.0f}' font-family='monospace' font-size='11'>",
        f"<text x='{pad}' y='20'>{html.escape(title)}</text>",
    ]
    for panel, (key, label, y_fmt) in enumerate(
        (
            ("accuracy", "accuracy", lambda v: f"{v:.2f}"),
            ("p50_ms", "p50 latency (ms)", lambda v: f"{v:,.0f}"),
        )
    ):
        left = pad + panel * (panel_w + 30)
        out.append(
            f"<text x='{left}' y='36'>{label}</text>"
            f" <rect x='{left}' y='{top}' width='{panel_w}' height='{height}' "
            f"fill='#f8f8f8' stroke='#ccc'/>"
        )
        # x gridlines for every N; labels only on ticks that clear each other
        labeled = choose_ticks(left, panel_w)
        for n in ns:
            x = x_of(n, left, panel_w)
            tick = (
                (f"<text x='{x:.1f}' y='{top + height + 14}' text-anchor='middle'>{n}</text>")
                if n in labeled
                else ""
            )
            out.append(
                f"<line x1='{x:.1f}' y1='{top}' x2='{x:.1f}' y2='{top + height}' "
                f"stroke='#e4e4e4'/>{tick}"
            )
        all_y = [
            p[key] for points in series.values() for p in points.values() if p.get(key) is not None
        ]
        if panel == 0:
            y_lo, y_hi = 0.0, 1.0
        else:
            y_hi = max(all_y) if all_y else 1.0
            y_lo = min(all_y) if all_y else 0.0
            if y_hi <= y_lo:
                y_hi = y_lo + 1.0

        def y_of(v: float, top=top, height=height, y_lo=y_lo, y_hi=y_hi):
            return top + height - (v - y_lo) / (y_hi - y_lo) * height

        # y gridlines: 5 ticks
        for i in range(6):
            v = y_lo + (y_hi - y_lo) * i / 5
            y = y_of(v)
            out.append(
                f"<line x1='{left}' y1='{y:.1f}' x2='{left + panel_w}' y2='{y:.1f}' "
                f"stroke='#e4e4e4'/>"
                f"<text x='{left - 6}' y='{y + 4:.1f}' text-anchor='end'>{y_fmt(v)}</text>"
            )
        for name, points in order:
            color = colors[name]
            coords = [
                (x_of(n, left, panel_w), y_of(points[n][key]))
                for n in ns
                if n in points and points[n].get(key) is not None
            ]
            if len(coords) >= 2:
                out.append(_polyline(coords, color))
                out.append(
                    f"<circle cx='{coords[-1][0]:.1f}' cy='{coords[-1][1]:.1f}' "
                    f"r='2.5' fill='{color}'/>"
                )
        for name, _last, first_missing in truncations:
            x = x_of(first_missing, left, panel_w)
            out.append(
                f"<line x1='{x:.1f}' y1='{top}' x2='{x:.1f}' y2='{top + height}' "
                f"stroke='{colors[name]}' stroke-width='1.5' "
                f"stroke-dasharray='3 3' opacity='0.85'/>"
            )
    legend_last = {name: last for name, last, _ in truncations}
    col_w = (w - 2 * pad) / legend_cols
    for i, (name, _) in enumerate(order):
        col, row = i % legend_cols, i // legend_cols
        x, y = pad + col * col_w, legend_top + row * 16.0
        text = name + (f" (ends at N={legend_last[name]})" if name in legend_last else "")
        out.append(
            f"<line x1='{x:.1f}' y1='{y - 3.5:.1f}' x2='{x + 14:.1f}' "
            f"y2='{y - 3.5:.1f}' stroke='{colors[name]}' stroke-width='3' "
            f"stroke-linecap='round'/>"
            f"<text x='{x + 20:.1f}' y='{y:.1f}'>{html.escape(text)}</text>"
        )
    out.append("</svg>")
    return "".join(out)


def reliability_svg(name: str, bins: list[dict]) -> str:
    """Reliability diagram: accuracy bar and mean-confidence dot per bin."""
    w, h, pad = 560.0, 300.0, 46.0
    top, height = 40.0, h - 110
    bw = (w - 2 * pad) / len(bins)
    out = [
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{w:.0f}' height='{h:.0f}' "
        f"viewBox='0 0 {w:.0f} {h:.0f}' font-family='monospace' font-size='11'>",
        f"<text x='{pad}' y='20'>{html.escape(name)}: scores / outcomes</text>",
        f"<rect x='{pad}' y='{top}' width='{w - 2 * pad}' height='{height}' "
        f"fill='#f8f8f8' stroke='#ccc'/>",
    ]
    # y gridlines at 0, 0.25, ..., 1 so bar and dot heights read directly
    for i in range(5):
        v = i / 4
        y = top + height - v * height
        out.append(
            f"<line x1='{pad}' y1='{y:.1f}' x2='{w - pad}' y2='{y:.1f}' "
            f"stroke='#e4e4e4'/>"
            f"<text x='{pad - 6:.0f}' y='{y + 4:.1f}' text-anchor='end'>{v:.2f}</text>"
        )
    out += [
        f"<line x1='{pad}' y1='{top}' x2='{w - pad}' y2='{top + height}' "
        f"stroke='#999' stroke-dasharray='4 3'/>",
        f"<text x='{w - pad}' y='{top - 6}' text-anchor='end'>"
        "identity reference; gold-labelled items only</text>",
    ]
    for i, b in enumerate(bins):
        x = pad + i * bw
        bar_h = b["acc"] * height if b["n"] else 0.0
        out.append(
            f"<rect x='{x + 3:.1f}' y='{top + height - bar_h:.1f}' "
            f"width='{bw - 6:.1f}' height='{bar_h:.1f}' fill='#2455e4' opacity='0.55'/>"
        )
        if b["n"]:
            cy = top + height - b["conf"] * height
            out.append(f"<circle cx='{x + bw / 2:.1f}' cy='{cy:.1f}' r='3.5' fill='#d42a2a'/>")
        out.append(
            f"<text x='{x + bw / 2:.1f}' y='{top + height + 14}' "
            f"text-anchor='middle'>{i / 10:.1f}</text>"
            f"<text x='{x + bw / 2:.1f}' y='{top + height + 28}' "
            f"text-anchor='middle' fill='#666'>{b['n']}</text>"
        )
    out.append(
        f"<text x='{w / 2:.0f}' y='{h - 16}' text-anchor='middle'>"
        "confidence bin (bar = observed accuracy, dot = mean confidence, n below)</text>"
    )
    out.append("</svg>")
    return "".join(out)


def reliability_bins(rows: list[dict]) -> list[dict]:
    """10 reliability bins over valid scored rows."""
    scored = [r for r in rows if r["ok"] and r["gold_index"] >= 0]
    bins = [{"lo": i / 10, "n": 0, "confidences": [], "acc_sum": 0} for i in range(10)]
    for r in scored:
        conf = min(max(r["confidence"], 0.0), 1.0)
        idx = int(np.searchsorted(np.linspace(0.0, 1.0, 11)[1:-1], conf, side="left"))
        bins[idx]["n"] += 1
        bins[idx]["confidences"].append(conf)
        bins[idx]["acc_sum"] += 1 if r["correct"] else 0
    out = []
    for b in bins:
        n = b["n"]
        out.append(
            {
                "lo": b["lo"],
                "n": n,
                "conf": math.fsum(b["confidences"]) / n if n else None,
                "acc": b["acc_sum"] / n if n else 0.0,
            }
        )
    return out


# ---- archive ---------------------------------------------------------------


_PUBLIC_NUMBERS = frozenset(
    {
        "repeat",
        "attempt",
        "choice_index",
        "confidence",
        "gold_index",
        "retries",
        "latency_ms",
        "elapsed_ms",
        "input_tokens",
        "output_tokens",
        "correct",
        "ok",
        "malformed",
        "usage_complete",
        "known_cost_usd",
        "complete",
        "budget_estimate_usd",
    }
)
_PUBLIC_USAGE_KEYS = frozenset(
    {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_write_input_tokens",
        "cache_creation_input_tokens",
        "input_tokens_include_cache",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
        "cached_tokens",
        "reasoning_tokens",
        "audio_tokens",
        "accepted_prediction_tokens",
        "rejected_prediction_tokens",
        "prompt_tokens_details",
        "completion_tokens_details",
        "input_tokens_details",
        "cache_write_tokens",
        "promptTokenCount",
        "candidatesTokenCount",
        "thoughtsTokenCount",
        "totalTokenCount",
        "cachedContentTokenCount",
        "cache_creation",
        "ephemeral_5m_input_tokens",
        "ephemeral_1h_input_tokens",
    }
)
_PUBLIC_ENUMS = {
    "outcome": {
        "ok",
        "malformed",
        "rate_limited",
        "transport",
        "provider_rejected",
        "auth",
        "error",
    },
    "error_category": {"schema", "transport", "auth", "provider", "unknown"},
    "category": {"schema", "transport", "auth", "provider", "unknown", "ok"},
    "phase": {"negotiation", "decision"},
    "latency_scope": {"request", "decision"},
    "request_config": {"v1", "v2", "v3"},
}


def _public_configuration(value: object) -> dict | None:
    """Keep exact known decoding settings so their fingerprint remains useful."""
    keys = {
        "model",
        "name",
        "provider",
        "temperature",
        "response_format",
        "max_tokens",
        "max_tokens_param",
        "extra_body",
        "thinking",
        "tool_choice",
        "type",
        "level",
        "json_schema",
        "strict",
        "schema",
        "properties",
        "choice_index",
        "confidence",
        "required",
        "additionalProperties",
        "minimum",
        "maximum",
        "confidence_semantics",
        "reasoning_effort",
        "service_tier",
    }
    strings = {
        "json_object",
        "json_schema",
        "dmb_decision",
        "object",
        "integer",
        "number",
        "choice_index",
        "confidence",
        "max_tokens",
        "max_completion_tokens",
        "enabled",
        "disabled",
        "low",
        "none",
        "minimal",
        "default",
        "record_decision",
        "provider-defined confidence",
        *(p.model for p in PRICES),
        *(p.contender for p in PRICES),
        *(p.contender.split(":", 1)[0] for p in PRICES),
    }

    def approved(child):
        if isinstance(child, dict):
            return all(key in keys and approved(v) for key, v in child.items())
        if isinstance(child, list):
            return all(approved(v) for v in child)
        return (
            child is None
            or type(child) is bool
            or _finite_number(child)
            or (isinstance(child, str) and child in strings)
        )

    return value if isinstance(value, dict) and approved(value) else None


def _public_usage(value: object) -> dict:
    """Only recognized numeric usage fields; no response text or arbitrary keys."""
    if not isinstance(value, dict):
        return {}
    out = {}
    for key in _PUBLIC_USAGE_KEYS & value.keys():
        child = value[key]
        if child is None or type(child) is bool or _finite_number(child):
            out[key] = child
        elif isinstance(child, dict):
            out[key] = _public_usage(child)
    return out


def _scrub(value: object, *, run_id: str | None = None, contender: str = "") -> dict:
    """Project a licensed record onto approved measurements, never raw content.

    Unknown keys and string values are excluded instead of recursively
    trusting provider fields. Consequently nesting depth cannot bypass the
    policy. Identifiers are fixed enums or validated benchmark identifiers.
    """
    if not isinstance(value, dict):
        return {"publication_redaction": _REDACTED}
    out = {"publication_redaction": _REDACTED}
    for key in _PUBLIC_NUMBERS & value.keys():
        child = value[key]
        if child is None or type(child) is bool or _finite_number(child, minimum=-1):
            out[key] = child
    known_contenders = {p.contender for p in PRICES}
    identity = contender if contender in known_contenders else value.get("contender")
    if not isinstance(identity, str) or identity not in known_contenders:
        identity = ""
    if identity:
        out["contender"] = identity
        out["provider"] = identity.split(":", 1)[0]
    if run_id is not None:
        out["run_id"] = run_id  # archive directory identity, never provider data
    suite = value.get("suite")
    if isinstance(suite, str) and suite in (*SUITE_ORDER, *SUITE_IDS.values()):
        out["suite"] = suite
    item_id = value.get("item_id")
    if isinstance(item_id, str) and re.fullmatch(
        r"s[1-5]-(?:(?:ng|ud)-|n[0-9]+-)?[0-9]+(?:-p[0-9]+)?", item_id
    ):
        out["item_id"] = item_id
    for key, allowed in _PUBLIC_ENUMS.items():
        if isinstance(value.get(key), str) and value[key] in allowed:
            out[key] = value[key]
        elif value.get(key) is None and key in value:
            out[key] = None
    fingerprint = value.get("configuration_fingerprint")
    if isinstance(fingerprint, str) and re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        out["configuration_fingerprint"] = fingerprint
    config = _public_configuration(value.get("effective_configuration"))
    if config is not None:
        out["effective_configuration"] = config
    if "error" in value:
        out["error"] = None if value["error"] is None else _REDACTED
    usage = _record_usage(value, identity if isinstance(identity, str) else "")
    if usage:
        out["usage_details"] = _public_usage(usage)
    if isinstance(value.get("decision"), dict):
        out["decision"] = _scrub(value["decision"], contender=identity or "")
    if isinstance(value.get("attempts"), list):
        out["attempts"] = [_scrub(a, contender=identity or "") for a in value["attempts"]]
    return out


def _archive_record(
    value: object,
    *,
    run_id: str,
    force: bool = False,
    contender: str = "",
) -> object:
    if isinstance(value, dict):
        item_id = value.get("item_id")
        licensed = value.get("suite") in ("s2_spam", "s2-gate-spam") or (
            isinstance(item_id, str) and item_id.startswith("s2-")
        )
        if force or licensed:
            return _scrub(value, run_id=run_id, contender=contender)
        return {
            key: _archive_record(child, run_id=run_id, contender=contender)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            _archive_record(child, run_id=run_id, force=force, contender=contender)
            for child in value
        ]
    return value


def archive_runs(run_dirs: list[Path], out_path: Path, redact_suite: str = "s2_spam") -> Path:
    """Pack run directories into a tar.gz, scrubbing the licensed suite.

    Licensed raw files and S2 rows in shared logs use an allowlist.
    Probe logs always use that same allowlist: provider text is unnecessary
    to reproduce probe accounting. Unknown binary/text sidecars are omitted.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with (
        out_path.open("wb") as handle,
        gzip.GzipFile(filename="", mode="wb", fileobj=handle, mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as tar,
    ):
        for run_dir in dict.fromkeys(run_dirs):
            for file in sorted(run_dir.rglob("*")):
                if not file.is_file():
                    continue
                arcname = f"{run_dir.name}/{file.relative_to(run_dir)}"
                force = redact_suite in file.name or any(
                    word in file.name.lower() for word in ("negotiat", "probe")
                )
                if (
                    file.suffix == ".jsonl"
                    and file.name != "results.jsonl"
                    and not any(suite in file.name for suite in SUITE_ORDER)
                ):
                    force = True
                if file.suffix == ".jsonl":
                    contender = ""
                    for suite in SUITE_ORDER:
                        if f".{suite}" in file.name:
                            contender = file.name.split(f".{suite}", 1)[0].replace("__", ":", 1)
                            break
                    scrubbed = "".join(
                        json.dumps(
                            _archive_record(
                                json.loads(line),
                                run_id=run_dir.name,
                                force=force,
                                contender=contender,
                            ),
                            allow_nan=False,
                            sort_keys=True,
                        )
                        + "\n"
                        for line in file.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    )
                    info = tarfile.TarInfo(arcname)
                    info.size = len(scrubbed.encode("utf-8"))
                    tar.addfile(info, io.BytesIO(scrubbed.encode("utf-8")))
                elif file.name in {"manifest.json", "prices.snapshot.json"}:
                    # Manifest free-text diagnostics can quote provider errors.
                    # Strip them everywhere, including run-wide stop summaries.
                    payload = json.loads(file.read_text(encoding="utf-8"))
                    if file.name == "manifest.json":
                        payload = _archive_manifest(payload, run_dir.name)
                    encoded = (
                        json.dumps(payload, indent=2, allow_nan=False, sort_keys=True) + "\n"
                    ).encode()
                    info = tarfile.TarInfo(arcname)
                    info.size = len(encoded)
                    tar.addfile(info, io.BytesIO(encoded))
                elif file.suffix == ".json":
                    # Other provider-produced sidecars have no approved schema.
                    continue
                elif not force and file.parent.name == "raw":
                    continue
                else:
                    # No arbitrary text sidecars enter a public raw archive.
                    continue
    return out_path


def _archive_manifest(value: object, run_id: str) -> object:
    """Publish an explicit manifest metadata schema, not diagnostic prose."""
    numeric = {
        "temperature",
        "concurrency_per_provider",
        "repeats",
        "malformed_retry",
        "transport_retry",
        "suite_wall_limit_s",
        "request_timeout_s",
        "hard_cap_usd",
        "soft_cap_usd",
        "item_limit",
        "items",
        "rows",
        "expected_rows",
        "drained_rows",
        "wall_s",
        "spend_usd",
        "decision_spend_usd",
        "negotiation_spend_usd",
        "unknown_usage_attempts",
        "total_cell_wall_s",
        "exit_code",
        "input_tokens",
        "output_tokens",
        "cost_complete",
        "incomplete_cost_attempts",
        "budget_accounted_usd",
        "prior",
        "input",
        "output",
        "count",
    }
    nested = {"protocol", "contenders", "cells", "skipped", "stops", "majority_prior"}
    enum_fields = {
        **_PUBLIC_ENUMS,
        "status": {
            "running",
            "complete",
            "completed",
            "ok",
            "stopped",
            "failed",
            "skipped",
            "interrupted",
            "dnf",
            "error",
            "stopped_budget",
            "stopped_accounting",
            "stopped_auth",
            "stopped_deadline",
            "auth_error",
            "budget",
            "accounting",
            "deadline",
            "execution_failure",
        },
        "rate_limit_backoff": {"exponential"},
    }
    names = {p.contender for p in PRICES}
    providers = {name.split(":", 1)[0] for name in names}
    if isinstance(value, list):
        return [_archive_manifest(child, run_id) for child in value]
    if isinstance(value, dict):
        out = {}
        for key, child in value.items():
            if key in {
                "error",
                "abort_reason",
                "reason",
                "notes",
                "deviations",
                "execution_errors",
            }:
                out[key] = [] if isinstance(child, list) else (_REDACTED if child else None)
            elif key == "run_id":
                out[key] = run_id
            elif key in numeric and (child is None or type(child) is bool or _finite_number(child)):
                out[key] = child
            elif key in nested:
                out[key] = _archive_manifest(child, run_id)
            elif key == "effective_configuration":
                config = _public_configuration(child)
                if config is not None:
                    out[key] = config
            elif key == "item_files" and isinstance(child, dict):
                out[key] = {
                    suite: digest
                    for suite, digest in child.items()
                    if suite in SUITE_ORDER
                    and isinstance(digest, str)
                    and re.fullmatch(r"[a-f0-9]{64}", digest)
                }
            elif key == "suites" and isinstance(child, list):
                out[key] = [suite for suite in child if suite in SUITE_ORDER]
            elif (
                key in {"contender", "name"}
                and isinstance(child, str)
                and child in names
                or key == "provider"
                and isinstance(child, str)
                and child in providers
                or key in {"suite", "source_suite"}
                and isinstance(child, str)
                and child in SUITE_ORDER
                or key in enum_fields
                and isinstance(child, str)
                and child in enum_fields[key]
            ):
                out[key] = child
            elif key in {
                "price_table_sha256",
                "prompt_sha256",
                "source_sha256",
                "configuration_fingerprint",
            } and isinstance(child, str):
                if re.fullmatch(r"[a-f0-9]{64}", child):
                    out[key] = child
            elif (
                key == "protocol_version"
                and isinstance(child, str)
                and re.fullmatch(r"v[0-9]+", child)
                or key == "record_format"
                and isinstance(child, str)
                and re.fullmatch(r"dmb-records-[0-9]+", child)
            ):
                out[key] = child
            elif key in {"created_at", "finished_at"} and isinstance(child, str):
                if re.fullmatch(r"[0-9T:.+Z -]+", child):
                    out[key] = child
            elif key in {
                "spend_by_contender_usd",
                "tokens_by_contender",
                "unknown_usage_attempts",
                "incomplete_cost_attempts",
            } and isinstance(child, dict):
                out[key] = {
                    name: _archive_manifest(record, run_id)
                    for name, record in child.items()
                    if name in names
                }
            elif key in SUITE_ORDER and isinstance(child, dict):
                out[key] = _archive_manifest(child, run_id)
            elif key in {"negotiation", "negotiation_usage", "accounting_gaps"}:
                # Setup metadata may contain arbitrary provider diagnostics.
                if isinstance(child, dict):
                    out[key] = {
                        name: _scrub(record, run_id=run_id, contender=name)
                        for name, record in child.items()
                        if name in names
                    }
                else:
                    out[key] = []
        return out
    return value if value is None or type(value) is bool or _finite_number(value) else _REDACTED


# ---- rendering -------------------------------------------------------------


def _md_table(header: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    for row in rows:
        out.append("| " + " | ".join(row) + " |")
    return "\n".join(out)


def _html_table(header: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{html.escape(h)}</th>" for h in header)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in row) + "</tr>" for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


@dataclass
class TableSpec:
    """One metric table: title, metric key, format, and suite scope."""

    title: str
    key: str
    fmt: object
    suites: frozenset[str] | None = None
    """Suites the metric applies to; None means every suite."""
    out_of_scope: str = ""
    """Cell text for suites outside ``suites`` ("" or "n/a")."""
    star_partial: bool = False
    """Append the partial-coverage star to values."""


_COMPUTED_KEYS = {"_malformed_rate", "_failed_rate"}

#: Keys every table spec must draw from: the one metric representation
#: (``CellMetrics.as_dict``) plus the two computed rates.
KNOWN_METRIC_KEYS = set(CellMetrics(contender="", suite="").as_dict()) | _COMPUTED_KEYS


def _table_specs() -> list[TableSpec]:
    pct = _fmt_pct
    return [
        TableSpec(
            "Accuracy (percent, valid rows, gold items)",
            "accuracy",
            pct,
            star_partial=True,
        ),
        TableSpec(
            "Macro-F1 (stable option labels; absent classes count as 0)",
            "macro_f1",
            _fmt_f,
            suites=LABEL_SUITES,
            out_of_scope="n/a",
            star_partial=True,
        ),
        TableSpec("ECE diagnostic (gold-labelled only; S5 underdetermined only)", "ece", _fmt_f),
        TableSpec(
            "Brier diagnostic (gold-labelled only; S5 underdetermined only)", "brier", _fmt_f
        ),
        TableSpec(
            "Malformed rate (percent of completed decisions)",
            "_malformed_rate",
            pct,
        ),
        TableSpec(
            "Failed attempts (rate, percent of completed decisions)",
            "_failed_rate",
            pct,
        ),
        TableSpec("Latency p50 (ms)", "p50_ms", _fmt_ms),
        TableSpec("Latency p95 (ms)", "p95_ms", _fmt_ms),
        TableSpec("Latency p99 (ms)", "p99_ms", _fmt_ms),
        TableSpec(
            "Known cost per 1,000 completed decisions (USD)",
            "cost_per_1000_usd",
            _fmt_usd,
        ),
        TableSpec(
            "S4 overall flip rate (all repeats and orders; percent of observed bases)",
            "flip_rate",
            pct,
            suites=frozenset({"s4_order"}),
        ),
        TableSpec(
            "S4 mean confidence range across all repeats and orders",
            "mean_conf_range",
            _fmt_f,
            suites=frozenset({"s4_order"}),
        ),
        TableSpec(
            "S5 score <= 0.5 on no-good items (percent; score semantics differ)",
            "admits_ignorance",
            pct,
            suites=frozenset({"s5_confidence"}),
        ),
        TableSpec(
            "S5 mean confidence on no-good items",
            "mean_conf_no_good",
            _fmt_f,
            suites=frozenset({"s5_confidence"}),
        ),
        TableSpec(
            "S4 within-order repeat disagreement (complete matched blocks, percent)",
            "within_order_flip_rate",
            pct,
            suites=frozenset({"s4_order"}),
        ),
        TableSpec(
            "S4 across-order disagreement (complete matched-repeat blocks, percent)",
            "across_order_flip_rate",
            pct,
            suites=frozenset({"s4_order"}),
        ),
        TableSpec(
            "S5 no-good ECE diagnostic (every valid choice is wrong)",
            "no_good_ece",
            _fmt_f,
            suites=frozenset({"s5_confidence"}),
        ),
        TableSpec(
            "S5 no-good Brier diagnostic (every valid choice is wrong)",
            "no_good_brier",
            _fmt_f,
            suites=frozenset({"s5_confidence"}),
        ),
    ]


def validate_table_specs() -> None:
    """Every configured metric key must exist in the one representation."""
    for spec in _table_specs():
        if spec.key not in KNOWN_METRIC_KEYS:
            raise KeyError(f"table spec {spec.title!r} uses unknown metric key {spec.key!r}")


def _cell_value(cell: CellMetrics, key: str) -> object:
    """One lookup path: the cell's ``as_dict`` representation."""
    if key == "_malformed_rate":
        return cell.n_malformed / cell.n_rows if cell.n_rows else None
    if key == "_failed_rate":
        return cell.n_failed / cell.n_rows if cell.n_rows else None
    return cell.as_dict()[key]


def _metric_tables(model: ReportModel) -> list[tuple[str, list[str], list[list[str]]]]:
    """One table per metric: (title, header, rows), from validated specs."""
    tables: list[tuple[str, list[str], list[list[str]]]] = []
    for spec in _table_specs():
        if spec.suites is not None and not spec.out_of_scope:
            # The metric applies to named suites only. Blank out-of-scope
            # columns read as a broken table, so list the suites measured.
            table_suites = [s for s in model.suites if s in spec.suites]
        else:
            table_suites = list(model.suites)
        if not table_suites:
            continue  # this report holds no cell the metric applies to
        rows = []
        for contender in model.contenders:
            row = [contender]
            for suite in table_suites:
                cell = model.cells.get((contender, suite))
                if cell is None:
                    row.append("-")
                    continue
                if spec.suites is not None and suite not in spec.suites:
                    row.append(spec.out_of_scope)
                    continue
                value = spec.fmt(_cell_value(cell, spec.key))
                if spec.key == "macro_f1" and not cell.macro_f1_applicable:
                    value = "n/a"
                if spec.star_partial and cell.partial and value not in ("-", "n/a"):
                    value += "*"  # partial coverage: see the coverage table
                if spec.key == "cost_per_1000_usd" and (
                    cell.cost_incomplete_reason and value != "-"
                ):
                    value += "†"  # cost is a lower bound or partial
                row.append(value)
            rows.append(row)
        header = ["contender"] + [SUITE_TITLES.get(s, s) for s in table_suites]
        title = spec.title
        if spec.star_partial:
            title += " (*: partial coverage; see the coverage table)"
        if spec.key == "cost_per_1000_usd":
            title += " (†: incomplete usage; see the coverage table)"
        if spec.key == "macro_f1":
            title += (
                ". Not applicable on S3 and S5: their option texts vary "
                "between items, so positions are not classes"
            )
        tables.append((title, header, rows))
    return tables


def _coverage_rows(model: ReportModel) -> list[list[str]]:
    rows = []
    for contender in model.contenders:
        for suite in model.suites:
            cell = model.cells.get((contender, suite))
            if cell is None:
                continue
            rows.append(
                [
                    contender,
                    SUITE_TITLES.get(suite, suite),
                    cell.status,
                    str(cell.expected_decisions),
                    str(cell.n_rows),
                    str(cell.n_ok),
                    str(cell.n_malformed),
                    str(cell.n_failed),
                    _fmt_pct(cell.completion_coverage),
                    _fmt_pct(cell.valid_coverage),
                    cell.stop_reason or "",
                    cell.source_run or "",
                ]
            )
    return rows


def _analysis_tables(model: ReportModel) -> list[Table]:
    intervals, order_counts, subsets, accounting = [], [], [], []
    for key in sorted(model.cells):
        cell = model.cells[key]
        interval = cell.accuracy_item_interval
        if interval:
            intervals.append(
                [
                    cell.contender,
                    cell.suite,
                    str(interval["n_clusters"]),
                    _fmt_pct(interval["estimate"]),
                    _fmt_pct(interval["lower"]),
                    _fmt_pct(interval["upper"]),
                ]
            )
        if cell.order_stability:
            order = cell.order_stability
            order_counts.append(
                [
                    cell.contender,
                    str(order["observations_per_block"]),
                    str(order["within_order_complete_blocks"]),
                    str(order["within_order_missing_blocks"]),
                    str(order["across_order_complete_blocks"]),
                    str(order["across_order_missing_blocks"]),
                ]
            )
        for kind, subset in cell.uncertainty_subsets.items():
            subsets.append([cell.contender, kind, str(subset["n_items"]), str(subset["n_valid"])])
        accounting.append(
            [
                cell.contender,
                cell.suite,
                "yes" if cell.cost_complete else "no",
                cell.cost_scope or "unavailable",
                cell.cost_incomplete_reason or "",
            ]
        )
    tables = [
        (
            "Cost accounting completeness",
            ["contender", "suite", "complete", "scope", "limitation"],
            accounting,
        ),
        (
            "Descriptive accuracy interval (equal item weights, 95% cluster bootstrap, percent)",
            ["contender", "suite", "clusters", "item mean", "lower", "upper"],
            intervals,
        ),
        (
            "S4 comparison coverage (missing blocks are excluded)",
            [
                "contender",
                "observations/block",
                "within complete",
                "within missing",
                "across complete",
                "across missing",
            ],
            order_counts,
        ),
        (
            "S5 diagnostic denominators (valid decisions only)",
            ["contender", "subset", "items", "valid decisions"],
            subsets,
        ),
    ]
    return [table for table in tables if table[2]]


_COVERAGE_HEADER = [
    "contender",
    "suite",
    "status",
    "expected",
    "completed",
    "valid",
    "malformed",
    "failed",
    "completion %",
    "valid %",
    "stop reason",
    "source run",
]


@dataclass
class ReportModel:
    """Everything the renderers need, computed once."""

    runs: list[RunData]
    cells: dict[tuple[str, str], CellMetrics]
    contenders: list[str]
    suites: list[str]
    cardinality: dict[str, dict[int, dict[str, float]]]
    reliability: dict[str, list[dict]]
    spend_usd: float
    selected_spend_usd: float | None
    deviations: list[str]
    notes: list[str]
    skipped: list[dict]
    protocol_notes: list[str]
    latency_scope: str
    correction_note: str | None = None
    paired_accuracy: list[dict] = field(default_factory=list)

    def json_summary(self) -> dict:
        return {
            "runs": [r.run_id for r in self.runs],
            "spend_usd": self.spend_usd,
            "selected_cells_spend_usd": (
                round(self.selected_spend_usd, 8) if self.selected_spend_usd is not None else None
            ),
            "selected_cells_cost_complete": all(c.cost_complete for c in self.cells.values()),
            "source_manifest_cost_complete": all(
                r.manifest.get("cost_complete", False) for r in self.runs
            ),
            "spend_scope": "reported source-manifest total; legacy amounts may be nominal",
            "latency_scope": self.latency_scope,
            "analysis_notes": self.protocol_notes,
            "paired_accuracy": self.paired_accuracy,
            "cells": [c.as_dict() for c in self.cells.values()],
        }


def build_report_model(
    runs: list[RunData],
    root: Path,
    allow_protocol_mix: bool = False,
) -> ReportModel:
    """Aggregate runs into the report model.

    Runs are deduplicated first (the same directory supplied twice is
    counted once), every run is verified, and later runs replace earlier
    cells whole. Tables, plots, and machine-readable output all draw from
    the same selected cells.
    """
    deduped: dict[Path, RunData] = {}
    for run in runs:
        deduped[run.dir.resolve()] = run
    runs = list(deduped.values())

    items_by_suite = verify_runs(runs, root, allow_protocol_mix=allow_protocol_mix)
    selection = select_cells(runs)

    prices_by_run = {run.run_id: _prices_for_run(run) for run in runs}
    cells: dict[tuple[str, str], CellMetrics] = {}
    for (contender, suite_id), selected in selection.items():
        run = selected.run
        cells[(contender, suite_id)] = aggregate_cell(
            contender,
            suite_id,
            selected.rows,
            items_by_suite.get(suite_id, {}),
            manifest_cell=selected.manifest_cell,
            source_run=run.run_id,
            prices=prices_by_run.get(run.run_id, {}),
            latency_scope=_protocol_of(run.manifest).get("latency_scope", "request"),
            selected=selected,
        )
    contenders = contender_order(cells)
    paired_accuracy = []
    for suite in SUITE_ORDER:
        clusters = {}
        for contender in contenders:
            selected = selection.get((contender, suite))
            if selected is not None:
                scored = [r for r in selected.rows if r["ok"] and r["gold_index"] >= 0]
                clusters[contender] = _correctness_clusters(scored, items_by_suite[suite])
        for first, second in combinations(sorted(clusters), 2):
            paired_accuracy.append(
                {
                    "suite": suite,
                    "first": first,
                    "second": second,
                    **paired_cluster_difference(clusters[first], clusters[second]),
                }
            )

    # Plots draw from the selected cells, like every table: a replaced
    # cell must not re-enter through a plot.
    cardinality: dict[str, dict[int, dict[str, float]]] = {}
    items = items_by_suite.get("s3_cardinality", {})
    s3_rows: dict[str, list[dict]] = {}
    for contender in contenders:
        selected = selection.get((contender, "s3_cardinality"))
        s3_rows[contender] = selected.rows if selected is not None else []
    for contender in contenders:
        by_n: dict[int, dict[str, float]] = {}
        for n in sorted({i.meta["N"] for i in items.values()}):
            ids = {i.item_id for i in items.values() if i.meta["N"] == n}
            n_rows = [r for r in s3_rows[contender] if r["item_id"] in ids]
            cell = aggregate_cell(contender, "s3_cardinality", n_rows, items)
            if cell.n_rows:
                by_n[n] = {
                    "accuracy": cell.accuracy,
                    "p50_ms": cell.latencies.get("p50_ms"),
                    "n_rows": float(cell.n_rows),
                }
        if by_n:
            cardinality[contender] = by_n

    reliability = {}
    for contender in contenders:
        pooled = [
            r for key, selected in selection.items() if key[0] == contender for r in selected.rows
        ]
        reliability[contender] = reliability_bins(pooled)

    deviations: list[str] = []
    notes: list[str] = []
    skipped: list[dict] = []
    protocol_notes: list[str] = []
    spend = 0.0
    for run in runs:
        licensed = "s2_spam" in run.manifest.get("item_files", {})
        if licensed:
            notes.append(
                f"[{run.run_id}] Free-text provider/run diagnostics are omitted by S2 "
                "publication policy; structured protocol and configuration metadata remain."
            )
            skipped.extend(
                dict(entry, reason="recorded skip; diagnostic omitted by S2 publication policy")
                for entry in run.manifest.get("skipped", [])
            )
        else:
            deviations.extend(run.manifest.get("deviations", []))
            notes.extend(f"[{run.run_id}] {n}" for n in run.manifest.get("notes", []))
            for contender in run.manifest.get("contenders", []):
                for note in contender.get("notes", []):
                    notes.append(f"[{run.run_id}] {contender['name']}: {note}")
            skipped.extend(run.manifest.get("skipped", []))
        spend += run.manifest.get("spend_usd", 0.0)

    if allow_protocol_mix:
        versions = sorted(
            {_protocol_of(run.manifest).get("protocol_version", "v1") for run in runs}
        )
        protocol_notes.append(
            "Mixed protocol versions merged by explicit policy: "
            + ", ".join(versions)
            + ". Replacement cells from different versions may differ in "
            "latency scope, prompt text, and record format; the per-cell "
            "source run column says which version each cell came from."
        )
    unknown_costs = [c for c in cells.values() if c.cost_incomplete_reason]
    if unknown_costs:
        protocol_notes.append(
            "Cost caveats: some cells report incomplete usage. Unknown usage "
            "is not a measured zero; see the cost table markers and the "
            "coverage table."
        )
    protocol_notes += [
        "Confidence semantics differ: LLM scores are prompted probabilities; jev's native "
        "score is provider-defined and has not been established as probability of correctness. "
        "ECE/Brier for native scores are score-to-outcome diagnostics, not proof of dishonesty "
        "or comparable probability calibration.",
        "S5 legacy ECE/Brier fields cover only underdetermined items with synthetic hidden "
        "labels. Separate no-good diagnostics include every valid forced choice as wrong. "
        "Scores <= 0.5 are a descriptive cutoff, not an honesty verdict.",
        "S4 overall flips combine repeat nondeterminism and option-order variation. Within-order "
        "and across-order rates use complete blocks with equal observation counts and fixed "
        "repeat matching; missing blocks are excluded and counted in the JSON. Across-order "
        "disagreement alone does not establish a causal position effect.",
        "Item-cluster bootstrap intervals use 2,000 deterministic resamples, equal item weights, "
        "and S4 base items as clusters. Pairwise differences use common observed items. "
        "Repeated responses are not independent examples; intervals do not cover missingness "
        "or dataset shift and pairwise intervals are not multiplicity-adjusted.",
        "Risk/coverage points in the JSON use fixed, unfitted score cutoffs and expected "
        "decisions as the coverage denominator. They describe these observations only. "
        "Selecting a deployment threshold requires a separate held-out calibration/evaluation "
        "split; no deployment cutoff is recommended here.",
    ]
    if any(_protocol_of(r.manifest)["protocol_version"] == "v1" for r in runs):
        protocol_notes.append(
            "Historical v1/v1.1 provider observations used unequal uncertainty instructions: "
            "LLMs were explicitly told to report low confidence under uncertainty; jev was not. "
            "New shared instructions do not retroactively make those measurements comparable."
        )

    latency_scopes = {_protocol_of(run.manifest).get("latency_scope", "request") for run in runs}
    latency_scope = "mixed" if len(latency_scopes) > 1 else next(iter(latency_scopes), "request")

    known_subtotals = [c.cost_usd for c in cells.values() if c.cost_usd is not None]
    selected_spend = math.fsum(known_subtotals) if known_subtotals else None
    return ReportModel(
        runs=runs,
        cells=cells,
        contenders=contenders,
        suites=[s for s in SUITE_ORDER if any(k[1] == s for k in cells)],
        cardinality=cardinality,
        reliability=reliability,
        spend_usd=spend,
        selected_spend_usd=selected_spend,
        deviations=sorted(set(deviations)),
        notes=notes,
        skipped=skipped,
        protocol_notes=protocol_notes,
        latency_scope=latency_scope,
        paired_accuracy=paired_accuracy,
    )


LATENCY_SCOPE_TEXT = {
    "request": ("Latency covers one provider request (the final attempt of each decision)."),
    "decision": (
        "Latency covers the complete decision: first attempt through final "
        "outcome, including retry backoff."
    ),
    "mixed": (
        "Latency scopes are mixed across source runs; see each cell's "
        "source run and protocol version."
    ),
}


def _demote_headings(text: str) -> str:
    """One heading level deeper, so an inlined note cannot restart H1.

    Fenced code blocks pass through untouched. Lines without a space
    after the hashes are not headings and stay as they are.
    """
    out: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            out.append(line)
            continue
        if not fenced and line.startswith("#"):
            stripped = line.lstrip("#")
            level = len(line) - len(stripped)
            if 1 <= level <= 5 and not stripped[:1].strip():
                line = "#" + line
        out.append(line)
    return "\n".join(out)


def render_md(model: ReportModel, json_path: Path | None = None) -> str:
    """The full Markdown report."""
    lines = [
        "# Decision-model benchmark: results",
        "",
        f"Runs: {', '.join(r.run_id for r in model.runs)}.",
        f"Source manifests record ${model.spend_usd:.2f} in total "
        "(historical totals may use nominal rates or incomplete usage). "
        f"Recomputed known subtotal for selected cells: {_fmt_usd(model.selected_spend_usd)}. "
        "This is not a complete bill where accounting is marked incomplete.",
        "",
        "Every number in this file recomputes from `results.jsonl` and the",
        "manifests in the raw archive. Later runs replace earlier cells",
        "whole; each coverage row names its source run.",
        "",
        "Denominators:",
        "",
        "- Accuracy and macro-F1 cover valid decisions on gold-labelled items.",
        "  General ECE/Brier use that subset too; S5 no-good diagnostics are separate.",
        "- Completion coverage is completed decisions divided by expected",
        "  decisions; valid coverage is valid decisions divided by expected",
        "  decisions.",
        "- Malformed and failed rates use completed decisions as the",
        "  denominator.",
        "- Cost covers the attempt charges each cell's `cost_scope` names;",
        "  unknown usage is never treated as a measured zero.",
        f"- {LATENCY_SCOPE_TEXT[model.latency_scope]}",
        "",
    ]
    if model.correction_note:
        lines += ["## Correction note", "", _demote_headings(model.correction_note), ""]
    if model.protocol_notes:
        lines += ["## Protocol and data caveats", ""]
        lines += [f"- {n}" for n in model.protocol_notes] + [""]
    if model.deviations:
        lines += ["## Protocol deviations (recorded, not edited)", ""]
        lines += [f"- {d}" for d in model.deviations] + [""]
    if model.skipped:
        lines += ["## Skipped", ""]
        lines += [f"- {s['contender']}: {s['reason']}" for s in model.skipped] + [""]
    if model.notes:
        lines += ["## Run notes", ""]
        lines += [f"- {n}" for n in model.notes] + [""]

    lines += [
        "## Coverage (expected versus completed decisions)",
        "",
        _md_table(_COVERAGE_HEADER, _coverage_rows(model)),
        "",
        "A cell is partial when it stopped early or returned malformed or",
        "failed decisions. Empty, failed, and skipped cells stay listed",
        "with their reasons.",
        "",
    ]
    for title, header, rows in [*_metric_tables(model), *_analysis_tables(model)]:
        lines += [f"## {title}", "", _md_table(header, rows), ""]

    lines += ["## Cardinality (S3)", "", "![cardinality](cardinality.svg)", ""]
    lines += ["## Reliability diagrams", ""]
    for contender in model.contenders:
        lines += [
            f"### {contender}",
            "",
            f"![{contender}](reliability-{_safe(contender)}.svg)",
            "",
        ]

    lines += ["## Machine-readable cell metrics", ""]
    if json_path:
        lines += [f"See `{json_path.name}` next to this file.", ""]
    lines += [
        "## Data and licenses",
        "",
        "- banking77 (S1, S4 base items): PolyAI, CC BY 4.0; intent labels",
        "  appear in the raw logs with this attribution.",
        "- UCI SMS spam (S2): UCI currently identifies the collection as CC BY 4.0",
        "  (https://archive.ics.uci.edu/dataset/228/sms+spam+collection). Text stays",
        "  local under this project's conservative publication policy.",
        "- S3, S5 are synthetic and generated by this repository's code",
        "  from seed 20260918.",
        "",
    ]
    return "\n".join(lines) + "\n"


def _safe(name: str) -> str:
    return name.replace(":", "__").replace("/", "_").replace(".", "_")


_PAGE_CSS = """
body { font-family: Georgia, serif; margin: 2rem auto; max-width: 60rem;
       padding: 0 1rem; color: #1a1a1a; }
h1, h2, h3 { font-family: system-ui, sans-serif; line-height: 1.2; }
table { border-collapse: collapse; margin: 1rem 0; font-family: monospace; font-size: 0.85rem; }
th, td { border: 1px solid #ddd; padding: 0.25rem 0.6rem; text-align: right; }
th:first-child, td:first-child { text-align: left; }
svg { max-width: 100%; height: auto; margin: 1rem 0; }
code { background: #f4f4f4; padding: 0 0.15rem; }
"""


Table = tuple[str, list[str], list[list[str]]]


def render_html(model: ReportModel, tables: list[Table]) -> str:
    """Static HTML page: tables plus inline SVG plots."""
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<title>Decision-model benchmark</title>",
        f"<style>{_PAGE_CSS}</style></head><body>",
        "<h1>Decision-model benchmark: results</h1>",
        f"<p>Runs: {html.escape(', '.join(r.run_id for r in model.runs))}. "
        f"Source manifests record ${model.spend_usd:.2f} (historical totals may be nominal "
        "or incomplete); recomputed known subtotal for selected "
        f"cells: {_fmt_usd(model.selected_spend_usd)}. See accounting completeness.</p>",
        "<p>Every number recomputes from <code>results.jsonl</code> and the",
        " manifests in the raw archive. Accuracy and macro-F1 cover gold-labelled items. "
        " General ECE/Brier do too; S5 no-good diagnostics are separate. Malformed and failed",
        " attempts have their own columns. Cost covers the attempt charges",
        " each cell's cost scope names.</p>",
        f"<p>{html.escape(LATENCY_SCOPE_TEXT[model.latency_scope])}</p>",
    ]
    if model.correction_note:
        parts.append("<h2>Correction note</h2>")
        parts.append(f"<pre>{html.escape(model.correction_note)}</pre>")
    if model.protocol_notes:
        parts.append("<h2>Protocol and data caveats</h2><ul>")
        parts += [f"<li>{html.escape(n)}</li>" for n in model.protocol_notes]
        parts.append("</ul>")
    if model.deviations:
        parts.append("<h2>Protocol deviations</h2><ul>")
        parts += [f"<li>{html.escape(d)}</li>" for d in model.deviations]
        parts.append("</ul>")
    if model.skipped:
        parts.append("<h2>Skipped</h2><ul>")
        parts += [
            f"<li>{html.escape(s['contender'])}: {html.escape(s['reason'])}</li>"
            for s in model.skipped
        ]
        parts.append("</ul>")
    parts.append("<h2>Coverage (expected versus completed decisions)</h2>")
    parts.append(_html_table(_COVERAGE_HEADER, _coverage_rows(model)))
    for title, header, rows in [*tables, *_analysis_tables(model)]:
        parts.append(f"<h2>{html.escape(title)}</h2>")
        parts.append(_html_table(header, rows))
    parts.append("<h2>Cardinality (S3)</h2>")
    parts.append(cardinality_svg(model.cardinality))
    parts.append("<h2>Reliability diagrams</h2>")
    for contender in model.contenders:
        parts.append(f"<h3>{html.escape(contender)}</h3>")
        parts.append(reliability_svg(contender, model.reliability[contender]))
    parts.append("<h2>Data and licenses</h2><ul>")
    parts += [
        "<li>banking77 (S1, S4 base items): PolyAI, CC BY 4.0; intent labels"
        " appear in the raw logs with this attribution.</li>",
        "<li>UCI SMS spam (S2): <a href='https://archive.ics.uci.edu/dataset/228/sms+spam+collection'>"
        "UCI identifies the collection as CC BY 4.0</a>. Text remains local under the "
        "project's conservative publication policy.</li>",
        "<li>S3 and S5 are synthetic, generated by this repository from seed 20260918.</li>",
    ]
    parts.append("</ul>")
    parts.append("</body></html>")
    return "".join(parts)


def render_report(
    run_dir: Path,
    out_dir: Path,
    extra_run_dirs: list[Path] | None = None,
    name: str | None = None,
    allow_protocol_mix: bool = False,
    correction_note_path: Path | None = None,
) -> Path:
    """Render MD, HTML, SVGs, JSON summary, and the raw archive."""
    validate_table_specs()
    run_dirs = [run_dir, *(extra_run_dirs or [])]
    runs = [load_run(d) for d in run_dirs]
    root = run_dir.resolve().parents[1]
    model = build_report_model(runs, root, allow_protocol_mix=allow_protocol_mix)
    if correction_note_path is not None:
        model.correction_note = correction_note_path.read_text(encoding="utf-8").strip()

    out_dir.mkdir(parents=True, exist_ok=True)
    name = name or run_dir.name
    (out_dir / "cardinality.svg").write_text(cardinality_svg(model.cardinality), encoding="utf-8")
    for contender in model.contenders:
        path = out_dir / f"reliability-{_safe(contender)}.svg"
        path.write_text(reliability_svg(contender, model.reliability[contender]), encoding="utf-8")

    json_path = out_dir / f"{name}.cells.json"
    json_path.write_text(
        json.dumps(model.json_summary(), indent=2, default=str) + "\n", encoding="utf-8"
    )
    (out_dir / f"{name}.md").write_text(render_md(model, json_path), encoding="utf-8")
    (out_dir / f"{name}.html").write_text(
        render_html(model, _metric_tables(model)), encoding="utf-8"
    )
    archive_runs(run_dirs, out_dir / f"{name}-raw.tar.gz")
    return out_dir
