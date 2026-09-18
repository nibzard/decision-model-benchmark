"""Report aggregation and rendering tests with synthetic run data.

Covers merge verification (finding 9), whole-cell replacement (finding 10),
stable-label macro-F1 (finding 1), and the metric field mapping plus
coverage tables (findings 8, 12).
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

import dmb.report as report
from dmb.prices import prices_table_hash
from dmb.report import (
    MergeError,
    aggregate_cell,
    archive_runs,
    build_report_model,
    cardinality_svg,
    load_run,
    reliability_bins,
    reliability_svg,
    render_report,
    select_cells,
    validate_table_specs,
    verify_runs,
)
from dmb.suites.items import DecisionItem, save_items, sha256_file


def _row(
    item_id: str,
    suite: str,
    choice: int | None,
    conf: float,
    gold: int,
    *,
    ok: bool = True,
    malformed: bool = False,
    latency_ms: float = 100.0,
    retries: int = 0,
    input_tokens: int | None = 100,
    output_tokens: int | None = 10,
) -> dict:
    return {
        "run_id": "test",
        "contender": "openai:test-model",
        "provider": "openai",
        "suite": suite,
        "item_id": item_id,
        "repeat": 0,
        "choice_index": choice,
        "confidence": conf,
        "correct": ok and choice == gold and gold >= 0,
        "ok": ok,
        "malformed": malformed,
        "error": None,
        "retries": retries,
        "latency_ms": latency_ms,
        "latency_scope": "decision",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "gold_index": gold,
    }


# ---- cell aggregation -------------------------------------------------------


def _label_items(ids: list[str], gold: int) -> dict[str, DecisionItem]:
    return {
        k: DecisionItem(
            item_id=k, suite="s", state="x", options=["a", "b"],
            gold_index=gold,
        )
        for k in ids
    }


def test_aggregate_cell_basic_counts():
    rows = [
        _row("a", "s1_intent77", 0, 0.9, 0),
        _row("b", "s1_intent77", 1, 0.8, 0),  # wrong
        _row("c", "s1_intent77", None, None, 1, ok=False, malformed=True),
        _row("d", "s1_intent77", None, None, 1, ok=False),
    ]
    items = _label_items("abcd", 0)
    items["c"].gold_index = 1
    items["d"].gold_index = 1
    cell = aggregate_cell("openai:test-model", "s1_intent77", rows, items)
    assert cell.n_rows == 4
    assert cell.n_malformed == 1
    assert cell.n_failed == 1
    assert cell.accuracy == 0.5  # 1 of 2 valid rows
    assert cell.ece > 0.0
    assert cell.brier > 0.0
    assert cell.latencies["p50_ms"] == 100.0
    # Malformed and failed rows make the cell partial even at 100% accuracy
    # on valid rows.
    assert cell.partial is True


def test_macro_f1_uses_stable_option_labels():
    """Finding 1: macro-F1 over option texts, positions ignored.

    Two cells with identical label-level outcomes but permuted option
    orders must score identically; position-based macro-F1 would not.
    """
    straight_items = _label_items(["a", "b"], 0)
    straight_items["b"].gold_index = 1
    straight_rows = [
        _row("a", "s1_intent77", 0, 0.9, 0),  # predicts "a", gold "a"
        _row("b", "s1_intent77", 1, 0.8, 1),  # predicts "b", gold "b"
    ]
    straight = aggregate_cell(
        "openai:test-model", "s1_intent77", straight_rows, straight_items
    )
    # Permuted suite: options are ["b", "a"]; the same texts are picked.
    permuted_items = {
        k: DecisionItem(
            item_id=k, suite="s", state="x", options=["b", "a"], gold_index=1 - i,
        )
        for i, k in enumerate("ab")
    }
    permuted_rows = [
        _row("a", "s1_intent77", 1, 0.9, 1),  # picks "a" from position 1
        _row("b", "s1_intent77", 0, 0.8, 0),  # picks "b" from position 0
    ]
    permuted = aggregate_cell(
        "openai:test-model", "s1_intent77", permuted_rows, permuted_items
    )
    assert straight.macro_f1 == pytest.approx(1.0)
    assert permuted.macro_f1 == pytest.approx(1.0)
    assert straight.macro_f1 == permuted.macro_f1


def test_macro_f1_absent_classes_score_zero():
    """A constant predictor gets 0 for the class it never predicts."""
    ids = [f"i{k}" for k in range(10)]
    rows = [_row(f"i{k}", "s2_spam", 0, 0.9, 0 if k < 8 else 1) for k in range(10)]
    items = _label_items(ids, 0)
    for k, item in enumerate(items.values()):
        item.gold_index = 0 if k < 8 else 1
    cell = aggregate_cell("openai:test-model", "s2_spam", rows, items)
    # ham F1 = 2*8/(2*8+0+2) = 8/9; spam F1 = 0 (never predicted).
    assert cell.macro_f1 == pytest.approx((8 / 9 + 0.0) / 2)


def test_macro_f1_not_applicable_on_suites_with_moving_options():
    rows = [_row("n1", "s3_cardinality", 0, 0.9, 0)]
    items = {"n1": DecisionItem(
        item_id="n1", suite="s3", state="x", options=["w1", "w2"], gold_index=0,
        meta={"N": 2},
    )}
    cell = aggregate_cell("openai:test-model", "s3_cardinality", rows, items)
    assert cell.macro_f1 is None
    assert cell.macro_f1_applicable is False


def test_aggregate_cell_flip_rate_maps_through_permutation():
    # base item s1-1: perm p1 maps choice 0 -> old 5; p2 maps choice 1 -> old 5
    items = {
        "s1-1-p1": DecisionItem(
            item_id="s1-1-p1", suite="s4", state="x",
            options=["a", "b"], gold_index=0, base_item_id="s1-1",
            meta={"perm": [5, 7]},
        ),
        "s1-1-p2": DecisionItem(
            item_id="s1-1-p2", suite="s4", state="x",
            options=["a", "b"], gold_index=1, base_item_id="s1-1",
            meta={"perm": [7, 5]},
        ),
    }
    rows = [
        _row("s1-1-p1", "s4_order", 0, 0.8, 0),
        _row("s1-1-p2", "s4_order", 1, 0.6, 1),
    ]
    cell = aggregate_cell("openai:test-model", "s4_order", rows, items)
    # both map to old index 5: no flip
    assert cell.flip_rate == 0.0
    rows[1] = _row("s1-1-p2", "s4_order", 0, 0.6, 1)  # maps to 7: flip
    cell = aggregate_cell("openai:test-model", "s4_order", rows, items)
    assert cell.flip_rate == 1.0


def test_aggregate_cell_no_good_option_honesty():
    rows = [
        _row("ng1", "s5_confidence", 0, 0.3, -1),
        _row("ng2", "s5_confidence", 1, 0.7, -1),
        _row("ud1", "s5_confidence", 0, 0.9, 0),
    ]
    items = {
        r["item_id"]: DecisionItem(
            item_id=r["item_id"], suite="s5", state="x", options=["a", "b"],
            gold_index=r["gold_index"],
        )
        for r in rows
    }
    cell = aggregate_cell("openai:test-model", "s5_confidence", rows, items)
    assert cell.admits_ignorance == 0.5
    assert abs(cell.mean_conf_no_good - 0.5) < 1e-9
    assert cell.accuracy == 1.0  # only the gold-labelled row is scored


# ---- synthetic run fixtures -------------------------------------------------


def _write_run(
    root: Path,
    run_id: str,
    contender: str,
    suites: dict[str, list[dict]],
    *,
    protocol: dict | None = None,
    cells: list[dict] | None = None,
    attempts: dict[str, list[dict]] | None = None,
):
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True)
    item_files = {}
    for suite_id, rows in suites.items():
        items = [
            DecisionItem(
                item_id=r["item_id"],
                suite=suite_id,
                state="secret sms text" if suite_id == "s2_spam" else "state",
                options=["a", "b"],
                gold_index=r["gold_index"],
                meta={"perm": [0, 1]} if suite_id == "s4_order" else {"N": 2},
                base_item_id=r["item_id"] if suite_id == "s4_order" else None,
            )
            for r in rows
        ]
        path = root / "data" / "suites" / f"{suite_id}.jsonl"
        if not path.exists():
            save_items(items, path)
        item_files[suite_id] = sha256_file(path)
        raw = run_dir / "raw" / f"{contender.replace(':', '__')}.{suite_id}.jsonl"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(
            json.dumps({
                "item_id": rows[0]["item_id"] if rows else "x",
                "attempts": [{"detail": "secret sms text"}],
                "raw": {"response": {"content": "secret sms text"}},
            }) + "\n",
            encoding="utf-8",
        )
        if attempts is not None and suite_id in attempts:
            attempt_path = (
                run_dir / "raw" / f"{contender.replace(':', '__')}.{suite_id}.attempts.jsonl"
            )
            attempt_path.write_text(
                "".join(json.dumps(a) + "\n" for a in attempts[suite_id]),
                encoding="utf-8",
            )
    all_rows = [dict(r, contender=contender) for rows in suites.values() for r in rows]
    (run_dir / "results.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in all_rows), encoding="utf-8"
    )
    manifest = {
        "run_id": run_id,
        "item_files": item_files,
        "protocol": protocol or {
            "temperature": 0, "repeats": 1, "malformed_retry": 1,
            "transport_retry": 1, "rate_limit_backoff": "exponential",
        },
        "price_table_sha256": prices_table_hash(),
        "deviations": ["test deviation"],
        "notes": ["note"],
        "skipped": [],
        "spend_usd": 0.01,
        "cells": cells if cells is not None else [
            {"contender": contender, "suite": suite_id, "status": "ok",
             "abort_reason": None, "expected_rows": len(rows), "rows": len(rows)}
            for suite_id, rows in suites.items()
        ],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return run_dir


def _one_suite_rows() -> dict[str, list[dict]]:
    return {"s2_spam": [_row("i1", "s2_spam", 0, 0.9, 0), _row("i2", "s2_spam", 1, 0.4, 1)]}


# ---- verification (finding 9) -----------------------------------------------


def test_verify_runs_rejects_changed_item_file(tmp_path: Path):
    run_dir = _write_run(tmp_path, "v1", "openai:test-model", _one_suite_rows())
    # Tamper with the frozen file after the run.
    items = _one_suite_rows()
    path = tmp_path / "data" / "suites" / "s2_spam.jsonl"
    path.write_text(path.read_text() + json.dumps({"extra": True}) + "\n")
    with pytest.raises(MergeError, match="v1.*s2_spam.*hash"):
        verify_runs([load_run(run_dir)], tmp_path)
    _ = items


def test_verify_runs_rejects_conflicting_hashes_between_runs(
    tmp_path: Path, monkeypatch
):
    """Two runs recording different item hashes for one suite cannot merge."""
    run_a = _write_run(tmp_path, "a", "openai:test-model", _one_suite_rows())
    run_b = _write_run(tmp_path, "b", "openai:test-model", _one_suite_rows())
    a, b = load_run(run_a), load_run(run_b)
    a.manifest["item_files"]["s2_spam"] = "h1"
    b.manifest["item_files"]["s2_spam"] = "h2"
    calls = {"n": 0}

    def fake_hash(_path):
        calls["n"] += 1
        return "h1" if calls["n"] == 1 else "h2"

    monkeypatch.setattr(report, "sha256_file", fake_hash)
    with pytest.raises(MergeError, match="conflicting item hashes.*a.*b"):
        verify_runs([a, b], tmp_path)


def _append_row(run_dir: Path, row: dict) -> None:
    with (run_dir / "results.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def test_verify_runs_validates_rows_against_frozen_items(tmp_path: Path):
    run_dir = _write_run(tmp_path, "badref", "openai:test-model", _one_suite_rows())
    _append_row(run_dir, _row("ghost-item", "s2_spam", 0, 0.5, 0))
    with pytest.raises(MergeError, match="unknown item.*ghost-item"):
        verify_runs([load_run(run_dir)], tmp_path)


def test_verify_runs_rejects_out_of_range_choice(tmp_path: Path):
    run_dir = _write_run(tmp_path, "oor", "openai:test-model", _one_suite_rows())
    row = _row("i1", "s2_spam", 7, 0.5, 0)
    _append_row(run_dir, row)
    with pytest.raises(MergeError, match="choice 7 out of range"):
        verify_runs([load_run(run_dir)], tmp_path)


def test_verify_runs_rejects_gold_mismatch(tmp_path: Path):
    run_dir = _write_run(tmp_path, "badgold", "openai:test-model", _one_suite_rows())
    row = _row("i1", "s2_spam", 0, 0.5, gold=1)
    _append_row(run_dir, row)
    with pytest.raises(MergeError, match="gold"):
        verify_runs([load_run(run_dir)], tmp_path)


def test_protocol_mismatch_requires_explicit_policy(tmp_path: Path):
    v1 = _write_run(tmp_path, "v1", "openai:test-model", _one_suite_rows())
    v2 = _write_run(
        tmp_path, "v2", "openai:test-model", _one_suite_rows(),
        protocol={
            "temperature": 0, "repeats": 1, "malformed_retry": 1,
            "transport_retry": 1, "rate_limit_backoff": "exponential",
            "protocol_version": "v2", "latency_scope": "decision",
            "prompt_sha256": "abc",
        },
    )
    runs = [load_run(v1), load_run(v2)]
    with pytest.raises(MergeError, match="protocol_version.*--allow-protocol-mix"):
        verify_runs(runs, tmp_path)
    model = build_report_model(runs, tmp_path, allow_protocol_mix=True)
    assert any("Mixed protocol versions" in n for n in model.protocol_notes)
    assert model.latency_scope == "mixed"


def test_stale_price_table_refused_for_snapshotless_run(tmp_path: Path, monkeypatch):
    run_dir = _write_run(tmp_path, "old", "openai:test-model", _one_suite_rows())
    run = load_run(run_dir)
    run.manifest["price_table_sha256"] = "0" * 64
    monkeypatch.setattr(report, "PRICES", ())  # force a current-hash change
    with pytest.raises(MergeError, match="price"):
        report._prices_for_run(run)


# ---- replacement semantics (finding 10) --------------------------------------


def test_later_run_replaces_earlier_cell_whole(tmp_path: Path):
    first = _write_run(tmp_path, "v1", "openai:test-model", _one_suite_rows())
    better = [_row("i1", "s2_spam", 0, 0.9, 0), _row("i2", "s2_spam", 0, 0.9, 1)]
    second = _write_run(tmp_path, "v2", "openai:test-model", {"s2_spam": better})
    model = build_report_model([load_run(first), load_run(second)], tmp_path)
    cell = model.cells[("openai:test-model", "s2_spam")]
    assert cell.source_run == "v2"
    assert cell.n_rows == 2
    assert cell.accuracy == 0.5  # only from the replacing run


def test_later_failed_cell_does_not_fall_back_to_earlier_success(tmp_path: Path):
    """A later empty cell replaces the earlier one; coverage shows why."""
    first = _write_run(tmp_path, "v1", "openai:test-model", _one_suite_rows())
    failed_cell = [{
        "contender": "openai:test-model", "suite": "s2_spam",
        "status": "stopped", "abort_reason": "hard cap exceeded",
        "expected_rows": 2, "rows": 0,
    }]
    second = _write_run(
        tmp_path, "v2", "openai:test-model", {"s2_spam": []}, cells=failed_cell,
    )
    model = build_report_model([load_run(first), load_run(second)], tmp_path)
    cell = model.cells[("openai:test-model", "s2_spam")]
    assert cell.source_run == "v2"
    assert cell.n_rows == 0
    assert cell.accuracy is None  # no fallback to the v1 score
    assert cell.status == "stopped"
    assert cell.stop_reason == "hard cap exceeded"
    assert cell.completion_coverage == 0.0
    assert cell.partial is True


def test_same_run_supplied_twice_is_counted_once(tmp_path: Path):
    run_dir = _write_run(tmp_path, "v1", "openai:test-model", _one_suite_rows())
    run = load_run(run_dir)
    model = build_report_model([run, load_run(run_dir)], tmp_path)
    assert model.runs == [run]
    assert model.spend_usd == 0.01  # not 0.02
    cell = model.cells[("openai:test-model", "s2_spam")]
    assert cell.n_rows == 2  # rows not doubled


def test_duplicate_item_repeat_rejected(tmp_path: Path):
    rows = _one_suite_rows()
    rows["s2_spam"].append(_row("i1", "s2_spam", 1, 0.5, 0))  # same item+repeat
    run_dir = _write_run(tmp_path, "dupe", "openai:test-model", rows)
    with pytest.raises(MergeError, match="duplicate item-repeat"):
        build_report_model([load_run(run_dir)], tmp_path)


def test_select_cells_uses_manifest_and_rows():
    class FakeRun:
        def __init__(self, manifest, rows):
            self.manifest = manifest
            self.rows = rows
            self.run_id = "fake"
            self.dir = Path(".")

    earlier = FakeRun(
        {"cells": [{"contender": "c", "suite": "s"}]},
        [{"contender": "c", "suite": "s", "item_id": "x", "repeat": 0}],
    )
    later = FakeRun(
        {"cells": [{"contender": "c", "suite": "s"}]}, [],
    )
    selection = select_cells([earlier, later])  # type: ignore[arg-type]
    assert selection[("c", "s")].run is later
    assert selection[("c", "s")].rows == []


# ---- cost scope and coverage (findings 8, 12) --------------------------------


def test_cost_counts_every_recorded_attempt(tmp_path: Path):
    """With attempt logs, a retried decision bills both attempts."""
    rows = [_row("i1", "s2_spam", 0, 0.9, 0, retries=1)]
    rows[0]["contender"] = "openai:gpt-5.4-nano"
    attempts = {"s2_spam": [
        {"input_tokens": 500_000, "output_tokens": 0},   # failed attempt
        {"input_tokens": 500_000, "output_tokens": 100_000},  # final
    ]}
    run_dir = _write_run(
        tmp_path, "v2run", "openai:gpt-5.4-nano", {"s2_spam": rows},
        attempts=attempts,
    )
    model = build_report_model([load_run(run_dir)], tmp_path)
    cell = model.cells[("openai:gpt-5.4-nano", "s2_spam")]
    assert cell.cost_scope == "every recorded attempt of this cell"
    # Both attempts at gpt-5.4-nano list prices: 0.1 + 0.1 + 0.125.
    assert cell.cost_usd == 0.325
    assert cell.cost_per_1000 == 325.0
    assert cell.cost_incomplete_reason is None


def test_cost_marked_incomplete_when_usage_unknown(tmp_path: Path):
    rows = [_row("i1", "s2_spam", 0, 0.9, 0, input_tokens=None, output_tokens=None)]
    rows[0]["contender"] = "openai:gpt-5.4-nano"
    run_dir = _write_run(tmp_path, "unk", "openai:gpt-5.4-nano", {"s2_spam": rows})
    model = build_report_model([load_run(run_dir)], tmp_path)
    cell = model.cells[("openai:gpt-5.4-nano", "s2_spam")]
    assert cell.cost_incomplete_reason is not None
    assert "usage" in cell.cost_incomplete_reason
    assert cell.cost_usd == 0.0  # unknown is never a measured zero...


def test_v1_retried_cost_is_marked_lower_bound(tmp_path: Path):
    """Rows without attempt logs cannot see the discarded attempt's usage."""
    rows = [_row("i1", "s2_spam", 0, 0.9, 0, retries=1)]
    rows[0]["contender"] = "openai:gpt-5.4-nano"
    run_dir = _write_run(tmp_path, "v1run", "openai:gpt-5.4-nano", {"s2_spam": rows})
    model = build_report_model([load_run(run_dir)], tmp_path)
    cell = model.cells[("openai:gpt-5.4-nano", "s2_spam")]
    assert "lower bound" in cell.cost_incomplete_reason


