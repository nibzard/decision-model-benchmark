"""Balanced low-cost pilot; isolated frozen items, one repeat, one shared budget.

No calls with --prepare-only. Normal execution makes paid provider requests.
The sample is not mergeable with historical full-suite runs and does not
estimate repeat stability. Reports and sanitized archives are written locally.
"""

from __future__ import annotations

import argparse
import json
import random
import time
from collections import defaultdict
from pathlib import Path

from dmb.contenders import build_contenders
from dmb.contenders.jev import JevContender
from dmb.contenders.llm_gemini import GeminiContender
from dmb.report import render_report
from dmb.runner import RunSpec, exit_code_for, run_grid, validate_run_id
from dmb.suites.build import SUITE_ORDER
from dmb.suites.items import DMB_SEED, load_items, save_items, sha256_file

ROOT = Path(__file__).resolve().parents[1]
RECENT = [
    "openai:gpt-6-luna",
    "openai:gpt-6-sol",
    "openai:gpt-5.6-terra",
    "gemini:gemini-3.5-flash-lite",
    "gemini:gemini-3.8-flash",
    "gemini:gemini-3.1-pro-preview",
]
CHEAP = ["openai:gpt-6-luna", "gemini:gemini-3.5-flash-lite"]


def add_jev(study_id, total_cap):
    """Add the decision-model control on the existing frozen pilot items."""
    validate_run_id(study_id)
    study = ROOT / ".benchmark-studies" / study_id
    original = study / "runs" / study_id
    prior = json.loads((original / "manifest.json").read_text())
    if total_cap > prior["protocol"]["hard_cap_usd"]:
        raise ValueError("extension cannot raise the original budget")
    runs = [original] + sorted(p for p in (study / "runs").iterdir() if p != original)
    manifests = [json.loads((p / "manifest.json").read_text()) for p in runs]
    if any(m["status"] == "running" for m in manifests):
        raise ValueError("all prior runs must be terminal before extension")
    spent = sum(m["spend_usd"] for m in manifests)
    remaining = total_cap - spent
    if remaining <= 0:
        raise ValueError("prior runs exhausted the shared budget")
    c = JevContender(model="jev-1.13.0")
    spec = RunSpec(
        run_id=study_id + "-jev",
        suites=list(SUITE_ORDER),
        contenders=[c],
        repeats=1,
        hard_cap_usd=remaining,
        soft_cap_usd=remaining,
        negotiate=True,
        notes=["same frozen pilot items; Jev 1.13 pinned; provider-defined confidence"],
    )
    try:
        added, manifest = run_grid(spec, study)
    finally:
        c.close()
    out = ROOT / "results" / study_id
    note = out / "STUDY.md"
    note.write_text(
        note.read_text() + "\nJev was added on the exact same frozen sample with "
        "one repeat and the shared uncertainty instructions, using pinned "
        "jev-1.13.0. Its native confidence is provider-defined, so probability "
        "calibration comparisons remain qualified. Input price $0.042/MTok, "
        "output free, reconfirmed at https://docs.typesafe.ai/models on "
        f"2026-09-26; invoices not reconciled. Jev known spend including probes: "
        f"${manifest['spend_usd']:.8f}; all runs combined: "
        f"${spent + manifest['spend_usd']:.8f}.\n"
    )
    render_report(
        original, out, extra_run_dirs=runs[1:] + [added], name=study_id, correction_note_path=note
    )
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "jev_known_spend_usd": manifest["spend_usd"],
                "combined_known_spend_usd": spent + manifest["spend_usd"],
                "report": str(out),
            },
            indent=2,
        ),
        flush=True,
    )
    return exit_code_for(manifest)


class PacedPro(GeminiContender):
    """Account-specific 25 RPM limit, with headroom; use one runner worker."""

    def __init__(self):
        super().__init__("gemini-3.1-pro-preview")
        self.next_request = 0.0
        self.notes.append("requests paced at least 3 seconds apart; latency includes pacing")

    def _post(self, body):
        time.sleep(max(0.0, self.next_request - time.monotonic()))
        self.next_request = time.monotonic() + 3.0
        return super()._post(body)


