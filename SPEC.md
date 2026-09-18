# DMB — Decision-model benchmark

- **Status:** draft v0.1
- **Date:** 2026-09-18
- **Owner:** Nikola
- **Goal:** an independent, reproducible measurement of "System One" decision
  models versus constrained LLMs versus deterministic baselines.

## Why

TypeSafe AI shipped jev with vendor-run claims: 40x–200x faster, ~$0.042/MTok
input, free output, calibrated confidence, no schema violations, a 255-choice
cardinality cap. Every number comes from their own evals, and their post
admits the bias. No independent measurement exists. DMB is that measurement.

The question DMB answers, narrowly: **for a typed decision (pick one of N
options, return a calibrated confidence), what do you actually get from each
model class — at what latency, cost, and failure rate?**

## Non-goals

- No prose-quality or reasoning benchmarks. Generation is out of scope.
- No agent workflows, tool use, or multi-step loops. One call, one decision.
- No fine-tuning. Contenders run as served.
- No live dashboard. A static report per release is enough.

## Contenders

Three classes. Every contender receives the same serialized decision state
and returns `{choice_index: int, confidence: float}` against a schema fixed
in advance. The input format is identical for all — the comparison is the
model class, not the prompt.

### A. Decision model

| Contender | Status |
|---|---|
| jev (TypeSafe AI) | No key yet (waitlist). Adapter ships behind the same interface; column stays empty until access opens. |

### B. Constrained LLMs

All run with temperature 0, native structured output where the provider
offers it, confidence requested in the same schema.

| Contender | Key in env | Structured output |
|---|---|---|
| `openai:gpt-5.4-nano` | `OPENAI_API_KEY` | `json_schema` |
| `openai:gpt-5.4-mini` | `OPENAI_API_KEY` | `json_schema` |
| `anthropic:claude-haiku-4-5` | `ANTHROPIC_AUTH_TOKEN` | forced tool use |
| `anthropic:claude-sonnet-4-6` | `ANTHROPIC_AUTH_TOKEN` | forced tool use |
| `zai:glm-5.3-flash` | `ZAI_API_KEY` | JSON mode |
| `zai:glm-5.3` | `ZAI_API_KEY` | JSON mode |
| `deepseek:deepseek-chat` | `DEEPSEEK_API_KEY` | JSON mode |
| `cerebras:<open-model>` | `CEREBRAS_API_KEY` | JSON mode |

Cerebras matters on purpose: it is the "fast LLM" wing, so the latency story
is not only jev versus slow reasoning models.

### C. Deterministic baselines

| Contender | Purpose |
|---|---|
| `random` | Floor. |
| `majority` | Class prior. |
| `keyword` | Simple matching heuristic per suite. The "do you even need a model" control. |

## Suites

Five suites. Real data where a license permits; seeded synthetic where the
point is control. Target 300 items per suite (200 eval + 100 for order
stability reruns). Items are frozen at release; a release is not a release
until the item files are hashed into the run manifest.

### S1 `intent-77` — 77-way routing (real)

Banking77 intent classification. Input: customer utterance. Options: the 77
intent labels. Gold: dataset label. Measures mid-cardinality accuracy and
calibration on a real distribution.

### S2 `gate-spam` — binary gating (real)

UCI SMS spam. Input: message text. Options: `{spam, ham}`. The "LLM as
bouncer" call every product actually makes.

### S3 `cardinality` — planted-answer sweep (synthetic)

Generated decision items with a planted correct option among N distractors,
difficulty controlled by distractor similarity. N sweeps
`{2, 8, 32, 64, 128, 192, 254, 255, 256, 384, 512}`, 25 items per step. This
is the suite that tests the 255 cliff and the two-stage slowdown claim, plus
how each LLM's accuracy decays as the option list grows.

### S4 `order-stability` — same items, shuffled options

