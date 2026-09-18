"""DMB command line: build suites, run grids, render reports.

    uv run dmb build
    uv run dmb run --run-id smoke --smoke
    uv run dmb run --run-id v1
    uv run dmb run --run-id v1-jev --only-jev
    uv run dmb report runs/v1 --out results/v1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contenders import build_contenders, build_majority_table
from .runner import RunSpec, run_grid
from .suites.build import SUITE_ORDER
from .suites.items import load_items


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def cmd_build(_args: argparse.Namespace) -> int:
    from .suites.build import build_all

    build_all(_repo_root())
    return 0


def _majority_table(suite_keys: list[str]) -> dict[frozenset[str], tuple[str, float]]:
    suite_items = {}
    for key in suite_keys:
        path = _repo_root() / "data" / "suites" / f"{key}.jsonl"
        if path.exists():
            suite_items[key] = load_items(path)
    return build_majority_table(suite_items)


def cmd_run(args: argparse.Namespace) -> int:
    if args.suites:
        suites = [key.strip() for key in args.suites.split(",")]
    else:
        suites = list(SUITE_ORDER)
    include_jev = args.include_jev or args.only_jev
    contenders, skipped = build_contenders(
        include_jev=include_jev,
        majority_table=_majority_table(suites),
        negotiate=not args.no_negotiate,
    )
    if args.only_jev:
        contenders = [c for c in contenders if c.provider == "typesafe"]
    if not contenders:
        print("no contenders available; check env keys", file=sys.stderr)
        return 2
    spec = RunSpec(
        run_id=args.run_id,
        suites=suites,
        contenders=contenders,
        repeats=args.repeats,
        item_limit=50 if args.smoke else args.item_limit,
        hard_cap_usd=args.hard_cap,
        soft_cap_usd=args.soft_cap,
        notes=[
            "skipped contenders: " + json.dumps(skipped) if skipped else "no skips",
            f"negotiate={not args.no_negotiate}",
        ],
    )
    run_dir = run_grid(spec, _repo_root())
    manifest = json.loads((run_dir / "manifest.json").read_text())
    print(f"run {spec.run_id}: {len(manifest['cells'])} cells, spend ${manifest['spend_usd']}")
    for cell in manifest["cells"]:
        print(
            f"  {cell['contender']:28s} {cell['suite']:16s}"
            f" {cell['status']:8s} {cell['wall_s']:8.1f}s"
        )
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from .report import render_report

    run_dir = Path(args.run_dir)
    out_dir = Path(args.out) if args.out else _repo_root() / "results" / run_dir.name
    render_report(run_dir, out_dir, extra_run_dirs=[Path(p) for p in (args.extra or [])])
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
    run.add_argument("--hard-cap", type=float, default=60.0)
    run.add_argument("--soft-cap", type=float, default=40.0)
    run.add_argument("--no-negotiate", action="store_true")
    run.set_defaults(func=cmd_run)

    report = sub.add_parser("report", help="render MD + HTML from a run directory")
    report.add_argument("run_dir")
    report.add_argument("--out", default=None)
    report.add_argument(
        "--extra",
        action="append",
        help="extra run directory merged in (for example the jev-only v1.1 run)",
    )
    report.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
