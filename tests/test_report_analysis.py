"""Report integrity, privacy, and interpretation regressions."""

import hashlib
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from dmb import report
from dmb.metrics import cluster_mean_interval, paired_cluster_difference, score_risk_coverage
from dmb.prices import snapshot_hash, snapshot_payload
from dmb.report import MergeError, aggregate_cell, archive_runs, build_report_model, load_run
from dmb.suites.items import DecisionItem
from test_report import _one_suite_rows, _row, _write_run


def test_top_level_protocol_version_is_normalized(tmp_path):
    a = load_run(_write_run(tmp_path, "a", "openai:gpt-5.4-nano", _one_suite_rows()))
    b = load_run(_write_run(tmp_path, "b", "openai:gpt-5.4-nano", _one_suite_rows()))
    b.manifest["protocol_version"] = "v3"
    with pytest.raises(MergeError, match="protocol_version"):
        report.verify_runs([a, b], tmp_path)
    assert report._protocol_of(b.manifest)["protocol_version"] == "v3"
    b.manifest["protocol"]["protocol_version"] = "v2"
    with pytest.raises(MergeError, match="conflicting"):
        report.verify_runs([a, b], tmp_path, allow_protocol_mix=True)


def test_snapshot_verifies_exact_legacy_payload_before_filling_defaults(tmp_path):
    run = load_run(_write_run(tmp_path, "a", "openai:gpt-5.4-nano", _one_suite_rows()))
    payload = [
        {k: v for k, v in p.items() if not k.startswith("cache_")} for p in snapshot_payload()
    ]
    expected = hashlib.sha256(json.dumps(payload, sort_keys=True, indent=2).encode()).hexdigest()
    run.manifest["price_table_sha256"] = expected
    path = run.dir / "prices.snapshot.json"
    path.write_text(json.dumps(payload))
    assert report._prices_for_run(run)["openai:gpt-5.4-nano"].cache_read_per_mtok is None
    payload[0]["input_per_mtok"] += 0.01
    path.write_text(json.dumps(payload))
    with pytest.raises(MergeError, match="snapshot hash"):
        report._prices_for_run(run)


@pytest.mark.parametrize(
    "field,value",
    [
        ("choice_index", True),
        ("choice_index", 0.0),
        ("choice_index", "0"),
        ("confidence", float("nan")),
        ("confidence", float("inf")),
        ("confidence", 1.2),
        ("confidence", None),
        ("correct", False),
        ("correct", 1),
        ("repeat", -1),
        ("repeat", 1),
        ("repeat", True),
        ("latency_ms", -1),
        ("latency_ms", float("nan")),
        ("input_tokens", -1),
        ("ok", "true"),
        ("malformed", True),
    ],
)
def test_invalid_scoring_rows_are_rejected(tmp_path, field, value):
    run = load_run(_write_run(tmp_path, "a", "openai:gpt-5.4-nano", _one_suite_rows()))
    run.rows[0][field] = value
    with pytest.raises(MergeError):
        report.verify_runs([run], tmp_path)


def test_failed_numeric_correct_flag_is_rejected(tmp_path):
    run = load_run(_write_run(tmp_path, "a", "openai:gpt-5.4-nano", _one_suite_rows()))
    run.rows[0].update(ok=False, correct=0)
    with pytest.raises(MergeError, match="correct flag"):
        report.verify_runs([run], tmp_path)


def test_v3_configuration_drift_is_rejected(tmp_path):
    name = "openai:gpt-5.4-nano"
    run = load_run(_write_run(tmp_path, "a", name, _one_suite_rows()))
    config = {"model": "gpt-5.4-nano", "temperature": 0}
    fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    run.manifest["contenders"] = [
        {"name": name, "effective_configuration": config, "configuration_fingerprint": fingerprint}
    ]
    for row in run.rows:
        row["configuration_fingerprint"] = fingerprint
    report.verify_runs([run], tmp_path)
    run.rows[0]["configuration_fingerprint"] = "0" * 64
    with pytest.raises(MergeError, match="configuration drift"):
        report.verify_runs([run], tmp_path)


