"""Expanded datasets keep gold labels, split boundaries, and grouped denominators."""

import json

import pytest

from dmb import cli
from dmb.contenders.base import MockContender
from dmb.expanded_metrics import decision_units, task_metrics
from dmb.report import build_report_model, load_run, render_report
from dmb.runner import RunSpec, run_grid
from dmb.suites.expanded import (
    EXPANDED_SUITES,
    SOURCE_FILE,
    banking_items,
    clinc_items,
    nlupp_items,
    text_hash,
)
from dmb.suites.items import save_items
from dmb.thresholds import (
    apply_threshold,
    choose_threshold,
    evaluate_thresholds,
    fit_thresholds,
    write_result,
)


def routing_data():
    return {
        "train": [["seed", "a"], ["seed b", "b"]],
        "val": [["first validation", "a"], ["second validation", "b"]],
        "oos_val": [["outside validation", "oos"]],
        "test": [["first test", "a"], ["second test", "b"]],
        "oos_test": [["outside test", "oos"]],
    }


def grouped_data():
    ontology = {
        "intents": {
            "greet": {"description": "Is this a greeting?", "domain": ["general"]},
            "transfer": {"description": "Is this about a transfer?", "domain": ["banking"]},
            "booking": {"description": "Is this about a booking?", "domain": ["hotels"]},
        }
    }
    return nlupp_items(
        ontology,
        {
            ("banking", 16): [
                {"text": "separate validation", "intents": ["greet"]},
                {"text": " TRANSFER please ", "intents": ["transfer"]},
            ],
            ("banking", 18): [
                {"text": "transfer please", "intents": ["transfer"]},
                {"text": "hello", "intents": ["greet"]},
            ],
        },
    )


def gold_rows(items):
    return [
        {
            "item_id": i.item_id,
            "repeat": 0,
            "choice_index": i.gold_index,
            "gold_index": i.gold_index,
            "ok": True,
            "correct": True,
            "confidence": 0.9,
        }
        for i in items
    ]


def test_banking_preserves_official_test_and_selects_disjoint_balanced_validation():
    train = [{"text": f"{label}-{n}", "category": label} for label in ("a", "b") for n in range(14)]
    test = [{"text": " A-0 ", "category": "a"}, {"text": "test-b", "category": "b"}]
    splits = banking_items(train, test)
    assert [i.state for i in splits["test"]] == [r["text"] for r in test]
    assert len(splits["validation"]) == 20
    assert {i.gold_index for i in splits["validation"]} == {0, 1}
    assert text_hash("a-0") not in {i.meta["source_text_sha256"] for i in splits["validation"]}
    assert banking_items(train, test) == splits


def test_clinc_preserves_oos_gold_and_removes_validation_overlap():
    data = routing_data()
    data["val"].append([" FIRST  TEST ", "a"])
    splits = clinc_items(data)
    assert len(splits["validation"]) == 3
    assert splits["test"][-1].gold_index == 2
    assert splits["test"][-1].meta["out_of_scope"] is True
    assert all(len(i.options) == 3 for i in splits["test"])


def test_nlupp_keeps_domain_labels_together_and_does_not_expose_gold():
    splits = grouped_data()
    assert len(splits["validation"]) == 2  # overlapping message removes its entire label group
    assert len(splits["test"]) == 4
    assert {i.meta["intent"] for i in splits["test"]} == {"greet", "transfer"}
    assert len({i.base_item_id for i in splits["test"]}) == 2
    payload = json.loads(splits["test"][0].state)
    assert set(payload) == {"message", "question", "instruction"}
    assert splits["test"][0].meta["source_text_sha256"] == text_hash(payload["message"])


def test_group_metrics_include_missing_labels_and_do_not_hide_all_negative_predictions():
    items = grouped_data()["test"]
    by_id = {i.item_id: i for i in items}
    rows = gold_rows(items)
    assert task_metrics(rows, by_id)["complete_message_accuracy"] == 1.0
    missing = task_metrics(rows[:-1], by_id)
    assert missing["complete_message_accuracy"] == 0.5
    assert missing["complete_message_coverage"] == 0.5
    for row in rows:
        row.update(choice_index=0, correct=row["gold_index"] == 0)
    result = task_metrics(rows, by_id)
    assert result["success_all_requested"] == 0.5
    assert result["micro_intent_f1"] == 0
    assert result["complete_message_accuracy"] == 0
    with pytest.raises(ValueError, match="duplicate"):
        decision_units(rows + rows[:1], by_id)


