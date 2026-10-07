# Plan 002: Benchmark the Decisions API

## Status

- Goal: Add the Decisions API and compare it with Luna and jev.
- Started: September 30, 2026.
- Status: Scored run complete. Expansion to the full suites remains.

## Verified evidence

The [DevDay recap](https://openai.com/index/devday-2026-recap/) announces a
limited preview. It describes context from text or images and questions with
finite predefined answers. It does not define a request or response schema.

On September 30, the official documentation search returns no Decisions API
guide. The documentation server lists 225 endpoint paths. None contains
`decisions`. The [API reference](https://developers.openai.com/api/reference/overview)
does not establish its endpoint, pricing, model identifier, or confidence fields.
These search results do not prove that private preview documentation is absent.

A second search also checks the exact phrase `"Decisions API"` in the official
documentation search. It returns zero hits. The fetched API changelog and pricing
page contain no Decisions entry. The public index at
`https://developers.openai.com/api/docs/llms.txt` contains no Decisions API guide.
The user does not have preview documentation. The adapter needs a published
contract or documentation supplied through preview access.

On October 3, a fresh official documentation search for `"Decisions API"`
still returns zero hits. The current endpoint list and fetched API changelog
contain no Decisions entry. An official-domain web search also finds no guide.
The request schema and pricing remain unverified. No Decisions API requests
start, and the existing control sample remains available for the comparison.

On October 7, the official [Decisions API guide](
https://developers.openai.com/api/docs/guides/decisions) is published and
listed in the public documentation index. The API changelog entry dated
October 6 reads "Released the Decisions API in beta with `gpt-6-luna`."
The guide defines `POST /v1/decisions` and names `gpt-6-luna` as the only
model, in public beta. A request carries `model`, `input`, and `questions`.
Question types are `predicate`, `choice`, and `score`. Answers arrive in
an `answers` array keyed by the question `name`, and any answer may be a
`refusal`. A `choice` answer returns `choice`, `probabilities`, and
`confidence`. A `score` answer returns `score`, `probabilities`, and
`confidence`; the score is the probability-weighted average of zero-based
level indices. A `predicate` answer returns `probability` and no
`confidence`. Input costs $0.10 per 1M tokens, and only input tokens are
billed. Images must be inline base64 data URLs; hosted URLs and `file_id`
inputs are unsupported. Independent questions can share one request. The
guide documents no rate limits and no separate endpoint reference page.

## Prepared study

Local study: `.benchmark-studies/decisions-preview-2026-09-30`.
The existing pilot sampler freezes 256 decisions with seed `20260918`:

| Suite | Decisions |
|---|---:|
| Banking77 | 77 |
| SMS spam | 50 |
| Option count | 44 |
| Option order | 45 |
| Forced uncertainty | 40 |

`study.json` records source hashes, sample hashes, and selected item identifiers.
Source text remains local. The sample cannot merge with full-suite historical runs.

Controls use `openai:gpt-6-luna` and `jev-1.13.0`, with one repeat and one
worker per provider. A shared $2 cap includes probes and retries.
In-flight requests can exceed that cap, as documented by the existing runner.
Control artifacts go to `results/decisions-preview-controls-2026-09-30`.
The controls are not Decisions API results.

The control run completes all 512 decisions. Luna returns 256 valid answers;
jev returns 244 valid answers and rejects 12 option lists above 255 choices.
Known spend, including setup probes, is $0.03489273. The 12 jev rejections
omit token usage, so this amount is an incomplete known subtotal.
The sampler and publication checks pass: 15 tests. All five sample hashes match.

## Implemented adapter

`src/dmb/contenders/openai_decisions.py` adds `openai-decisions:gpt-6-luna`.
The adapter asks one `choice` question named `decision` using the shared
instruction string from `render.py`. Answers are matched by `name`, and the
returned `choice` value maps to `choice_index` by position in the original
options list. Missing or invalid confidence marks the answer malformed, so
every valid row carries confidence and the reports need no change. Refusal
answers are malformed. Error classes follow the jev adapter: 429 rate
limit, 401/403 auth, 400 provider rejection, 5xx transport.

The guide does not show the usage object. Usage accepts either documented
field naming (`input_tokens`/`output_tokens` or
`prompt_tokens`/`completion_tokens`); absent fields stay `None`, and the
raw response is preserved in full. The negotiation probe and the
accounting stop therefore block scored requests before spend whenever
usage stays unknown.

The registry gates the contender on `OPENAI_API_KEY` behind
`--include-decisions` or `--only-decisions`; it never runs by default.
The price table carries the verified $0.10 per 1M input-token entry with
zero cache and output rates.

The jev instruction and state helpers were renamed to provider-neutral
`render_choice_instructions` and `render_plain_state` with byte-identical
output. `prompt_fingerprint()` still equals the controls run's recorded
`4aec925478a8201511cee940eb52a552a7d8e3ac05d1d5adf2c9d1772db1a39f`, so a
Decisions run can merge with the controls in one report without a
protocol-mix flag.

Mocked contract tests in `tests/test_decisions_adapter.py` cover valid
answers, invalid identifiers, malformed shapes, refusals, HTTP classes,
usage handling, frozen settings, registry gating, pricing, and the
accounting stop. `uv run pytest` passes 315 tests; `uv run ruff check .`
is clean.

## Scored run (October 7, 2026)

`scripts/run_decisions_preview.py` runs `openai-decisions:gpt-6-luna`
against the exact frozen sample inside the study directory, after
verifying the sample hashes and the prompt fingerprint against the
control manifest. One repeat, one worker, matching the controls; a
separate $5 cap covered the authorization. The negotiation probe
succeeded (176 input tokens) and confirmed the usage object shape:
`input_tokens`/`output_tokens` with zero-valued cache detail fields.
Run `decisions-preview-decisions` finished with status `complete` and
all five cells ok.

Valid decisions: 240 of 256. The provider rejected the 12 option lists
of 256 or more choices: the endpoint caps `choices` at 255, the same
limit jev documents, and the items with 254 and 255 options succeed.
Four S5 no-good items returned refusal answers on both attempts and
count as malformed, not wrong. Known spend is $0.0187722 from 186,630
scored input tokens; the 12 rejected requests return no usage, so the
amount is an incomplete known subtotal.

Accuracy on valid rows, with controls in parentheses (Luna, jev):
S1 76.6 (84.4, 83.1); S2 100.0 (90.0, 98.0); S3 100.0 on 32 valid
(100.0, 100.0 on 32 valid); S4 88.9 (88.9, 86.7); S5 10.0 on 36 valid
(15.0, 5.0). Paired item-cluster bootstrap on S1: Decisions minus Luna
is -7.8 points (95% CI -14.3 to -1.3); on S2 it is +10.0 points (2 to
18); the jev intervals cross zero. Latency p50 is 215-239 ms against
Luna's 1,228-1,362 ms and jev's 252-277 ms.

The comparison report is `results/decisions-preview-comparison-2026-10-07`,
merging the controls and the scored run under one protocol hash. The
published controls report in
`results/decisions-preview-controls-2026-09-30` is untouched.

## Remaining steps

1. Expand to Banking77, CLINC150, and NLU++ validation and test suites.
   Treat the native multiple-question mode as a separate experiment, as the
   guide limits shared requests to independent questions. Measure cost and
   latency per complete message and per question.

## Completion criteria

A verified adapter passes mocked tests and repository checks. A real Decisions
API run records effective settings, hashes, usage, failures, and terminal status.
A report compares matched items and labels missing measurements explicitly.
No endpoint, confidence value, price, or benchmark result is invented.