def test_s4_separates_repeat_changes_and_reports_missing_blocks():
    items = {
        f"b-p{p}": DecisionItem(
            item_id=f"b-p{p}",
            suite="s4_order",
            state="x",
            options=["a", "b"],
            gold_index=0,
            base_item_id="b",
            meta={"perm": [0, 1]},
        )
        for p in range(3)
    }
    # Every permutation produces the same answer on a given repeat.
    # Variability is entirely repeat-wise, so across-order blocks never flip.
    rows = [
        dict(_row(item, "s4_order", repeat % 2, 0.8, 0), repeat=repeat)
        for item in items
        for repeat in range(3)
    ]
    cell = aggregate_cell("openai:test-model", "s4_order", rows, items)
    assert cell.flip_rate == 1.0
    assert cell.order_stability["within_order_flip_rate"] == 1.0
    assert cell.order_stability["across_order_flip_rate"] == 0.0
    assert cell.order_stability["observations_per_block"] == 3
    rows[0].update(ok=False, correct=False, choice_index=None, confidence=None)
    cell = aggregate_cell("openai:test-model", "s4_order", rows, items)
    assert cell.order_stability["within_order_missing_blocks"] == 1
    assert cell.order_stability["across_order_missing_blocks"] == 1
    assert cell.order_stability["within_order_complete_blocks"] == 2


def test_s4_capped_run_uses_eligible_bases_for_block_size(tmp_path):
    items = {
        f"{base}-p{p}": DecisionItem(
            item_id=f"{base}-p{p}",
            suite="s4_order",
            state="x",
            options=["a", "b"],
            gold_index=0,
            base_item_id=base,
            meta={"perm": [0, 1]},
        )
        for base in ("a", "b")
        for p in range(3)
    }
    rows = [
        dict(_row(item, "s4_order", 0, 0.8, 0), repeat=repeat)
        for item in list(items)[:4]
        for repeat in range(2)
    ]
    run = report.RunData("smoke", tmp_path, {"protocol": {"item_limit": 4, "repeats": 2}}, rows)
    manifest_cell = {"expected_rows": 8}
    selected = report.SelectedCell(run, manifest_cell, rows)
    cell = aggregate_cell(
        "openai:test-model", "s4_order", rows, items, manifest_cell=manifest_cell, selected=selected
    )
    assert cell.order_stability["observations_per_block"] == 2
    assert cell.order_stability["total_base_items"] == 2
    assert cell.order_stability["eligible_base_items"] == 1
    assert cell.order_stability["excluded_base_items"] == 1
    assert cell.order_stability["within_order_complete_blocks"] == 3
    assert cell.order_stability["across_order_complete_blocks"] == 2
    empty = report._order_stability(rows[:1], dict(list(items.items())[:1]), expected_repeats=2)
    assert empty["observations_per_block"] == 0
    assert empty["within_order_flip_rate"] is None


def test_s5_no_good_diagnostics_are_separate_and_native_score_is_labeled():
    rows = [
        _row("s5-ng-001", "s5_confidence", 0, 0.8, -1),
        _row("s5-ud-001", "s5_confidence", 0, 0.9, 0),
    ]
    items = {
        r["item_id"]: DecisionItem(
            item_id=r["item_id"],
            suite="s5_confidence",
            state="x",
            options=["a", "b"],
            gold_index=r["gold_index"],
        )
        for r in rows
    }
    cell = aggregate_cell("typesafe:jev", "s5_confidence", rows, items)
    assert cell.ece == pytest.approx(0.1)  # retained legacy underdetermined field
    assert cell.uncertainty_subsets["no_good_option"]["ece"] == pytest.approx(0.8)
    assert cell.uncertainty_subsets["no_good_option"]["brier"] == pytest.approx(0.64)
    assert "underdetermined only" in cell.calibration_subset
    assert "not established as probability" in cell.confidence_semantics
    assert cell.risk_coverage[0]["risk"] == 0.5


def test_reliability_bins_match_ece_edge_convention():
    bins = report.reliability_bins([_row("x", "s1_intent77", 0, 0.9, 0)])
    assert bins[8]["n"] == 1
    assert bins[9]["n"] == 0


