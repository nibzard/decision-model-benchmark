"""DMB command line: build suites, run grids, render reports.

    uv run dmb build
    uv run dmb run --run-id smoke --smoke
    uv run dmb run --run-id v1
    uv run dmb run --run-id v1-jev --only-jev
    uv run dmb report runs/v1 --out results/v1

Run exit codes: 0 complete; 2 usage error (bad run ID, no contenders);
3 execution failure; 4 budget/accounting stop; 5 authentication stop;
6 deadline stop; 130 interrupted. A stopped or failed run keeps partial results and a terminal
manifest status.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contenders import build_contenders
from .contenders.baselines import PRIOR_SOURCES, build_majority_table
from .runner import (
    EXIT_EXECUTION,
    EXIT_INTERRUPTED,
    EXIT_USAGE,
    RunConfigurationError,
    RunIdError,
    RunSpec,
    exit_code_for,
    run_grid,
    validate_spec,
)
from .suites.build import SUITE_ORDER
from .suites.items import load_items, sha256_file


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def cmd_build(_args: argparse.Namespace) -> int:
    from .suites.build import build_all

    build_all(_repo_root())
    return 0


def _majority_setup(suites: list[str]) -> tuple[dict, dict[str, dict]]:
    """Load prior sources and build the majority table with provenance.

    The S1 source loads even when only S4 is selected, so the order
    experiment always runs against the same frozen S1 prior.
    """
    needed_sources = sorted({PRIOR_SOURCES[s] for s in suites if s in PRIOR_SOURCES})
    suite_items: dict[str, list] = {}
    source_hashes: dict[str, str] = {}
    for key in needed_sources:
        path = _repo_root() / "data" / "suites" / f"{key}.jsonl"
        if path.exists():
            suite_items[key] = load_items(path)
            source_hashes[key] = sha256_file(path)
    for key in suites:  # target suites need their items for option sets
        if key not in suite_items:
            path = _repo_root() / "data" / "suites" / f"{key}.jsonl"
            if path.exists():
                suite_items[key] = load_items(path)
    return build_majority_table(suite_items, source_hashes=source_hashes)


def cmd_run(args: argparse.Namespace) -> int:
    suites = (
        [key.strip() for key in args.suites.split(",")]
        if args.suites is not None
        else list(SUITE_ORDER)
    )
    spec = RunSpec(
        run_id=args.run_id,
        suites=suites,
        contenders=[],
        repeats=1 if args.smoke else args.repeats,
        item_limit=50 if args.smoke else args.item_limit,
        hard_cap_usd=args.hard_cap,
        soft_cap_usd=args.soft_cap,
        negotiate=not args.no_negotiate,
    )
    try:
        validate_spec(spec, require_contenders=False)
        unknown = set(suites) - set(SUITE_ORDER)
        if unknown:
            raise RunConfigurationError(f"unknown suites: {', '.join(sorted(unknown))}")
        if args.only_jev and args.contenders is not None:
            raise RunConfigurationError("--only-jev cannot be combined with --contenders")
        wanted = (
            [word.strip() for word in args.contenders.split(",")]
            if args.contenders is not None
            else None
        )
        if wanted is not None and any(not word for word in wanted):
            raise RunConfigurationError("contender filters must not be empty")
        if args.only_jev:
            wanted = ["typesafe:jev"]
        if (_repo_root() / "runs" / spec.run_id).exists():
            raise RunIdError(f"run directory runs/{spec.run_id} already exists; use a new run ID")
        for suite in suites:
            if not (_repo_root() / "data" / "suites" / f"{suite}.jsonl").is_file():
                raise RunConfigurationError(f"suite {suite} is missing; run dmb build first")
        majority_table, spec.majority_provenance = _majority_setup(suites)
    except (RunConfigurationError, RunIdError, OSError, ValueError) as exc:
        print(f"cannot start run: {exc}", file=sys.stderr)
        return EXIT_USAGE

    # Construction performs no decision probes. The runner reserves the
    # directory, checks pricing, and persists its initial manifest first.
    spec.contenders, skipped, _unused_usage = build_contenders(
        include_jev=args.include_jev or args.only_jev,
        majority_table=majority_table,
        negotiate=False,
        wanted=wanted,
    )
    if not spec.contenders:
        print("no contenders available; check filters and env keys", file=sys.stderr)
        return EXIT_USAGE
    spec.deviations = sorted({c.deviation for c in spec.contenders if c.deviation})
    spec.notes = [
        note
        for note in (
            "skipped contenders: " + json.dumps(skipped) if skipped else "no skips",
            f"negotiate={spec.negotiate}",
            f"contender filter: {wanted}" if wanted else "",
            "smoke run: 50 items per suite, 1 repeat" if args.smoke else "",
        )
        if note
    ]
    try:
        _run_dir, manifest = run_grid(spec, _repo_root())
    except (RunIdError, RunConfigurationError) as exc:
        print(f"cannot start run: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as exc:
        print(f"run persistence failed: {exc}", file=sys.stderr)
        return EXIT_EXECUTION
    finally:
        for contender in spec.contenders:
            contender.close()
    qualifier = "known subtotal" if not manifest.get("cost_complete", True) else "measured"
    print(
        f"run {spec.run_id}: {len(manifest['cells'])} cells, "
        f"{qualifier} spend ${manifest['spend_usd']}"
    )
    for cell in manifest["cells"]:
        print(
            f"  {cell['contender']:28s} {cell['suite']:16s}"
            f" {cell['status']:8s} {cell['wall_s']:8.1f}s"
        )
    code = exit_code_for(manifest)
    if code:
        print(f"run status: {manifest['status']} (exit {code})", file=sys.stderr)
    return code


def cmd_report(args: argparse.Namespace) -> int:
    from .report import render_report

    run_dir = Path(args.run_dir)
    out_dir = Path(args.out) if args.out else _repo_root() / "results" / run_dir.name
    render_report(
        run_dir,
        out_dir,
        extra_run_dirs=[Path(p) for p in (args.extra or [])],
        name=args.name,
        allow_protocol_mix=args.allow_protocol_mix,
        correction_note_path=(Path(args.correction_note) if args.correction_note else None),
    )
    print(f"report written to {out_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dmb")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="build and hash all suite item files")
    build.set_defaults(func=cmd_build)

    run = sub.add_parser("run", help="run a contender x suite grid")
    run.add_argument("--run-id", required=True)
    run.add_argument("--suites", help="comma-separated suite keys; default all")
    run.add_argument("--smoke", action="store_true", help="50 items per suite, 1 repeat")
    run.add_argument("--repeats", type=int, default=3)
    run.add_argument("--item-limit", type=int, default=None)
    run.add_argument("--include-jev", action="store_true")
    run.add_argument("--only-jev", action="store_true")
    run.add_argument(
        "--contenders",
        help="comma-separated substrings; keep only matching contenders",
    )
    run.add_argument("--hard-cap", type=float, default=60.0)
    run.add_argument("--soft-cap", type=float, default=40.0)
    run.add_argument("--no-negotiate", action="store_true")
    run.set_defaults(func=cmd_run)

    report = sub.add_parser("report", help="render MD + HTML from a run directory")
    report.add_argument("run_dir")
    report.add_argument("--out", default=None)
    report.add_argument(
        "--name",
        default=None,
        help="report version label used for file names (default: run directory name)",
    )
    report.add_argument(
        "--extra",
        action="append",
        help="extra run directory merged in; later runs replace earlier cells",
    )
    report.add_argument(
        "--allow-protocol-mix",
        action="store_true",
        help="allow merging runs whose protocol versions differ; the report "
        "records the mix and its policy visibly",
    )
    report.add_argument(
        "--correction-note",
        default=None,
        help="path to a Markdown note included verbatim in the report",
    )
    report.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
