"""Plan 002 scored run: Decisions API on the frozen preview sample.

Runs ``openai-decisions:gpt-6-luna`` against the exact frozen 256-item
sample in ``.benchmark-studies/decisions-preview-2026-09-30``, matching
the control run's one repeat and one worker per provider. The cap is
separate from the controls' shared $2: $5 total authorized on 2026-10-07,
at the verified list price of $0.10 per 1M input tokens (input-only
billing). Renders a comparison report against the existing controls.
"""

from __future__ import annotations

import json
from pathlib import Path

from dmb.contenders.openai_decisions import OpenAIDecisionsContender
from dmb.contenders.render import prompt_fingerprint
from dmb.report import render_report
from dmb.runner import RunSpec, exit_code_for, run_grid, validate_run_id
from dmb.suites.build import SUITE_ORDER
from dmb.suites.items import sha256_file

ROOT = Path(__file__).resolve().parents[1]
STUDY_ID = "decisions-preview-2026-09-30"
CONTROL_RUN = "decisions-preview-controls"
RUN_ID = "decisions-preview-decisions"
REPORT_ID = "decisions-preview-comparison-2026-10-07"
HARD_CAP_USD = 5.0
SOFT_CAP_USD = 4.0


def main() -> int:
    study = ROOT / ".benchmark-studies" / STUDY_ID
    validate_run_id(RUN_ID)
    controls = study / "runs" / CONTROL_RUN
    prior = json.loads((controls / "manifest.json").read_text(encoding="utf-8"))
    if prior["status"] == "running":
        raise ValueError("control run must be terminal before the scored run")
    expected = prior["item_files"]
    actual = {
        suite: sha256_file(study / "data" / "suites" / f"{suite}.jsonl") for suite in expected
    }
    if actual != expected:
        raise ValueError("frozen sample files no longer match the control run manifest")
    if prior["protocol"]["prompt_sha256"] != prompt_fingerprint():
        raise ValueError("prompt fingerprint drifted from the control run")

    contender = OpenAIDecisionsContender()
    spec = RunSpec(
        run_id=RUN_ID,
        suites=list(SUITE_ORDER),
        contenders=[contender],
        repeats=1,
        concurrency=1,
        hard_cap_usd=HARD_CAP_USD,
        soft_cap_usd=SOFT_CAP_USD,
        negotiate=True,
        notes=[
            "Decisions API scored run on the same frozen 256-item sample; "
            "one repeat and one worker, matching the controls",
            "separate cap: $5 authorized 2026-10-07; verified list price "
            "$0.10 per 1M input tokens, input-only billing",
            "provider-defined confidence; the negotiation probe verifies the "
            "usage object shape and the accounting stop before scored requests",
        ],
    )
    try:
        run_dir, manifest = run_grid(spec, study)
    finally:
        contender.close()
    out = render_report(
        controls,
        ROOT / "results" / REPORT_ID,
        extra_run_dirs=[run_dir],
        name=REPORT_ID,
    )
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "decisions_known_spend_usd": manifest["spend_usd"],
                "cost_complete": manifest.get("cost_complete"),
                "cells": [
                    {k: cell.get(k) for k in ("contender", "suite", "status")}
                    for cell in manifest["cells"]
                ],
                "report": str(out),
            },
            indent=2,
        ),
        flush=True,
    )
    return exit_code_for(manifest)


if __name__ == "__main__":
    raise SystemExit(main())