S1 items rerun with permuted option order (3 permutations). Gold labels map
through the permutation. Measures flip rate (same item, different option
position → different choice) and calibration drift. This is the position-bias
probe; nobody has published it for this model class.

### S5 `confidence-honesty` — forced uncertainty (synthetic)

Items with no good option, plus items where the correct option is present but
underdetermined by the state text. Gold: `confidence ≤ 0.5` behavior. Measures
whether a contender admits ignorance or bluffs. Calibration metrics here tell
you whose confidence you can threshold.

## Metrics

Per contender, per suite:

| Metric | Definition |
|---|---|
| Accuracy / macro-F1 | Against gold (S3 maps through permutations). |
| Malformed rate | Replies that fail schema or bounds after one retry. |
| ECE | 10-bin expected calibration error over confidence. |
| Brier | On the reported confidence for correctness. |
| p50 / p95 / p99 latency | End-to-end client wall time, 3 repeats. |
| Cost per 1,000 decisions | From provider usage fields, list prices pinned with date. |
| Flip rate | S4 only: choice changes under option permutation. |
| Cardinality curve | S3 accuracy and latency versus N. |

## Protocol

1. Temperature 0 (or provider minimum). No caching. No provider-side
   response caching claims without the usage proof in the raw log.
2. Concurrency capped at 4 per provider; on rate limit, exponential backoff,
   one retry, then the item counts as failed.
3. One retry on malformed LLM reply (recorded); a second failure counts as
   malformed, not wrong.
4. Prices live in one table with the date they were checked. Cost is
   computed from usage, never from estimates.
5. Every run writes `runs/<id>/manifest.json`: contender versions, item-file
   hashes, price table hash, library versions, wall time, total spend.
6. Raw responses are kept in the run directory and published with the
   report. No cherry-picking; the report is generated, not written.

## Report

- `results/vN.md` and a static `results/vN.html`: one table per metric,
   one cardinality plot, one reliability diagram per contender.
- Raw run directories attached as an archive.
- Published on nibzard.com with the raw data. The headline number is never a
  single speedup; it is the per-suite table.

## Repository layout

```
decision-model-benchmark/
  SPEC.md              this file
  src/dmb/
    contenders/        base.py, llm_openai.py, llm_anthropic.py, llm_zai.py,
                       llm_deepseek.py, llm_cerebras.py, jev.py, baselines.py
    suites/            s1_intent77.py, s2_spam.py, s3_cardinality.py,
                       s4_order.py, s5_confidence.py, build.py
    metrics.py         accuracy, ECE, Brier, flip rate
    runner.py          executes a grid, writes run manifests
    report.py          renders MD + HTML from run directories
  data/                gitignored; built by suites/build.py
  runs/                gitignored per-run output
  results/             published tables and plots
  tests/               metric unit tests, mock-contender runner test
```

Python 3.12, `uv`. Dependencies: `httpx`, `pydantic`, `numpy`. Nothing else.

## Budget and kill criteria

- Soft cap: $40 total provider spend per release. Hard cap: $60. The runner
  tracks spend live and aborts at the hard cap.
- A contender that cannot finish a suite within 2,000 s wall time is recorded
  as DNF for that suite, with partial results kept.
- If banking77 or SMS spam licensing cannot be confirmed clean for
  republication of item text, the suite ships with locally-kept items and
  publishable aggregate numbers only.
- jev stays out of the first published release. The release goes out without
  it; jev lands as v1.1 when the key arrives. No waiting on a waitlist.

## Task list

### Phase 0 — skeleton (half day)

- [ ] T0.1 `git init`, `uv init`, dependency set, `src/dmb` package, ruff
      config. Done when `uv run pytest` passes with a placeholder test.
- [ ] T0.2 `contenders/base.py`: `Contender` protocol —
      `decide(state: str, options: list[str]) -> Decision` where `Decision`
      carries `choice_index`, `confidence`, `latency_ms`, `input_tokens`,
      `output_tokens`, `raw`. Plus `MockContender` with scripted replies.
