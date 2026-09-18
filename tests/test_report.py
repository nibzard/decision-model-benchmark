"""Report aggregation and rendering tests with synthetic run data."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

from dmb.report import (
    aggregate_cell,
    archive_runs,
    build_report_model,
    cardinality_svg,
    load_run,
    reliability_bins,
    reliability_svg,
    render_report,
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
        "retries": 0,
        "latency_ms": latency_ms,
        "input_tokens": 100,
        "output_tokens": 10,
        "gold_index": gold,
    }


def test_aggregate_cell_basic_counts():
    rows = [
        _row("a", "s1_intent77", 0, 0.9, 0),
        _row("b", "s1_intent77", 1, 0.8, 0),  # wrong
        _row("c", "s1_intent77", None, None, 1, ok=False, malformed=True),
        _row("d", "s1_intent77", None, None, 1, ok=False),
    ]
    items = {
        f"s1-{k}": DecisionItem(
            item_id=f"s1-{k}", suite="s", state="x", options=["a", "b"],
            gold_index=index % 2,
        )
        for index, k in enumerate("abcd")
    }
    cell = aggregate_cell("openai:test-model", "s1_intent77", rows, items)
    assert cell.n_rows == 4
    assert cell.n_malformed == 1
    assert cell.n_failed == 1
    assert cell.accuracy == 0.5  # 1 of 2 valid rows
    assert cell.ece > 0.0
    assert cell.brier > 0.0
    assert cell.latencies["p50_ms"] == 100.0


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


def _write_run(root: Path, run_id: str, contender: str, suites: dict[str, list[dict]]):
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
                "item_id": rows[0]["item_id"],
                "attempts": [{"detail": "secret sms text"}],
                "raw": {"response": {"content": "secret sms text"}},
            }) + "\n",
            encoding="utf-8",
        )
    all_rows = [dict(r, contender=contender) for rows in suites.values() for r in rows]
    (run_dir / "results.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in all_rows), encoding="utf-8"
    )
    (run_dir / "manifest.json").write_text(
        json.dumps({
            "run_id": run_id,
            "item_files": item_files,
            "deviations": ["test deviation"],
            "notes": ["note"],
            "skipped": [],
            "spend_usd": 0.01,
        }),
        encoding="utf-8",
    )
    return run_dir


def test_render_report_end_to_end(tmp_path: Path):
    suites = {
        "s2_spam": [_row("i1", "s2_spam", 0, 0.9, 0), _row("i2", "s2_spam", 1, 0.4, 1)],
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

    with tarfile.open(out / "vtest-raw.tar.gz") as tar:
        s2 = tar.extractfile("vtest/raw/openai__test-model.s2_spam.jsonl")
        assert s2 is not None
        scrubbed = json.loads(s2.read().decode("utf-8"))
        assert "secret sms text" not in json.dumps(scrubbed)
        assert scrubbed["raw"]["response"]["content"] == "[redacted: s2 license]"


def test_reliability_and_cardinality_svg_shape():
    rows = [_row("a", "s1_intent77", 0, 0.95, 0), _row("b", "s1_intent77", 1, 0.95, 1)]
    bins = reliability_bins(rows)
    assert sum(b["n"] for b in bins) == 2
    svg = reliability_svg("x:y", bins)
    assert svg.startswith("<svg") and "x:y" in svg
    curve = cardinality_svg({"c": {2: {"accuracy": 0.5, "p50_ms": 10.0},
                                   8: {"accuracy": 0.6, "p50_ms": 20.0}}})
    assert "polyline" in curve


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
