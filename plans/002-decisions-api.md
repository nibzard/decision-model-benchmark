# Plan 002: Benchmark the Decisions API

## Status

- Goal: Add the Decisions API and compare it with Luna and jev.
- Started: September 30, 2026.
- Status: Controls complete; preview contract needed for the adapter.

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

## Remaining steps

1. Obtain the preview contract: endpoint, authentication, model identifier,
   request schema, answer identifiers, response schema, limits, usage, and pricing.
2. Implement a separate contender using that contract. Preserve original answer
   order and map the returned identifier to `choice_index`. Preserve failed
   request usage. Let the runner own retries. Freeze the effective configuration.
3. Check confidence behavior. Reports currently require confidence on valid rows.
   If the API omits it, support valid answers without confidence across reports
   and intent metrics. Exclude those answers from score diagnostics and threshold
   fitting. Do not substitute a constant or an unverified probability.
4. Add mocked contract tests for valid answers, invalid identifiers, malformed
   responses, authentication errors, rate limits, usage, and frozen settings.
   Verify pricing and the accounting stop before making scored requests.
5. Run Decisions API against these exact frozen items. Match control concurrency
   and repeat settings. Set a separate small cap using verified preview pricing.
   Report accuracy, failures, latency, cost, and paired item differences.
6. Expand to Banking77, CLINC150, and NLU++ validation and test suites.
   Treat any native multiple-question mode as a separate experiment. Measure
   cost and latency per complete message and per question.

## Completion criteria

A verified adapter passes mocked tests and repository checks. A real Decisions
API run records effective settings, hashes, usage, failures, and terminal status.
A report compares matched items and labels missing measurements explicitly.
No endpoint, confidence value, price, or benchmark result is invented.