def repair_pro(study_id, total_cap):
    """Replace Pro cells whole; retain and account for the original run."""
    validate_run_id(study_id)
    study = ROOT / ".benchmark-studies" / study_id
    original = study / "runs" / study_id
    prior = json.loads((original / "manifest.json").read_text())
    if prior["status"] == "running":
        raise ValueError("original run must be terminal before repair")
    if total_cap > prior["protocol"]["hard_cap_usd"]:
        raise ValueError("repair cannot raise the original budget")
    spent = prior["spend_usd"]
    remaining = total_cap - spent
    if remaining <= 0:
        raise ValueError("original run exhausted the shared budget")
    c = PacedPro()
    spec = RunSpec(
        run_id=study_id + "-pro-paced",
        suites=list(SUITE_ORDER),
        contenders=[c],
        repeats=1,
        concurrency=1,
        hard_cap_usd=remaining,
        soft_cap_usd=remaining,
        negotiate=True,
        deviations=[c.deviation],
        notes=c.notes,
    )
    try:
        repaired, manifest = run_grid(spec, study)
    finally:
        c.close()
    out = ROOT / "results" / study_id
    note = out / "STUDY.md"
    completed = sum(
        json.loads(line)["ok"] for line in (repaired / "results.jsonl").read_text().splitlines()
    )
    note.write_text(
        note.read_text() + "\nGemini Pro originally exceeded this account's 25 RPM "
        "quota. Its five cells are replaced by a rerun attempt with one worker and "
        "requests at least 3 seconds apart. Pro latency includes pacing and is "
        "not a like-for-like speed comparison. Both runs are retained in the "
        f"archive. Rerun status: {manifest['status']}; valid rerun decisions: {completed}/256. "
        "Incomplete Pro coverage must not be interpreted as model quality. "
        f"Original known spend: ${spent:.8f}; rerun known spend: "
        f"${manifest['spend_usd']:.8f}; combined: "
        f"${spent + manifest['spend_usd']:.8f}. Usage-less 429 responses leave "
        "an accounting qualification; these figures are known list-price costs, "
        "not an invoice reconciliation.\n"
    )
    render_report(
        original, out, extra_run_dirs=[repaired], name=study_id, correction_note_path=note
    )
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "combined_known_spend_usd": spent + manifest["spend_usd"],
                "report": str(out),
            },
            indent=2,
        ),
        flush=True,
    )
    return exit_code_for(manifest)


def sample_items(suite, items):
    """Use a fixed seed; cover every intent/N, paired permutations, both S5 halves."""
    rng = random.Random(DMB_SEED)
    groups = defaultdict(list)
    for item in items:
        if suite in {"s1_intent77", "s2_spam"}:
            key = item.options[item.gold_index]
        elif suite == "s3_cardinality":
            key = len(item.options)
        elif suite == "s4_order":
            key = item.base_item_id
        else:
            key = "no_good" if item.gold_index < 0 else "underdetermined"
        groups[key].append(item)
    for group in groups.values():
        rng.shuffle(group)
    if suite == "s1_intent77":
        chosen = [groups[key][0] for key in sorted(groups)]
    elif suite == "s2_spam":
        # Preserve approximately the original ham/spam prior, not artificial 50/50.
        n_spam = round(50 * len(groups["spam"]) / len(items))
        chosen = groups["ham"][: 50 - n_spam] + groups["spam"][:n_spam]
    elif suite == "s3_cardinality":
        chosen = [item for key in sorted(groups) for item in groups[key][:4]]
    elif suite == "s4_order":
        bases = sorted(groups)
        rng.shuffle(bases)
        chosen = [item for base in bases[:15] for item in groups[base]]
    else:
        chosen = groups["no_good"][:20] + groups["underdetermined"][:20]
    return sorted(chosen, key=lambda item: item.item_id)