def test_baseline_cost_is_measured_zero(tmp_path: Path):
    rows = [_row("i1", "s2_spam", 0, 0.9, 0, input_tokens=0, output_tokens=0)]
    rows[0]["contender"] = "baseline:majority"
    run_dir = _write_run(tmp_path, "bases", "baseline:majority", {"s2_spam": rows})
    model = build_report_model([load_run(run_dir)], tmp_path)
    cell = model.cells[("baseline:majority", "s2_spam")]
    assert cell.cost_usd == 0.0
    assert cell.cost_per_1000 == 0.0
    assert cell.cost_scope == "no billable calls"
    assert cell.cost_incomplete_reason is None


def test_metric_tables_render_cost_and_conf_range_not_dash(tmp_path: Path):
    """Finding 8: every configured metric key resolves (no silent '-')."""
    validate_table_specs()
    priced = _one_suite_rows()
    for row in priced["s2_spam"]:
        row["contender"] = "openai:gpt-5.4-nano"
    run_dir = _write_run(tmp_path, "v1", "openai:gpt-5.4-nano", priced)
    out = render_report(run_dir, tmp_path / "results")
    md = (out / "v1.md").read_text(encoding="utf-8")
    assert "$0.03" in md  # cost per 1000 renders, not "-": 110 tokens x1k
    # The coverage table is present with its denominators.
    assert "## Coverage" in md
    assert "expected" in md and "source run" in md
    json_summary = json.loads((out / "v1.cells.json").read_text(encoding="utf-8"))
    keys = {key for cell in json_summary["cells"] for key in cell}
    assert "cost_per_1000_usd" in keys
    assert "completion_coverage" in keys
    assert "latency_scope" in keys


