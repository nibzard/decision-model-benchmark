# Measuring decision models against constrained LLMs: an independent benchmark

Draft. Numbers land from `results/v1.md` when the full run finishes; the
method and caveat sections below are final.

Publishing this post on nibzard.com is a manual step. The generated
report and raw archive are the source of truth; this post only quotes
them.

## The claim under test

TypeSafe AI shipped jev ("System One") with vendor-run numbers: 40x to
200x faster than an LLM, about $0.042 per million input tokens with free
output, calibrated confidence, zero schema violations, and a hard cap of
255 choices. Every number came from their own evaluation, and their
launch post admits the bias. No independent measurement existed. This
benchmark is one.

The narrow question: for a typed decision — pick one of N options,
return a confidence you can threshold — what does each model class
actually deliver, at what latency, cost, and failure rate?

## Method in one paragraph

Every contender gets the same serialized state text and the same option
list, and must return `{choice_index: int, confidence: float}`. Five
suites: 77-way banking intent (banking77, CC BY 4.0), SMS spam (UCI,
kept local, text never republished), a cardinality sweep from 2 to 512
options with a planted code word, the same banking items with permuted
option order (the position-bias probe), and forced-uncertainty items
where no option is correct or the state underdetermines the answer.
Temperature 0 everywhere (or the provider minimum), concurrency capped
at 4 per provider, one recorded retry on malformed replies, exponential
backoff on rate limits, prices pinned with the date they were checked,
cost computed from provider usage fields — never estimated. Raw logs
publish with the report; every number recomputes from them.

## What counts as a failure

A reply that fails the schema or the bounds after one retry is
malformed, not wrong. A contender that cannot finish a suite inside
2,000 seconds is DNF for that suite and keeps its partial rows. Cost
overruns stop the grid at a hard cap of $60. Nothing is dropped
quietly: deviations, skips, and DNFs land in the manifest.

## Protocol deviations, recorded not hidden

- Z.ai GLM models cannot disable thinking; the runs pin the provider's
  minimum level ("low") so suites fit the wall-time budget.
- The Anthropic endpoint here is a gateway that serves thinking-enabled
  output by default and sometimes ignores forced tool calls; the adapter
  pins thinking off (provider minimum) and parses text replies when the
  tool call does not arrive, tagged per row in the raw log.
- DeepSeek list prices are peak-hour prices; off-peak is half.

## Results

<!-- Tables inserted from results/v1.md after the full run. -->

## Caveats

- One endpoint per provider: gateway behavior (thinking injection,
  tool-call support) is part of what a real deployment sees, but it is
  one deployment, not the provider's whole surface.
- The jev input price is a vendor claim until an invoice confirms it;
  the price table marks it as such.
- banking77 and the SMS suite are English, short-text, and old; the
  synthetic suites are code-word lookups. None of this measures
  long-context reasoning.
- Three repeats measure latency stability, not decision stability;
  position-bias and bluffing get their own suites instead.

## What is next

jev ships in v1.1 on the same frozen items and protocol, and the
cardinality suite gets the headline: measured behavior at the claimed
255-choice cap, versus the vendor's own numbers.