def prepare(root: Path, study_id: str) -> tuple[Path, dict]:
    validate_run_id(study_id)
    study = root / ".benchmark-studies" / study_id
    study.mkdir(parents=True, exist_ok=False)
    sources, hashes, counts, item_ids = {}, {}, {}, {}
    for suite in SUITE_ORDER:
        source = root / "data/suites" / f"{suite}.jsonl"
        chosen = sample_items(suite, load_items(source))
        sources[suite] = sha256_file(source)
        hashes[suite] = save_items(chosen, study / "data/suites" / f"{suite}.jsonl")
        counts[suite] = len(chosen)
        item_ids[suite] = [item.item_id for item in chosen]
    provenance = {
        "study_id": study_id,
        "seed": DMB_SEED,
        "source_item_hashes": sources,
        "sample_item_hashes": hashes,
        "counts": counts,
        "selected_item_ids": item_ids,
        "sampling": "one per Banking77 intent; SMS prior preserved; four per cardinality; "
        "15 S4 bases/all three orders; 20 items from each S5 half",
        "limitations": "small stratified pilot, one repeat; no repeat-stability inference; "
        "S1 macro-F1 has one reference item per intent; S4 has only 15 bases; "
        "not mergeable with historical full-suite runs",
    }
    (study / "study.json").write_text(json.dumps(provenance, indent=2) + "\n")
    return study, provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study-id", required=True)
    parser.add_argument("--profile", choices=["six", "cheap"], default="six")
    parser.add_argument("--hard-cap", type=float, default=10.0)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--repair-pro", action="store_true")
    parser.add_argument("--add-jev", action="store_true")
    args = parser.parse_args()
    if not 0 < args.hard_cap <= 10 or args.hard_cap != args.hard_cap:
        parser.error("pilot cap must be positive and at most $10")
    if args.add_jev:
        if args.prepare_only or args.repair_pro:
            parser.error("add-jev cannot be combined with prepare-only or repair-pro")
        return add_jev(args.study_id, args.hard_cap)
    if args.repair_pro:
        if args.prepare_only:
            parser.error("repair-pro and prepare-only are mutually exclusive")
        return repair_pro(args.study_id, args.hard_cap)
    study, provenance = prepare(ROOT, args.study_id)
    wanted = RECENT if args.profile == "six" else CHEAP
    print(
        json.dumps(
            {
                "profile": args.profile,
                "counts": provenance["counts"],
                "planned_decisions": sum(provenance["counts"].values()) * len(wanted),
                "hard_cap": args.hard_cap,
            },
            indent=2,
        ),
        flush=True,
    )
    if args.prepare_only:
        return 0
    contenders, skipped, _ = build_contenders(wanted=wanted, negotiate=False)
    if skipped or {c.name for c in contenders} != set(wanted):
        for c in contenders:
            c.close()
        raise ValueError("selected pilot credentials/models unavailable; no probes started")
    spec = RunSpec(
        run_id=args.study_id,
        suites=list(SUITE_ORDER),
        contenders=contenders,
        repeats=1,
        hard_cap_usd=args.hard_cap,
        soft_cap_usd=args.hard_cap,
        negotiate=True,
        deviations=sorted(c.deviation for c in contenders if c.deviation),
        notes=[provenance["sampling"], provenance["limitations"]],
    )
    try:
        run, manifest = run_grid(spec, study)
    finally:
        for c in contenders:
            c.close()
    out = ROOT / "results" / args.study_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "study.json").write_text(json.dumps(provenance, indent=2) + "\n")
    note = out / "STUDY.md"
    note.write_text(
        "# Recent-model pilot\n\n"
        + provenance["sampling"]
        + ".\n\n"
        + provenance["limitations"]
        + ".\n\n"
        "Minimum supported reasoning settings are pinned per endpoint. Costs use dated standard "
        "paid-tier list rates; Gemini account tier/invoices are not reconciled. "
        "No tools, grounding, or explicit cache storage are requested. "
        "Astra is excluded. All started attempts and probes count toward the shared cap.\n"
    )
    render_report(run, out, name=args.study_id, correction_note_path=note)
    print(
        json.dumps(
            {"status": manifest["status"], "spend_usd": manifest["spend_usd"], "report": str(out)},
            indent=2,
        ),
        flush=True,
    )
    return exit_code_for(manifest)


if __name__ == "__main__":
    raise SystemExit(main())