def test_partial_cells_starred_and_failed_cells_listed(tmp_path: Path):
    rows = {"s2_spam": [_row("i1", "s2_spam", 0, 0.9, 0)]}
    cells = [{
        "contender": "openai:test-model", "suite": "s2_spam",
        "status": "stopped", "abort_reason": "hard cap exceeded",
        "expected_rows": 2, "rows": 1,
    }]
    run_dir = _write_run(tmp_path, "part", "openai:test-model", rows, cells=cells)
    out = render_report(run_dir, tmp_path / "results2")
    md = (out / "part.md").read_text(encoding="utf-8")
    assert "100.0*" in md  # accuracy starred: partial coverage
    assert "50.0" in md  # completion coverage 1 of 2
    assert "hard cap exceeded" in md  # stop reason visible


# ---- rendering and archive ---------------------------------------------------


def test_render_report_end_to_end(tmp_path: Path):
    suites = {
        "s2_spam": _one_suite_rows()["s2_spam"],
        "s3_cardinality": [_row("n1", "s3_cardinality", 0, 0.9, 0)],
        "s4_order": [_row("p1", "s4_order", 0, 0.8, 0)],
    }
    run_dir = _write_run(tmp_path, "vtest", "openai:test-model", suites)

    out = render_report(run_dir, tmp_path / "results")
    md = (out / "vtest.md").read_text(encoding="utf-8")
    html_out = (out / "vtest.html").read_text(encoding="utf-8")
    assert "openai:test-model" in md
    assert "test deviation" in md
    assert "<table>" in html_out and "<svg" in html_out
    assert (out / "cardinality.svg").exists()
    assert (out / "reliability-openai__test-model.svg").exists()
    # Macro-F1 says n/a on the suites where positions are not classes.
    assert "n/a" in md

    with tarfile.open(out / "vtest-raw.tar.gz") as tar:
        s2 = tar.extractfile("vtest/raw/openai__test-model.s2_spam.jsonl")
        assert s2 is not None
        scrubbed = json.loads(s2.read().decode("utf-8"))
        assert "secret sms text" not in json.dumps(scrubbed)
        assert scrubbed["raw"]["response"]["content"] == "[redacted: s2 license]"


