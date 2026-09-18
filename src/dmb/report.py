"""Render the published report from run directories.

Everything here derives from the run directories plus the frozen, hashed
item files: tables, one cardinality plot, one reliability diagram per
contender, and a raw archive. S2 state text is scrubbed from the archive
(the SMS spam license does not cover republication of item text).

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

import html
import io
import json
import math
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .metrics import accuracy, brier, confidence_spread, ece, flip_rate, macro_f1_labeled
from .prices import PRICES, Price, load_snapshot, prices_table_hash
from .suites.build import SUITE_ORDER
from .suites.items import DecisionItem, load_items, sha256_file

SUITE_TITLES: dict[str, str] = {
    "s1_intent77": "S1 intent77 (77-way banking)",
    "s2_spam": "S2 SMS spam (2-way)",
    "s3_cardinality": "S3 cardinality sweep",
    "s4_order": "S4 order stability",
    "s5_confidence": "S5 confidence honesty",
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

# Fields that may carry item text; scrubbed in the published archive.
_REDACT_KEYS = {"content", "text", "thinking", "state", "detail", "error", "system", "input"}
_REDACTED = "[redacted: s2 license]"

PALETTE = [
    "#2455e4", "#d42a2a", "#187a3b", "#a21caf", "#b45309", "#0e7490",
    "#5b21b6", "#991b1b", "#4d7c0f", "#be185d", "#374151", "#7c2d12",
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
    protocol = dict(manifest.get("protocol", {}))
    for key, default in _V1_DEFAULTS.items():
        protocol.setdefault(key, default)
    return protocol


def _prices_for_run(run: RunData) -> dict[str, Price]:
    """Prices for one run: its stored snapshot, or the current table.

    A run without a snapshot (protocol v1) must have recorded the current
    table's hash; otherwise the current table would silently reprice an
    old report.
    """
    snapshot = run.dir / "prices.snapshot.json"
    if snapshot.exists():
        return load_snapshot(json.loads(snapshot.read_text(encoding="utf-8")))
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
    items_by_suite: dict[str, dict[str, DecisionItem]] = {}
    hashes_by_suite: dict[str, tuple[str, str]] = {}
    for run in runs:
        for suite_id, expected_hash in run.manifest.get("item_files", {}).items():
            path = root / "data" / "suites" / f"{suite_id}.jsonl"
            if not path.exists():
                raise MergeError(
                    f"run {run.run_id} references item file {path} which is missing"
                )
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
            items_by_suite[suite_id] = {
                item.item_id: item for item in load_items(path)
            }

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
                    raise MergeError(message + " (pass --allow-protocol-mix to "
                                        "merge anyway with a recorded note)")
                break  # one recorded note per run pair is enough

    for run in runs:
        for row in run.rows:
            suite_id = row.get("suite")
            items = items_by_suite.get(suite_id)
            if items is None:
                raise MergeError(
                    f"run {run.run_id} row {row.get('item_id')!r} references "
                    f"suite {suite_id!r} whose item file was not verified"
                )
            item = items.get(row.get("item_id"))
            if item is None:
                raise MergeError(
                    f"run {run.run_id} row references unknown item "
                    f"{row.get('item_id')!r} in suite {suite_id}"
                )
            if row.get("gold_index") != item.gold_index:
                raise MergeError(
                    f"run {run.run_id} row {item.item_id}: gold "
                    f"{row.get('gold_index')} != frozen item gold "
                    f"{item.gold_index}"
                )
            choice = row.get("choice_index")
            if choice is not None and not 0 <= choice < len(item.options):
                raise MergeError(
                    f"run {run.run_id} row {item.item_id}: choice {choice} "
                    f"out of range for {len(item.options)} options"
                )
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
            rows_by_cell.setdefault(
                (row.get("contender"), row.get("suite")), []
            ).append(row)
        for key in set(manifest_cells) | set(rows_by_cell):
            rows = rows_by_cell.get(key, [])
            seen: set[tuple[str, int]] = set()
            for row in rows:
                row_key = (row.get("item_id"), row.get("repeat"))
                if row_key in seen:
                    raise MergeError(
                        f"run {run.run_id} cell {key[0]}/{key[1]} has duplicate "
                        f"item-repeat {row_key}; cannot aggregate it"
                    )
                seen.add(row_key)
            selection[key] = SelectedCell(
                run=run, manifest_cell=manifest_cells.get(key), rows=rows
            )
    return selection


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
    # S4
    flip_rate: float | None = None
    conf_range: float | None = None
    # S5
    admits_ignorance: float | None = None
    mean_conf_no_good: float | None = None

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
            "flip_rate": self.flip_rate,
            "mean_conf_range": self.conf_range,
            "admits_ignorance": self.admits_ignorance,
            "mean_conf_no_good": self.mean_conf_no_good,
        }


def _attempt_costs(
    run_dir: Path, contender: str, suite_id: str, price: Price
) -> tuple[float, int, int]:
    """Cost over every recorded attempt when the run kept attempt logs.

    Returns ``(cost_usd, attempts_with_usage, attempts_without_usage)``.
    A missing attempts file means no request ever started: a measured zero.
    """
    safe = contender.replace(":", "__")
    path = run_dir / "raw" / f"{safe}.{suite_id}.attempts.jsonl"
    if not path.exists():
        return 0.0, 0, 0
    cost = 0.0
    known = unknown = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        in_tok, out_tok = record.get("input_tokens"), record.get("output_tokens")
        if in_tok is None or out_tok is None:
            unknown += 1
            continue
        cost += price.cost_usd(in_tok, out_tok)
        known += 1
    return cost, known, unknown


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
    cell.n_rows = len(rows)
    cell.n_items = len({r["item_id"] for r in rows})
    cell.n_ok = sum(1 for r in rows if r["ok"])
    cell.n_malformed = sum(1 for r in rows if r["malformed"])
    cell.n_failed = sum(1 for r in rows if not r["ok"] and not r["malformed"])

    if manifest_cell is not None:
        cell.status = manifest_cell.get("status", "ok")
        cell.stop_reason = manifest_cell.get("abort_reason")
        cell.expected_decisions = int(manifest_cell.get("expected_rows") or 0)
    if not cell.expected_decisions:
        repeats = max(1, len({r["repeat"] for r in rows}) or 1)
        cell.expected_decisions = len(items) * repeats if items else cell.n_rows
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
            pred_labels = [
                items[r["item_id"]].options[r["choice_index"]] for r in scored
            ]
            gold_labels = [
                items[r["item_id"]].options[r["gold_index"]] for r in scored
            ]
            classes = sorted({o for item in items.values() for o in item.options})
            cell.macro_f1 = macro_f1_labeled(pred_labels, gold_labels, classes)
        cell.ece = ece(confs, corrects)
        cell.brier = brier(confs, corrects)
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
        elif selected is not None and (
            selected.run.dir / "raw" / f"{contender.replace(':', '__')}.{suite_id}.attempts.jsonl"
        ).exists():
            cost, known, unknown = _attempt_costs(
                selected.run.dir, contender, suite_id, price
            )
            cell.cost_usd = cost
            cell.cost_per_1000 = cost / cell.n_rows * 1000 if cell.n_rows else None
            cell.cost_scope = "every recorded attempt of this cell"
            if unknown:
                cell.cost_incomplete_reason = (
                    f"{unknown} attempts reported no usage; their charge is unknown"
                )
        else:
            with_usage = [
                r for r in rows
                if r.get("input_tokens") is not None
                and r.get("output_tokens") is not None
            ]
            unknown = cell.n_rows - len(with_usage)
            retried = sum(1 for r in rows if r.get("retries"))
            total = sum(
                price.cost_usd(r["input_tokens"], r["output_tokens"])
                for r in with_usage
            )
            cell.cost_usd = total
            cell.cost_per_1000 = total / cell.n_rows * 1000 if cell.n_rows else None
            cell.cost_scope = "final-attempt usage of completed decisions"
            if retried:
                cell.cost_incomplete_reason = (
                    f"{retried} decisions retried; the discarded first attempts' "
                    "usage was not recorded, so cost is a lower bound"
                )
            elif unknown:
                cell.cost_incomplete_reason = (
                    f"{unknown} decisions reported no usage; their charge is unknown"
                )

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
        cell.flip_rate = flip_rate(by_base)
        cell.conf_range = confidence_spread(conf_by_base)["mean_range"]

    # S5: honesty metrics on no-good-option items (gold -1).
    no_good = [r for r in ok_rows if r["gold_index"] < 0]
    if no_good:
        cell.admits_ignorance = sum(1 for r in no_good if r["confidence"] <= 0.5) / len(
            no_good
        )
        cell.mean_conf_no_good = sum(r["confidence"] for r in no_good) / len(no_good)

    return cell


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
            n for n, p in points.items()
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
        (("accuracy", "accuracy", lambda v: f"{v:.2f}"),
         ("p50_ms", "p50 latency (ms)", lambda v: f"{v:,.0f}"))
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
                f"<text x='{x:.1f}' y='{top + height + 14}' "
                f"text-anchor='middle'>{n}</text>"
            ) if n in labeled else ""
            out.append(
                f"<line x1='{x:.1f}' y1='{top}' x2='{x:.1f}' y2='{top + height}' "
                f"stroke='#e4e4e4'/>{tick}"
            )
        all_y = [
            p[key]
            for points in series.values()
            for p in points.values()
            if p.get(key) is not None
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
        text = name + (
            f" (ends at N={legend_last[name]})" if name in legend_last else ""
        )
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
        f"<text x='{pad}' y='20'>{html.escape(name)}: reliability (pooled suites)</text>",
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
        f"<text x='{w - pad}' y='{top - 6}' text-anchor='end'>perfect calibration</text>",
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
            out.append(
                f"<circle cx='{x + bw / 2:.1f}' cy='{cy:.1f}' r='3.5' fill='#d42a2a'/>"
            )
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
    bins = [{"lo": i / 10, "n": 0, "conf_sum": 0.0, "acc_sum": 0} for i in range(10)]
    for r in scored:
        conf = min(max(r["confidence"], 0.0), 1.0)
        idx = min(int(conf * 10), 9)
        bins[idx]["n"] += 1
        bins[idx]["conf_sum"] += conf
        bins[idx]["acc_sum"] += 1 if r["correct"] else 0
    out = []
    for b in bins:
        n = b["n"]
        out.append({
            "lo": b["lo"],
            "n": n,
            "conf": b["conf_sum"] / n if n else None,
            "acc": b["acc_sum"] / n if n else 0.0,
        })
    return out


# ---- archive ---------------------------------------------------------------


def _scrub(value, depth: int = 0) -> object:
    """Replace strings under redaction keys; recurse into dicts and lists."""
    if depth > 8:
        return value
    if isinstance(value, dict):
        return {
            k: (_REDACTED if isinstance(v, str) and k in _REDACT_KEYS else _scrub(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub(v, depth + 1) for v in value]
    return value


def archive_runs(run_dirs: list[Path], out_path: Path, redact_suite: str = "s2_spam") -> Path:
    """Pack run directories into a tar.gz, scrubbing the licensed suite.

    Every ``.jsonl`` file whose name carries the licensed suite id is
    scrubbed - decision logs and attempt logs alike.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out_path, "w:gz") as tar:
        for run_dir in run_dirs:
            for file in sorted(run_dir.rglob("*")):
                if not file.is_file():
                    continue
                arcname = f"{run_dir.name}/{file.relative_to(run_dir)}"
                if redact_suite in file.name and file.suffix == ".jsonl":
                    scrubbed = "".join(
                        json.dumps(_scrub(json.loads(line)), default=str) + "\n"
                        for line in file.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    )
                    info = tarfile.TarInfo(arcname)
                    info.size = len(scrubbed.encode("utf-8"))
                    tar.addfile(info, io.BytesIO(scrubbed.encode("utf-8")))
                else:
                    tar.add(file, arcname=arcname)
    return out_path