def test_oos_recall_counts_missing_and_failed_positive_requests():
    items = clinc_items(routing_data())["test"]
    rows = gold_rows(items)[:-1]
    result = task_metrics(rows, {i.item_id: i for i in items})
    assert result["out_of_scope_recall"] == 0
    assert result["out_of_scope_precision"] is None
    assert result["in_scope_success"] == 1
    assert result["success_all_requested"] == pytest.approx(2 / 3)


def test_threshold_selection_respects_ties_minimum_and_abstention():
    units = [
        {"valid": True, "score": 0.9, "correct": True},
        {"valid": True, "score": 0.8, "correct": False},
        {"valid": True, "score": 0.8, "correct": True},
        {"valid": False, "score": None, "correct": False},
    ]
    assert choose_threshold(units, 0.0, 1)["threshold"] == 0.9
    assert choose_threshold(units, 0.0, 2)["threshold"] is None
    assert choose_threshold(units, 0.34, 2)["accepted"] == 3
    result = apply_threshold(units, 0.9)
    assert result["coverage"] == 0.25
    assert apply_threshold(units, None)["error_rate"] is None
    with pytest.raises(ValueError):
        choose_threshold(units, float("nan"), 1)


def make_run(root, name, suite, items, limit=None):
    save_items(items, root / f"data/suites/{suite}.jsonl")
    return run_grid(
        RunSpec(
            run_id=name,
            suites=[suite],
            contenders=[MockContender.always_zero(confidence=0.8)],
            repeats=1,
            negotiate=False,
            item_limit=limit,
        ),
        root,
    )[0]


def test_expanded_run_report_and_frozen_thresholds_roundtrip(tmp_path):
    splits = clinc_items(routing_data())
    validation = make_run(tmp_path, "validation", "s7_clinc150_validation", splits["validation"])
    test = make_run(tmp_path, "test", "s7_clinc150_test", splits["test"])
    frozen = fit_thresholds(validation, tmp_path, max_error=0.7, min_accepted=1)
    assert frozen["cells"][0]["validation"]["threshold"] == 0.8
    result = evaluate_thresholds(test, tmp_path, frozen)
    assert result["cells"][0]["coverage"] == 1.0
    assert result["cells"][0]["error_rate"] == pytest.approx(2 / 3)
    report = build_report_model([load_run(test)], tmp_path)
    assert report.suites == ["s7_clinc150_test"]
    cell = report.cells[("mock-zero", "s7_clinc150_test")]
    assert cell.expanded["out_of_scope_recall"] == 0
    out = tmp_path / "report"
    render_report(test, out)
    assert "S7 out-of-scope recall" in (out / "test.md").read_text()
    assert "CLINC150" in (out / "test.html").read_text()
    with pytest.raises(ValueError, match="validation"):
        fit_thresholds(test, tmp_path)
    frozen["cells"][0]["source_text_hashes"].append(splits["test"][0].meta["source_text_sha256"])
    with pytest.raises(ValueError, match="overlap"):
        evaluate_thresholds(test, tmp_path, frozen)


def test_thresholds_reject_configuration_drift_and_partial_message_groups(tmp_path):
    splits = grouped_data()
    validation = make_run(tmp_path, "val", "s8_nlupp_validation", splits["validation"])
    test = make_run(tmp_path, "test", "s8_nlupp_test", splits["test"])
    frozen = fit_thresholds(validation, tmp_path, max_error=0.9, min_accepted=1)
    assert frozen["cells"][0]["unit"] == "message"
    frozen["cells"][0]["signature"]["fingerprint"] = "changed"
    with pytest.raises(ValueError, match="configuration"):
        evaluate_thresholds(test, tmp_path, frozen)
    limited = make_run(tmp_path, "limited", "s8_nlupp_validation", splits["validation"], limit=1)
    with pytest.raises(ValueError, match="cuts an NLU"):
        fit_thresholds(limited, tmp_path, min_accepted=1)


def test_threshold_files_are_never_overwritten(tmp_path):
    path = tmp_path / "thresholds.json"
    write_result(path, {"threshold": 0.9})
    with pytest.raises(FileExistsError):
        write_result(path, {"threshold": 0.1})
    assert json.loads(path.read_text())["threshold"] == 0.9


def test_sources_are_pinned_and_cli_build_is_opt_in(monkeypatch, tmp_path):
    sources = json.loads(SOURCE_FILE.read_text())
    assert len(sources) == 14
    assert all(s["commit"] in s["url"] and len(s["sha256"]) == 64 for s in sources.values())
    import dmb.suites.expanded as expanded

    calls = []
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(expanded, "build_expanded", lambda root: calls.append(root))
    assert cli.main(["build", "--expanded"]) == 0
    assert calls == [tmp_path]
    assert len(EXPANDED_SUITES) == 6