def test_render_report_includes_correction_note(tmp_path: Path):
    run_dir = _write_run(tmp_path, "vcorr", "openai:test-model", _one_suite_rows())
    note = tmp_path / "note.md"
    note.write_text(
        "# Corrections in v2\n\n## What changed\n\nS4 majority prior corrected.\n"
    )
    out = render_report(run_dir, tmp_path / "results3", correction_note_path=note)
    md = (out / "vcorr.md").read_text(encoding="utf-8")
    html_out = (out / "vcorr.html").read_text(encoding="utf-8")
    assert "S4 majority prior corrected." in md
    assert "S4 majority prior corrected." in html_out
    # The inlined note sits under an H2, so its own headings move one
    # level down; the report keeps exactly one H1.
    assert "## Corrections in v2" in md
    assert "### What changed" in md
    assert "\n# Corrections in v2" not in md


def test_reliability_and_cardinality_svg_shape():
    rows = [_row("a", "s1_intent77", 0, 0.95, 0), _row("b", "s1_intent77", 1, 0.95, 1)]
    bins = reliability_bins(rows)
    assert sum(b["n"] for b in bins) == 2
    svg = reliability_svg("x:y", bins)
    assert svg.startswith("<svg") and "x:y" in svg
    # y-axis labels make bar and dot heights readable.
    assert ">0.50<" in svg and ">1.00<" in svg
    curve = cardinality_svg({"c": {2: {"accuracy": 0.5, "p50_ms": 10.0},
                                   8: {"accuracy": 0.6, "p50_ms": 20.0}}})
    assert "polyline" in curve