# ---- rendering -------------------------------------------------------------


def _md_table(header: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join("---" for _ in header) + "|"]
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
KNOWN_METRIC_KEYS = (
    set(CellMetrics(contender="", suite="").as_dict()) | _COMPUTED_KEYS
)


def _table_specs() -> list[TableSpec]:
    pct = _fmt_pct
    return [
        TableSpec(
            "Accuracy (percent, valid rows, gold items)",
            "accuracy", pct, star_partial=True,
        ),
        TableSpec(
            "Macro-F1 (stable option labels; absent classes count as 0)",
            "macro_f1", _fmt_f, suites=LABEL_SUITES, out_of_scope="n/a",
            star_partial=True,
        ),
        TableSpec("ECE (10 equal-width bins)", "ece", _fmt_f),
        TableSpec("Brier score", "brier", _fmt_f),
        TableSpec(
            "Malformed rate (percent of completed decisions)",
            "_malformed_rate", pct,
        ),
        TableSpec(
            "Failed attempts (rate, percent of completed decisions)",
            "_failed_rate", pct,
        ),
        TableSpec("Latency p50 (ms)", "p50_ms", _fmt_ms),
        TableSpec("Latency p95 (ms)", "p95_ms", _fmt_ms),
        TableSpec("Latency p99 (ms)", "p99_ms", _fmt_ms),
        TableSpec(
            "Cost per 1,000 requested decisions (USD)",
            "cost_per_1000_usd", _fmt_usd,
        ),
        TableSpec(
            "S4 flip rate (percent of base items)",
            "flip_rate", pct, suites=frozenset({"s4_order"}),
        ),
        TableSpec(
            "S4 mean confidence range across permutations",
            "mean_conf_range", _fmt_f, suites=frozenset({"s4_order"}),
        ),
        TableSpec(
            "S5 admits-ignorance rate (confidence <= 0.5 on no-good items, percent)",
            "admits_ignorance", pct, suites=frozenset({"s5_confidence"}),
        ),
        TableSpec(
            "S5 mean confidence on no-good items",
            "mean_conf_no_good", _fmt_f, suites=frozenset({"s5_confidence"}),
        ),
    ]


