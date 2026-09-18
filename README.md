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

## What each command writes

- `data/suites/*.jsonl` — frozen items plus `hashes.json`; byte-identical
  on rebuild (pinned seed).
- `runs/<id>/manifest.json` — protocol, item-file hashes, price-table
  hash, library versions, spend, per-cell status, deviations, skips.
- `runs/<id>/results.jsonl` — one row per item per repeat.
- `runs/<id>/raw/*.jsonl` — full attempt logs with provider responses.
- `results/<id>.md`, `results/<id>.html` — generated report; never
  hand-edited.
- `results/<id>-raw.tar.gz` — raw archive; SMS spam state text is
  scrubbed (that license does not cover republication).

## Honesty rules

1. No vendor sponsorship. Costs are paid, listed, and dated.
2. The protocol is frozen before the full run. Deviations are recorded
   in the manifest, not edited silently.
3. Negative results ship. A contender that loses stays in the table.
4. Raw logs publish with the report. Anyone can recompute every number.

jev stays out of the first published release (v1) and lands in v1.1.