def test_cluster_intervals_keep_repeats_together_and_pair_common_items():
    first = {"a": [1, 1, 1], "b": [0, 0, 0]}
    interval = cluster_mean_interval(first)
    assert interval["n_clusters"] == 2
    assert interval["estimate"] == 0.5
    assert interval == cluster_mean_interval({"a": [1], "b": [0]})
    pair = paired_cluster_difference(first, {"a": [0], "b": [0], "c": [1]})
    assert pair["estimate"] == 0.5
    assert pair["n_clusters"] == 2 and pair["n_second_clusters"] == 3
    assert cluster_mean_interval({"a": [1]})["lower"] is None
    curve = score_risk_coverage([0.9, 0.5], [True, False], 4)
    assert curve[0]["coverage"] == 0.5 and curve[0]["risk"] == 0.5
    assert curve[-1]["accepted"] == 0 and curve[-1]["risk"] is None


def test_archive_projects_shared_s2_and_probe_records_without_arbitrary_strings(tmp_path):
    run = tmp_path / "run"
    raw = run / "raw"
    raw.mkdir(parents=True)
    secret = "SENSITIVE MESSAGE TEXT MUST NEVER SHIP"
    deep = {secret: secret}
    for _ in range(25):
        deep = {"nested": deep}
    payload = dict(
        _row("s2-0001", "s2_spam", 0, 0.9, 0),
        contender="openai:gpt-5.4-nano",
        error=secret,
        usage_details={
            "prompt_tokens": 100,
            "cache_read_input_tokens": 80,
            "input_tokens_include_cache": True,
            "unknown": secret,
        },
        raw={"response": {"reasoning_content": secret, "id": secret, "x": deep}},
        arbitrary=secret,
    )
    (run / "results.jsonl").write_text(json.dumps(payload) + "\n")
    (raw / "openai__gpt-5.4-nano.s2_spam.jsonl").write_text(json.dumps(payload) + "\n")
    probe = dict(payload, suite=None, item_id=None, contender=secret, phase="negotiation")
    (raw / "negotiation.attempts.jsonl").write_text(json.dumps(probe) + "\n")
    manifest = {
        "run_id": "run",
        "notes": [secret],
        "arbitrary": deep,
        "status": "stopped_accounting",
        "protocol_version": "v3",
        "unknown_usage_attempts": {"openai:gpt-5.4-nano": 2},
    }
    (run / "manifest.json").write_text(json.dumps(manifest))
    archive = archive_runs([run], tmp_path / "out.tar.gz")
    second_archive = archive_runs([run], tmp_path / "different-name.tar.gz")
    assert archive.read_bytes() == second_archive.read_bytes()
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            assert secret not in tar.extractfile(member).read().decode()
        row = json.load(tar.extractfile("run/results.jsonl"))
        assert row["item_id"] == "s2-0001"
        assert row["usage_details"]["cache_read_input_tokens"] == 80
        assert row["confidence"] == 0.9 and row["input_tokens"] == 100
        assert "raw" not in row and "arbitrary" not in row
        m = json.load(tar.extractfile("run/manifest.json"))
        assert m["status"] == "stopped_accounting"
        assert m["unknown_usage_attempts"]["openai:gpt-5.4-nano"] == 2


