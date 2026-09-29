"""Fit score thresholds on validation runs; apply frozen thresholds to test runs."""

from __future__ import annotations

import json
import math
from pathlib import Path

from .expanded_metrics import decision_units
from .report import _protocol_of, load_run, select_cells, verify_runs
from .suites.expanded import FAMILIES


def choose_threshold(units: list[dict], max_error: float, min_accepted: int) -> dict:
    """Maximize observed validation coverage with tied scores kept together.

    This is empirical threshold selection, not a statistical error guarantee.
    """
    if not math.isfinite(max_error) or not 0 <= max_error < 1 or min_accepted < 1:
        raise ValueError("max_error must be in [0, 1); min_accepted must be positive")
    valid = sorted((u for u in units if u["valid"]), key=lambda u: -u["score"])
    best = {"threshold": None, "accepted": 0, "error_rate": None}
    errors = 0
    for index, unit in enumerate(valid):
        errors += not unit["correct"]
        if index + 1 < len(valid) and valid[index + 1]["score"] == unit["score"]:
            continue
        count = index + 1
        if count >= min_accepted and errors / count <= max_error:
            best = {"threshold": unit["score"], "accepted": count, "error_rate": errors / count}
    return {
        **best,
        "expected": len(units),
        "coverage": best["accepted"] / len(units) if units else 0,
    }


def apply_threshold(units: list[dict], threshold: float | None) -> dict:
    accepted = [
        u for u in units if u["valid"] and threshold is not None and u["score"] >= threshold
    ]
    return {
        "expected": len(units),
        "accepted": len(accepted),
        "coverage": len(accepted) / len(units) if units else 0.0,
        "error_rate": sum(not u["correct"] for u in accepted) / len(accepted) if accepted else None,
        "correct_all_requested": sum(u["correct"] for u in units) / len(units) if units else None,
    }


def _cells(run_dir: Path, split: str, root: Path):
    run = load_run(run_dir)
    items = verify_runs([run], root)
    protocol = _protocol_of(run.manifest)
    if protocol.get("repeats") != 1:
        raise ValueError("threshold analysis requires one repeat per input")
    configuration = {c["name"]: c for c in run.manifest.get("contenders", [])}
    cells = []
    for (contender, suite), selected in select_cells([run]).items():
        if suite not in {f"{family}_{split}" for family in FAMILIES}:
            raise ValueError(f"threshold {split} run contains unsupported suite {suite}")
        expected = items[suite]
        limit = protocol.get("item_limit")
        if limit is not None:
            expected = dict(list(expected.items())[:limit])
        if not expected or any(i.meta.get("split") != split for i in expected.values()):
            raise ValueError("suite split metadata does not match its name")
        if any(not i.meta.get("source_text_sha256") for i in expected.values()):
            raise ValueError("source text hashes are required for split verification")
        units = decision_units(selected.rows, expected)
        if split == "validation" and len(selected.rows) != len(expected):
            raise ValueError("threshold fitting requires a completed validation cell")
        if suite.startswith("s8_"):
            group_counts = {}
            for item in expected.values():
                group_counts[item.base_item_id] = group_counts.get(item.base_item_id, 0) + 1
            if any(group_counts[i.base_item_id] != i.meta["group_size"] for i in expected.values()):
                raise ValueError("item limit cuts an NLU++ message; use complete groups")
        config = configuration.get(contender)
        if config is None:
            raise ValueError(f"missing configuration for {contender}")
        signature = {
            "configuration": config.get("effective_configuration"),
            "fingerprint": config.get("configuration_fingerprint"),
            "protocol_version": protocol.get("protocol_version"),
            "prompt_sha256": protocol.get("prompt_sha256"),
        }
        cells.append(
            {
                "contender": contender,
                "family": suite.rsplit("_", 1)[0],
                "suite": suite,
                "units": units,
                "signature": signature,
                "source_text_hashes": sorted(
                    {i.meta["source_text_sha256"] for i in expected.values()}
                ),
                "item_sha256": run.manifest["item_files"][suite],
                "unit": "message" if suite.startswith("s8_") else "decision",
            }
        )
    if not cells:
        raise ValueError("run contains no cells to analyze")
    return run, cells


def fit_thresholds(
    run_dir: Path, root: Path, max_error: float = 0.05, min_accepted: int = 100
) -> dict:
    run, cells = _cells(run_dir, "validation", root)
    return {
        "format": "dmb-thresholds-v1",
        "validation_run": run.run_id,
        "max_validation_error": max_error,
        "min_accepted": min_accepted,
        "interpretation": "Empirical selection; test error may exceed the validation target. "
        "Jev confidence is a native score, not a correctness probability. "
        "NLU++ thresholds use the minimum intent score and require a wholly correct message.",
        "cells": [
            {
                **{k: v for k, v in cell.items() if k != "units"},
                "validation": choose_threshold(cell["units"], max_error, min_accepted),
            }
            for cell in cells
        ],
    }


def evaluate_thresholds(run_dir: Path, root: Path, frozen: dict) -> dict:
    if frozen.get("format") != "dmb-thresholds-v1":
        raise ValueError("unsupported threshold file")
    run, cells = _cells(run_dir, "test", root)
    parameters = {(c["contender"], c["family"]): c for c in frozen["cells"]}
    if len(parameters) != len(frozen["cells"]):
        raise ValueError("duplicate threshold cells")
    output = []
    for cell in cells:
        parameter = parameters.get((cell["contender"], cell["family"]))
        if parameter is None or parameter["signature"] != cell["signature"]:
            raise ValueError("missing threshold or model/protocol configuration changed")
        if set(parameter["source_text_hashes"]) & set(cell["source_text_hashes"]):
            raise ValueError("validation and test inputs overlap")
        threshold = parameter["validation"]["threshold"]
        if threshold is not None and (
            type(threshold) not in (float, int)
            or not math.isfinite(threshold)
            or not 0 <= threshold <= 1
        ):
            raise ValueError("invalid frozen threshold")
        output.append(
            {
                "contender": cell["contender"],
                "suite": cell["suite"],
                "unit": cell["unit"],
                "threshold": threshold,
                "item_sha256": cell["item_sha256"],
                **apply_threshold(cell["units"], threshold),
            }
        )
    return {"validation_run": frozen["validation_run"], "test_run": run.run_id, "cells": output}


def write_result(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