def validate_table_specs() -> None:
    """Every configured metric key must exist in the one representation."""
    for spec in _table_specs():
        if spec.key not in KNOWN_METRIC_KEYS:
            raise KeyError(
                f"table spec {spec.title!r} uses unknown metric key {spec.key!r}"
            )


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
            rows.append([
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
            ])
    return rows


_COVERAGE_HEADER = [
    "contender", "suite", "status", "expected", "completed", "valid",
    "malformed", "failed", "completion %", "valid %", "stop reason",
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
    selected_spend_usd: float
    deviations: list[str]
    notes: list[str]
    skipped: list[dict]
    protocol_notes: list[str]
    latency_scope: str
    correction_note: str | None = None

    def json_summary(self) -> dict:
        return {
            "runs": [r.run_id for r in self.runs],
            "spend_usd": self.spend_usd,
            "selected_cells_spend_usd": round(self.selected_spend_usd, 4),
            "latency_scope": self.latency_scope,
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
            r
            for key, selected in selection.items()
            if key[0] == contender
            for r in selected.rows
        ]
        reliability[contender] = reliability_bins(pooled)

    deviations: list[str] = []
    notes: list[str] = []
    skipped: list[dict] = []
    protocol_notes: list[str] = []
    spend = 0.0
    for run in runs:
        deviations.extend(run.manifest.get("deviations", []))
        notes.extend(f"[{run.run_id}] {n}" for n in run.manifest.get("notes", []))
        for contender in run.manifest.get("contenders", []):
            for note in contender.get("notes", []):
                notes.append(f"[{run.run_id}] {contender['name']}: {note}")
        skipped.extend(run.manifest.get("skipped", []))
        spend += run.manifest.get("spend_usd", 0.0)

    if allow_protocol_mix:
        versions = sorted({
            _protocol_of(run.manifest).get("protocol_version", "v1")
            for run in runs
        })
        protocol_notes.append(
            "Mixed protocol versions merged by explicit policy: "
            + ", ".join(versions)
            + ". Replacement cells from different versions may differ in "
            "latency scope, prompt text, and record format; the per-cell "
            "source run column says which version each cell came from."
        )
    unknown_costs = [
        c for c in cells.values() if c.cost_incomplete_reason
    ]
    if unknown_costs:
        protocol_notes.append(
            "Cost caveats: some cells report incomplete usage. Unknown usage "
            "is not a measured zero; see the cost table markers and the "
            "coverage table."
        )

    latency_scopes = {
        _protocol_of(run.manifest).get("latency_scope", "request") for run in runs
    }
    latency_scope = (
        "mixed" if len(latency_scopes) > 1 else next(iter(latency_scopes), "request")
    )

    selected_spend = sum(
        c.cost_usd for c in cells.values() if c.cost_usd is not None
    )
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
    )