def test_historical_cost_joins_raw_cache_usage_and_survives_redaction(tmp_path):
    name = "openai:gpt-5.4-nano"
    rows = [_row("s2-0001", "s2_spam", 0, 0.9, 0, input_tokens=1000, output_tokens=10)]
    run_dir = _write_run(tmp_path, "legacy", name, {"s2_spam": rows})
    # Restore a historical snapshot without a cache rate; never substitute current rates.
    payload = [
        {k: v for k, v in p.items() if not k.startswith("cache_")} for p in snapshot_payload()
    ]
    (run_dir / "prices.snapshot.json").write_text(json.dumps(payload))
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["price_table_sha256"] = snapshot_hash(payload)
    manifest_path.write_text(json.dumps(manifest))
    path = run_dir / "raw" / "openai__gpt-5.4-nano.s2_spam.jsonl"
    record = json.loads(path.read_text())
    record["raw"]["response"]["usage"]["prompt_tokens_details"]["cached_tokens"] = 800
    path.write_text(json.dumps(record) + "\n")
    cell = build_report_model([load_run(run_dir)], tmp_path).cells[(name, "s2_spam")]
    assert cell.cost_usd == pytest.approx((200 * 0.2 + 10 * 1.25) / 1_000_000)
    assert not cell.cost_complete and "cache read" in cell.cost_incomplete_reason
    archive = archive_runs([run_dir], tmp_path / "archive.tar.gz")
    extracted = tmp_path / "reimport"
    with tarfile.open(archive) as tar:
        tar.extractall(extracted, filter="data")
    regenerated = build_report_model([load_run(extracted / "legacy")], tmp_path)
    again = regenerated.cells[(name, "s2_spam")]
    assert again.cost_usd == cell.cost_usd
    assert again.cost_incomplete_reason == cell.cost_incomplete_reason


def test_archive_preserves_anthropic_exclusive_cache_semantics(tmp_path):
    raw = tmp_path / "run" / "raw"
    raw.mkdir(parents=True)
    record = {
        "item_id": "s2-0001",
        "repeat": 0,
        "suite": "s2-gate-spam",
        "raw": {
            "response": {
                "usage": {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 800}
            }
        },
    }
    path = raw / "anthropic__claude-haiku-4-5.s2_spam.jsonl"
    path.write_text(json.dumps(record) + "\n")
    archive = archive_runs([raw.parent], tmp_path / "archive.tar.gz")
    with tarfile.open(archive) as tar:
        saved = json.load(tar.extractfile(f"run/raw/{path.name}"))
    assert saved["usage_details"]["input_tokens_include_cache"] is False
    assert saved["suite"] == "s2-gate-spam"


def test_rendered_s2_report_never_publishes_free_text_diagnostics(tmp_path):
    name = "openai:gpt-5.4-nano"
    run = load_run(_write_run(tmp_path, "a", name, _one_suite_rows()))
    sentinel = "SENSITIVE PROVIDER ECHO OF SMS"
    run.manifest.update(
        notes=[sentinel], deviations=[sentinel], skipped=[{"contender": name, "reason": sentinel}]
    )
    run.manifest["cells"][0]["abort_reason"] = sentinel
    model = build_report_model([run], tmp_path)
    assert sentinel not in report.render_md(model)
    assert sentinel not in report.render_html(model, report._metric_tables(model))
    assert sentinel not in json.dumps(model.json_summary())


def test_report_artifacts_are_identical_across_fresh_hash_seeds(tmp_path):
    suites = {
        suite: [_row("i2", suite, 1, 0.85, 1), _row("i1", suite, 0, 0.75, 0)]
        for suite in ("s1_intent77", "s2_spam", "s3_cardinality", "s4_order")
    }
    _write_run(tmp_path, "source", "openai:gpt-5.4-nano", suites)
    script = (
        "from pathlib import Path; import sys; from dmb.report import render_report; "
        "render_report(Path(sys.argv[1])/'runs/source', Path(sys.argv[2]), name='stable')"
    )
    outputs = []
    for seed in ("1", "207"):
        out = tmp_path / f"output-{seed}"
        env = dict(
            os.environ,
            PYTHONHASHSEED=seed,
            PYTHONPATH=str(Path(report.__file__).resolve().parents[1]),
        )
        subprocess.run(
            [sys.executable, "-B", "-c", script, str(tmp_path), str(out)],
            env=env,
            check=True,
            capture_output=True,
        )
        outputs.append({path.name: path.read_bytes() for path in out.iterdir()})
    assert outputs[0] == outputs[1]


def test_reliability_confidence_sum_is_order_invariant():
    rows = [
        _row(str(i), "s1_intent77", 0, confidence, 0)
        for i, confidence in enumerate([0.71] * 101 + [0.8] * 99)
    ]
    assert report.reliability_bins(rows) == report.reliability_bins(list(reversed(rows)))