- [ ] T0.3 `metrics.py` with unit tests: accuracy, macro-F1, 10-bin ECE,
      Brier, flip rate. Gold: hand-computed cases.
- [ ] T0.4 Runner smoke: MockContender × synthetic 20-item suite →
      `runs/<id>/manifest.json` written, hashes correct.

### Phase 1 — suites (one day)

- [ ] T1.1 `s2_spam.py`: UCI SMS spam loader → frozen items file
      (200 eval + 100 spare). License note in the module docstring.
- [ ] T1.2 `s1_intent77.py`: banking77 loader → frozen items, 77 options
      serialized once.
- [ ] T1.3 `s3_cardinality.py`: generator, seed fixed, planted answers,
      distractor similarity controlled; N sweep as specified. Unit test:
      every item has exactly one planted answer; regeneration is
      byte-identical given the seed.
- [ ] T1.4 `s4_order.py`: permutation generator over S1 items; gold mapping
      test.
- [ ] T1.5 `s5_confidence.py`: no-good-option and underdetermined items,
      seeded, hand-reviewed once.
- [ ] T1.6 `suites/build.py`: builds and hashes all five item files in one
      command.

### Phase 2 — contenders (one day)

- [ ] T2.1 `llm_openai.py` with `json_schema` mode; retries and malformed
      counting per protocol. Smoke: 5 live items.
- [ ] T2.2 `llm_anthropic.py` with forced tool use. Smoke: 5 live items.
- [ ] T2.3 `llm_zai.py`, `llm_deepseek.py`, `llm_cerebras.py` (JSON mode,
      shared parsing path). Smoke: 5 live items each.
- [ ] T2.4 `baselines.py`: random, majority, keyword.
- [ ] T2.5 `jev.py`: full adapter against the documented schema, gated on
      `TYPESAFE_API_KEY`; skipped with a recorded reason when absent.
- [ ] T2.6 Decision-state renderer: one function all contenders share, so
      the input text cannot drift between adapters. Pinned by a test.

### Phase 3 — runner and cost (half day)

- [ ] T3.1 Grid runner: contenders × suites, concurrency 4, backoff, DNF
      tracking, live spend tracker with hard-cap abort.
- [ ] T3.2 Usage extraction per provider (usage fields differ); token
      accounting test with recorded response fixtures.
- [ ] T3.3 3-repeat latency handling: repeats recorded, report uses median.

### Phase 4 — smoke then full run (one day)

- [ ] T4.1 Smoke run: every contender on 50 items per suite. Fix everything
      that breaks. Gate: zero unexplained failures.
- [ ] T4.2 Full run v1 (no jev): all contenders, all suites, one manifest.
- [ ] T4.3 Sanity review: baselines beat random; majority matches class
      prior; any contender below random triggers an adapter bug hunt before
      results are trusted.

### Phase 5 — report and publish (half day)

- [ ] T5.1 `report.py`: tables, cardinality plot, reliability diagrams from
      the run directory alone.
- [ ] T5.2 `results/v1.md` + `v1.html`, raw archive.
- [ ] T5.3 Blog post on nibzard.com: method, tables, caveats, raw-data link.
      State that jev was not yet available and v1.1 will add it.

### Phase 6 — jev (when the key arrives)

- [ ] T6.1 Live smoke of `jev.py` on 5 items per suite.
- [ ] T6.2 Full run v1.1 with jev, same items, same protocol.
- [ ] T6.3 v1.1 report + blog update. The cardinality suite gets the
      headline: measured 255 behavior versus the claim.

## Honesty rules

1. No vendor sponsorship. Costs are paid, listed, and dated.
2. The protocol above is frozen before the full run. Deviations are recorded
   in the manifest, not edited silently.
3. Negative results ship. A contender that loses ships in the table.
4. Raw logs publish with the report. Anyone can recompute every number.
