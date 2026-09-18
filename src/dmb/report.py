"""Render the published report from run directories.

Everything here derives from the run directory plus the frozen, hashed
item files: tables, one cardinality plot, one reliability diagram per
contender, and a raw archive. S2 state text is scrubbed from the archive
(the SMS spam license does not cover republication of item text).

The report is generated, never hand-edited; numbers recompute from
``results.jsonl`` and the manifests.
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

from .metrics import accuracy, brier, confidence_spread, ece, flip_rate, macro_f1
from .prices import Price, price_for
from .suites.build import SUITE_ORDER
from .suites.items import DecisionItem, load_items, sha256_file

SUITE_TITLES: dict[str, str] = {
    "s1_intent77": "S1 intent77 (77-way banking)",
    "s2_spam": "S2 SMS spam (2-way)",
    "s3_cardinality": "S3 cardinality sweep",
    "s4_order": "S4 order stability",
    "s5_confidence": "S5 confidence honesty",
}

# Fields that may carry item text; scrubbed in the published archive.
_REDACT_KEYS = {"content", "text", "thinking", "state", "detail", "error", "system", "input"}
_REDACTED = "[redacted: s2 license]"

PALETTE = [
    "#2455e4", "#d42a2a", "#187a3b", "#a21caf", "#b45309", "#0e7490",
    "#5b21b6", "#991b1b", "#4d7c0f", "#be185d", "#374151", "#7c2d12",
]


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


def load_items_verified(root: Path, manifest: dict) -> dict[str, dict[str, DecisionItem]]:
    """Load the frozen item files, verifying each hash against the manifest.

    Raises if an item file changed after the run: report numbers must
    correspond to the exact items that were scored.
    """
    items_by_suite: dict[str, dict[str, DecisionItem]] = {}
    for suite_id, expected_hash in manifest.get("item_files", {}).items():
        path = root / "data" / "suites" / f"{suite_id}.jsonl"
        actual = sha256_file(path)
        if actual != expected_hash:
            raise ValueError(
                f"item file {path} hash {actual[:12]} != manifest {expected_hash[:12]}; "
                "the frozen items changed after the run"
            )
        items_by_suite[suite_id] = {item.item_id: item for item in load_items(path)}
    return items_by_suite


# ---- aggregation -----------------------------------------------------------


@dataclass
class CellMetrics:
    """Aggregates for one contender-suite cell."""

    contender: str
    suite: str
    n_rows: int = 0
    n_items: int = 0
    n_ok: int = 0
    n_malformed: int = 0
    n_failed: int = 0
    accuracy: float | None = None
    macro_f1: float | None = None
    ece: float | None = None
    brier: float | None = None
    latencies: dict[str, float] = field(default_factory=dict)
    cost_per_1000: float | None = None
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
            "n_rows": self.n_rows,
            "n_items": self.n_items,
            "n_ok": self.n_ok,
            "n_malformed": self.n_malformed,
            "n_failed": self.n_failed,
            "accuracy": self.accuracy,
            "macro_f1": self.macro_f1,
            "ece": self.ece,
            "brier": self.brier,
            "p50_ms": self.latencies.get("p50_ms"),
            "p95_ms": self.latencies.get("p95_ms"),
            "p99_ms": self.latencies.get("p99_ms"),
            "cost_per_1000_usd": self.cost_per_1000,
            "flip_rate": self.flip_rate,
            "mean_conf_range": self.conf_range,
            "admits_ignorance": self.admits_ignorance,
            "mean_conf_no_good": self.mean_conf_no_good,
        }


def _price_or_none(contender: str) -> Price | None:
    try:
        return price_for(contender)
    except KeyError:
        return None


def aggregate_cell(
    contender: str,
    suite_id: str,
    rows: list[dict],
    items: dict[str, DecisionItem],
) -> CellMetrics:
    """Compute every metric for one contender-suite cell.

    Accuracy, macro-F1, ECE, and Brier run over valid decisions on items
    with a gold label (gold >= 0). Malformed and failed attempts count in
    their own columns, never as wrong answers. Latency percentiles run
    over valid rows. Cost covers every attempt that reported usage.
    """
    cell = CellMetrics(contender=contender, suite=suite_id)
    cell.n_rows = len(rows)
    cell.n_items = len({r["item_id"] for r in rows})
    cell.n_ok = sum(1 for r in rows if r["ok"])
    cell.n_malformed = sum(1 for r in rows if r["malformed"])
    cell.n_failed = sum(1 for r in rows if not r["ok"] and not r["malformed"])

    scored = [r for r in rows if r["ok"] and r["gold_index"] >= 0]
    if scored:
        preds = [r["choice_index"] for r in scored]
        golds = [r["gold_index"] for r in scored]
        confs = [r["confidence"] for r in scored]
        corrects = [r["correct"] for r in scored]
        cell.accuracy = accuracy(preds, golds)
        num_classes = 1 + max(item.gold_index for item in items.values()
                              if item.gold_index >= 0) if items else 0
        if num_classes:
            cell.macro_f1 = macro_f1(preds, golds, num_classes)
        cell.ece = ece(confs, corrects)
        cell.brier = brier(confs, corrects)

    ok_rows = [r for r in rows if r["ok"]]
    if ok_rows:
        cell.latencies = {
            "p50_ms": _percentile([r["latency_ms"] for r in ok_rows], 50),
            "p95_ms": _percentile([r["latency_ms"] for r in ok_rows], 95),
            "p99_ms": _percentile([r["latency_ms"] for r in ok_rows], 99),
        }

    price = _price_or_none(contender)
    if price:
        total = sum(
            price.cost_usd(r["input_tokens"] or 0, r["output_tokens"] or 0) for r in rows
        )
        cell.cost_per_1000 = total / len(rows) * 1000 if rows else None

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


def aggregate_all(
    runs: list[RunData],
    items_by_suite: dict[str, dict[str, DecisionItem]],
) -> dict[tuple[str, str], CellMetrics]:
    """Aggregate every contender-suite cell across the merged runs.

    Later runs (the ``--extra`` jev run) win for a cell they contain.
    """
    rows_by_cell: dict[tuple[str, str], list[dict]] = {}
    for run in runs:  # later runs override earlier ones per cell
        for row in run.rows:
            rows_by_cell.setdefault((row["contender"], row["suite"]), []).append(row)
    cells: dict[tuple[str, str], CellMetrics] = {}
    for (contender, suite_id), rows in rows_by_cell.items():
        cells[(contender, suite_id)] = aggregate_cell(
            contender, suite_id, rows, items_by_suite.get(suite_id, {})
        )
    return cells


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
    """Two-panel SVG: accuracy versus N and p50 latency versus N (log x)."""
    ns = sorted({n for points in series.values() for n in points})
    if not ns:
        return "<svg xmlns='http://www.w3.org/2000/svg' width='720' height='60'></svg>"

    def x_of(n: int, left: float, width: float) -> float:
        frac = math.log2(n) / math.log2(ns[-1])
        return left + frac * width

    w, h, pad = 720.0, 320.0, 46.0
    panel_w = (w - 2 * pad - 30) / 2
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
        top, height = 40.0, h - 90
        out.append(
            f"<text x='{left}' y='36'>{label}</text>"
            f" <rect x='{left}' y='{top}' width='{panel_w}' height='{height}' "
            f"fill='#f8f8f8' stroke='#ccc'/>"
        )
        # x ticks for every N
        for n in ns:
            x = x_of(n, left, panel_w)
            out.append(
                f"<line x1='{x:.1f}' y1='{top}' x2='{x:.1f}' y2='{top + height}' "
                f"stroke='#e4e4e4'/>"
                f"<text x='{x:.1f}' y='{top + height + 14}' text-anchor='middle'>{n}</text>"
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
        for idx, (_name, points) in enumerate(sorted(series.items())):
            color = PALETTE[idx % len(PALETTE)]
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
    """Pack run directories into a tar.gz, scrubbing the licensed suite."""
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
class ReportModel:
    """Everything the renderers need, computed once."""

    runs: list[RunData]
    cells: dict[tuple[str, str], CellMetrics]
    contenders: list[str]
    suites: list[str]
    cardinality: dict[str, dict[int, dict[str, float]]]
    reliability: dict[str, list[dict]]
    spend_usd: float
    deviations: list[str]
    notes: list[str]
    skipped: list[dict]

    def json_summary(self) -> dict:
        return {
            "runs": [r.run_id for r in self.runs],
            "spend_usd": self.spend_usd,
            "cells": [c.as_dict() for c in self.cells.values()],
        }


def build_report_model(runs: list[RunData], root: Path) -> ReportModel:
    """Aggregate runs into the report model."""
    items_by_suite = load_items_verified(root, runs[0].manifest)
    cells = aggregate_all(runs, items_by_suite)
    contenders = contender_order(cells)

    cardinality: dict[str, dict[int, dict[str, float]]] = {}
    for contender in contenders:
        rows = [
            r for run in runs for r in run.rows
            if r["contender"] == contender and r["suite"] == "s3_cardinality"
        ]
        by_n: dict[int, dict[str, float]] = {}
        items = items_by_suite.get("s3_cardinality", {})
        for n in sorted({i.meta["N"] for i in items.values()}):
            ids = {i.item_id for i in items.values() if i.meta["N"] == n}
            n_rows = [r for r in rows if r["item_id"] in ids]
            cell = aggregate_cell(contender, "s3_cardinality", n_rows, items)
            if cell.n_rows:
                by_n[n] = {
                    "accuracy": cell.accuracy,
                    "p50_ms": cell.latencies.get("p50_ms"),
                    "n_rows": float(cell.n_rows),
                }
        if by_n:
            cardinality[contender] = by_n

    reliability = {
        contender: reliability_bins(
            [r for run in runs for r in run.rows if r["contender"] == contender]
        )
        for contender in contenders
    }

    deviations: list[str] = []
    notes: list[str] = []
    skipped: list[dict] = []
    spend = 0.0
    for run in runs:
        deviations.extend(run.manifest.get("deviations", []))
        notes.extend(f"[{run.run_id}] {n}" for n in run.manifest.get("notes", []))
        skipped.extend(run.manifest.get("skipped", []))
        spend += run.manifest.get("spend_usd", 0.0)
    return ReportModel(
        runs=runs,
        cells=cells,
        contenders=contenders,
        suites=[s for s in SUITE_ORDER if any(k[1] == s for k in cells)],
        cardinality=cardinality,
        reliability=reliability,
        spend_usd=spend,
        deviations=sorted(set(deviations)),
        notes=notes,
        skipped=skipped,
    )


def _metric_tables(model: ReportModel) -> list[tuple[str, list[str], list[list[str]]]]:
    """One table per metric: (title, header, rows)."""
    pct = lambda v: _fmt_pct(v)  # noqa: E731
    tables: list[tuple[str, list[str], list[list[str]]]] = []
    specs = [
        ("Accuracy (percent, valid rows, gold items)", "accuracy", pct),
        ("Macro-F1 (absent classes count as 0)", "macro_f1", _fmt_f),
        ("ECE (10 equal-width bins)", "ece", _fmt_f),
        ("Brier score", "brier", _fmt_f),
        ("Malformed rate (percent of rows)", "_malformed_rate", pct),
        ("Failed attempts (rate, percent of rows)", "_failed_rate", pct),
        ("Latency p50 (ms)", "p50_ms", _fmt_ms),
        ("Latency p95 (ms)", "p95_ms", _fmt_ms),
        ("Latency p99 (ms)", "p99_ms", _fmt_ms),
        ("Cost per 1,000 requested decisions (USD)", "cost_per_1000_usd", _fmt_usd),
        ("S4 flip rate (percent of base items)", "flip_rate", pct),
        ("S4 mean confidence range across permutations", "mean_conf_range", _fmt_f),
        ("S5 admits-ignorance rate (confidence <= 0.5 on no-good items, percent)",
         "admits_ignorance", pct),
        ("S5 mean confidence on no-good items", "mean_conf_no_good", _fmt_f),
    ]
    for title, key, fmt in specs:
        rows = []
        for contender in model.contenders:
            row = [contender]
            for suite in model.suites:
                cell = model.cells.get((contender, suite))
                if cell is None:
                    row.append("-")
                    continue
                if key == "_malformed_rate":
                    row.append(pct(cell.n_malformed / cell.n_rows if cell.n_rows else None))
                elif key == "_failed_rate":
                    row.append(pct(cell.n_failed / cell.n_rows if cell.n_rows else None))
                elif key in ("p50_ms", "p95_ms", "p99_ms"):
                    row.append(fmt(cell.latencies.get(key)))
                elif key in ("flip_rate", "mean_conf_range") and suite != "s4_order" or (
                    key in ("admits_ignorance", "mean_conf_no_good")
                    and suite != "s5_confidence"
                ):
                    row.append("")
                else:
                    row.append(fmt(getattr(cell, key, None)))
            rows.append(row)
        header = ["contender"] + [SUITE_TITLES.get(s, s) for s in model.suites]
        tables.append((title, header, rows))
    return tables


def render_md(model: ReportModel, json_path: Path | None = None) -> str:
    """The full Markdown report."""
    lines = [
        "# Decision-model benchmark: results",
        "",
        f"Runs: {', '.join(r.run_id for r in model.runs)}.",
        f"Total spend from usage fields: ${model.spend_usd:.2f} "
        "(list prices, dated in the price table).",
        "",
        "Every number in this file recomputes from `results.jsonl` and the",
        "manifests in the raw archive. Accuracy, macro-F1, ECE, and Brier cover",
        "valid decisions on items with a gold label; malformed and failed",
        "attempts have their own columns. Latency percentiles cover valid rows",
        "across all repeats. Cost covers every attempt that reported usage.",
        "S5 accuracy covers the underdetermined items only; the honesty columns",
        "cover the no-good-option items.",
        "",
    ]
    if model.deviations:
        lines += ["## Protocol deviations (recorded, not edited)", ""]
        lines += [f"- {d}" for d in model.deviations] + [""]
    if model.skipped:
        lines += ["## Skipped", ""]
        lines += [f"- {s['contender']}: {s['reason']}" for s in model.skipped] + [""]
    if model.notes:
        lines += ["## Run notes", ""]
        lines += [f"- {n}" for n in model.notes] + [""]

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
        f"Total spend from usage fields: ${model.spend_usd:.2f}.</p>",
        "<p>Every number recomputes from <code>results.jsonl</code> and the manifests",
        " in the raw archive. Accuracy, macro-F1, ECE, and Brier cover valid",
        " decisions on gold-labelled items; malformed and failed attempts have",
        " their own columns. Cost covers every attempt that reported usage.</p>",
    ]
    if model.deviations:
        parts.append("<h2>Protocol deviations</h2><ul>")
        parts += [f"<li>{html.escape(d)}</li>" for d in model.deviations]
        parts.append("</ul>")
    if model.skipped:
        parts.append("<h2>Skipped</h2><ul>")
        parts += [f"<li>{html.escape(s['contender'])}: {html.escape(s['reason'])}</li>"
                  for s in model.skipped]
        parts.append("</ul>")
    for title, header, rows in tables:
        parts.append(f"<h2>{html.escape(title)}</h2>")
        parts.append(_html_table(header, rows))
    parts.append("<h2>Cardinality (S3)</h2>")
    parts.append(cardinality_svg(model.cardinality))
    parts.append("<h2>Reliability diagrams</h2>")
    for contender in model.contenders:
        parts.append(f"<h3>{html.escape(contender)}</h3>")
        parts.append(reliability_svg(contender, model.reliability[contender]))
    parts.append("</body></html>")
    return "".join(parts)


def render_report(
    run_dir: Path,
    out_dir: Path,
    extra_run_dirs: list[Path] | None = None,
) -> Path:
    """Render MD, HTML, SVGs, JSON summary, and the raw archive."""
    run_dirs = [run_dir, *(extra_run_dirs or [])]
    runs = [load_run(d) for d in run_dirs]
    root = run_dir.resolve().parents[1]
    model = build_report_model(runs, root)

    out_dir.mkdir(parents=True, exist_ok=True)
    name = run_dir.name
    (out_dir / "cardinality.svg").write_text(
        cardinality_svg(model.cardinality), encoding="utf-8"
    )
    for contender in model.contenders:
        path = out_dir / f"reliability-{_safe(contender)}.svg"
        path.write_text(
            reliability_svg(contender, model.reliability[contender]), encoding="utf-8"
        )

    json_path = out_dir / f"{_safe(name)}.cells.json"
    json_path.write_text(
        json.dumps(model.json_summary(), indent=2, default=str) + "\n", encoding="utf-8"
    )
    (out_dir / f"{name}.md").write_text(render_md(model, json_path), encoding="utf-8")
    (out_dir / f"{name}.html").write_text(
        render_html(model, _metric_tables(model)), encoding="utf-8"
    )
    archive_runs(run_dirs, out_dir / f"{name}-raw.tar.gz")
    return out_dir