def test_cardinality_svg_legend_ticks_and_cutoff():
    """The figure names its lines, thins crowded ticks, marks the cutoff."""
    full = (2, 8, 32, 64, 128, 192, 254, 255, 256, 384, 512)
    series = {
        "a:full": {n: {"accuracy": 1.0, "p50_ms": 10.0} for n in full},
        # A rejected N keeps its entry with None values: rows exist, but
        # no valid decision came back (jev's 400 Too many choices).
        "b:capped": {
            **{n: {"accuracy": 1.0, "p50_ms": 20.0} for n in full if n <= 255},
            **{n: {"accuracy": None, "p50_ms": None} for n in full if n > 255},
        },
    }
    svg = cardinality_svg(series)
    # Legend: every line is named; a truncated one says where it ends.
    assert ">a:full<" in svg
    assert "b:capped (ends at N=255)" in svg
    # Dashed rule at the first N the truncated contender does not answer.
    assert svg.count("stroke-dasharray='3 3'") == 2  # one per panel
    # Crowded ticks: 255 (last answered N) keeps its label; the labels it
    # collides with are dropped in both panels.
    assert svg.count(">255<") == 2
    assert ">254<" not in svg and ">256<" not in svg


def test_single_suite_metric_tables_list_only_measured_suites(tmp_path: Path):
    """S4-only and S5-only tables drop the all-blank suite columns."""
    suites = {"s4_order": [_row("i1", "s4_order", 0, 0.9, 0)]}
    run_dir = _write_run(tmp_path, "vs4", "openai:test-model", suites)
    model = build_report_model([load_run(run_dir)], tmp_path)
    tables = report._metric_tables(model)
    flip = next(t for t in tables if t[0].startswith("S4 flip rate"))
    assert flip[1] == ["contender", "S4 order stability"]
    assert all(len(row) == 2 for row in flip[2])
    # No S5 cell exists, so the S5 tables carry no column and are dropped.
    assert not any(t[0].startswith("S5") for t in tables)
    # Suite-wide tables still list every suite of the report.
    acc = next(t for t in tables if t[0].startswith("Accuracy"))
    assert acc[1] == ["contender", "S4 order stability"]


def test_build_report_model_merges_extra_runs(tmp_path: Path):
    suites = {"s2_spam": [_row("i1", "s2_spam", 0, 0.9, 0)]}
    primary = _write_run(tmp_path, "v1", "openai:test-model", suites)
    extra = _write_run(tmp_path, "v1-jev", "typesafe:jev", suites)
    model = build_report_model([load_run(primary), load_run(extra)], tmp_path)
    assert {c.contender for c in model.cells.values()} == {
        "openai:test-model", "typesafe:jev",
    }


def test_archive_scrubs_only_licensed_suite(tmp_path: Path):
    good = tmp_path / "runs" / "r" / "raw" / "c.s1_intent77.jsonl"
    good.parent.mkdir(parents=True)
    payload = {"raw": {"response": {"content": "fine to publish"}}}
    good.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    out = archive_runs([tmp_path / "runs" / "r"], tmp_path / "a.tar.gz")
    with tarfile.open(out) as tar:
        member = tar.extractfile("r/raw/c.s1_intent77.jsonl")
        assert member is not None
        assert "fine to publish" in member.read().decode("utf-8")