LATENCY_SCOPE_TEXT = {
    "request": (
        "Latency covers one provider request (the final attempt of each "
        "decision)."
    ),
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
        f"Total spend from usage fields: ${model.spend_usd:.2f} "
        "(list prices, dated in the price table; spend of the selected "
        f"cells: ${model.selected_spend_usd:.2f}).",
        "",
        "Every number in this file recomputes from `results.jsonl` and the",
        "manifests in the raw archive. Later runs replace earlier cells",
        "whole; each coverage row names its source run.",
        "",
        "Denominators:",
        "",
        "- Accuracy, macro-F1, ECE, and Brier cover valid decisions on",
        "  items with a gold label.",
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
        "## Coverage (expected versus completed decisions)", "",
        _md_table(_COVERAGE_HEADER, _coverage_rows(model)),
        "",
        "A cell is partial when it stopped early or returned malformed or",
        "failed decisions. Empty, failed, and skipped cells stay listed",
        "with their reasons.", "",
    ]
    for title, header, rows in _metric_tables(model):
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
        "- UCI SMS spam (S2): research use; item text stays local and is",
        "  scrubbed from the published archive.",
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
        f"Total spend from usage fields: ${model.spend_usd:.2f}; selected "
        f"cells: ${model.selected_spend_usd:.2f}.</p>",
        "<p>Every number recomputes from <code>results.jsonl</code> and the",
        " manifests in the raw archive. Accuracy, macro-F1, ECE, and Brier",
        " cover valid decisions on gold-labelled items; malformed and failed",
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
        parts += [f"<li>{html.escape(s['contender'])}: {html.escape(s['reason'])}</li>"
                  for s in model.skipped]
        parts.append("</ul>")
    parts.append("<h2>Coverage (expected versus completed decisions)</h2>")
    parts.append(_html_table(_COVERAGE_HEADER, _coverage_rows(model)))
    for title, header, rows in tables:
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
        "<li>UCI SMS spam (S2): research use; item text stays local and is"
        " scrubbed from the published archive.</li>",
        "<li>S3 and S5 are synthetic, generated by this repository from"
        " seed 20260918.</li>",
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
    (out_dir / "cardinality.svg").write_text(
        cardinality_svg(model.cardinality), encoding="utf-8"
    )
    for contender in model.contenders:
        path = out_dir / f"reliability-{_safe(contender)}.svg"
        path.write_text(
            reliability_svg(contender, model.reliability[contender]), encoding="utf-8"
        )

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
