# DMB — decision-model benchmark

An independent, reproducible measurement of typed decisions: a decision
model (jev "System One"), constrained LLMs, and deterministic baselines
all answer the same question — pick one of N options and state a
calibrated confidence. One protocol, five suites, published raw logs.

Read `SPEC.md` for the full design. This file covers how to run it.

## Requirements

- Python 3.12 and `uv`
- API keys in the environment: `OPENAI_API_KEY`, `ANTHROPIC_AUTH_TOKEN`
  (with `ANTHROPIC_BASE_URL` when you use a gateway), `ZAI_API_KEY`,
  `DEEPSEEK_API_KEY`, `CEREBRAS_API_KEY`, and `TYPESAFE_API_KEY` for the
  jev run. Missing keys skip the contender and record the skip.

## Commands

```bash
uv sync                       # install httpx, pydantic, numpy
uv run dmb build              # build and hash all suite item files
uv run dmb run --run-id smoke --smoke   # 50 items per suite, 1 repeat
uv run dmb run --run-id v1              # full grid, 3 repeats, no jev
uv run dmb run --run-id v1.1 --only-jev # jev-only follow-up, same items
uv run dmb report runs/v1 --out results --extra runs/v1.1
uv run pytest                 # metric, adapter, runner, report tests
```

## Run IDs and exit codes

A run ID is one directory name: no slashes, no `.`, and not empty. The
runner reserves `runs/<id>` exclusively at start. If the directory
exists, the run refuses to start with exit code 2 — nothing is resumed,
merged, or overwritten. Use a new run ID and let the report merge cells
(see the next section).

`dmb run` exits 0 for a complete run. Exit 2 is a usage error: an
invalid or taken run ID, or no contenders available. Exit 3 is an
execution failure, 4 a budget stop, 5 an authentication stop, and 6 a
deadline stop. A stopped or failed run keeps its partial results, its
attempt logs, and a terminal manifest status.

## Merging runs in a report

`dmb report` verifies every supplied run before combining anything:
item-file hashes must match each manifest, conflicting hashes for one
suite are rejected, and rows are validated against the frozen items.
Later runs replace earlier cells whole — a later failed cell replaces an
earlier success instead of falling back to it. Every coverage row names
its source run. Runs whose protocol versions differ are rejected unless
you pass `--allow-protocol-mix`; the report then records the mix
visibly.

The corrected v2 report was produced with:

```bash
uv run dmb run --run-id v2-majority-fix --suites s1_intent77,s4_order \
  --contenders baseline:majority --repeats 3 --no-negotiate
uv run dmb report runs/v1 --extra runs/v1.1 --extra runs/v2-majority-fix \
  --allow-protocol-mix --correction-note results/v2/CORRECTIONS.md \
  --name v2 --out results/v2
```

`results/v2/CORRECTIONS.md` maps every corrected number to the defect
that produced the old one. The v1 and v1.1 reports stay untouched.

## What each command writes

- `data/suites/*.jsonl` — frozen items plus `hashes.json`; byte-identical
  on rebuild (pinned seed).
- `runs/<id>/manifest.json` — protocol, item-file hashes, price-table
  hash, library versions, spend, per-cell status, deviations, skips.
- `runs/<id>/prices.snapshot.json` — the exact price table the run was
  billed against; reports reprice from it.
- `runs/<id>/results.jsonl` — one row per item per repeat.
- `runs/<id>/raw/*.jsonl` — full attempt logs with provider responses,
  plus one attempt record per request in `*.attempts.jsonl`.
- `results/<name>.md`, `results/<name>.html` — generated report; never
  hand-edited.
- `results/<name>-raw.tar.gz` — raw archive; SMS spam state text is
  scrubbed (that license does not cover republication).

## Honesty rules

1. No vendor sponsorship. Costs are paid, listed, and dated.
2. The protocol is frozen before the full run. Deviations are recorded
   in the manifest, not edited silently.
3. Negative results ship. A contender that loses stays in the table.
4. Raw logs publish with the report. Anyone can recompute every number.
5. Unknown usage is never reported as a measured zero. Cost cells mark
   what their number covers and what is missing.

jev stayed out of the first published release (v1) and landed in v1.1.
The v2 report corrects the defects listed in `results/v2/CORRECTIONS.md`.
