# Measuring a decision model against constrained LLMs: an independent benchmark

TypeSafe AI shipped jev ("System One") with vendor-run numbers: 40x to
200x faster than an LLM, about $0.042 per million input tokens with free
output, calibrated confidence, zero schema violations, and a hard cap of
255 choices. Every number came from their own evaluation, and their
launch post admits the bias. No independent measurement existed. This
benchmark is one.

The narrow question: for a typed decision — pick one of N options,
return a confidence you can threshold — what does each model class
actually deliver, at what latency, cost, and failure rate?

Publishing this post on nibzard.com is a manual step. The generated
report and the raw archive are the source of truth and live in the
repository under `results/` (`v1.md`, `v1.1.md`, and the
`v1-raw.tar.gz` / `v1.1-raw.tar.gz` logs they recompute from); every
number below quotes them.

## Method in one paragraph

Every contender gets the same serialized state text and the same option
list, and must return `{choice_index: int, confidence: float}`. Five
suites: 77-way banking intent (banking77, CC BY 4.0), SMS spam (UCI,
kept local, item text never republished), a cardinality sweep from 2 to
512 options with a planted code word, the same banking items with
permuted option order (the position-bias probe), and forced-uncertainty
items where no option is correct or the state underdetermines the
answer. Temperature 0 everywhere (or the provider minimum), concurrency
capped at 4 per provider, one recorded retry on malformed replies,
exponential backoff on rate limits, prices pinned with the date they
were checked, cost computed from provider usage fields — never
estimated. Three repeats per item; latency percentiles cover all valid
rows. Raw logs publish with the report; anyone can recompute every
number.

## Contenders

Three deterministic baselines (random, majority, keyword), eight LLMs
(gpt-5.4-nano, gpt-5.4-mini, claude-haiku-4.5, claude-sonnet-4.6,
glm-5.3, glm-5.3-flash, deepseek-chat, gpt-oss-120b on Cerebras), and
jev. Twelve contenders, five suites, 60 cells, zero skipped cells, zero
DNFs. Version 1 ran the baselines and LLMs (55 cells, $28.14); version
1.1 ran jev on the same frozen items and protocol (5 cells, $0.21).

## Results

The headline is never a single speedup. It is the per-suite table;
these are the rows that matter. Accuracy covers valid decisions on
gold-labelled items only. Valid-decision rates sit above 99% everywhere
except three cells, which the report marks with a star: jev rejects all
S3 requests with 256 or more options (27% of that suite's rows, a
measured outcome), haiku lost 90 S4 rows to Anthropic rate limits after
the recorded retry, and nano returned "none" (out-of-schema) on 6.2% of
S5 rows. Malformed replies are near zero everywhere else.

| Contender | S1 banking 77-way | S2 spam | S3 code word | S4 permuted | Cost / 1k (S1) | p50 latency |
|---|---|---|---|---|---|---|
| jev | 76.3% | 93.0% | 100% to N=255, fails at 256+ | 76.7% | $0.07 | 264-276 ms |
| glm-5.3 | 80.4% | 94.9% | 100% | 82.0% | $2.42 | 2.4-5.6 s |
| glm-5.3-flash | 79.0% | 91.4% | 100% | 82.4% | $0.21 | 2.0-3.5 s |
| gpt-oss-120b (Cerebras) | 81.3% | 73.0% | 99.6% | 82.3% | $0.32 | 303-346 ms |
| deepseek-chat | 76.2% | 75.9% | 100% | 75.1% | $0.27 | 731-776 ms |
| claude-haiku-4.5 | 75.7% | 76.3% | 100% | 79.6% | $0.82 | 2.4-4.4 s |
| claude-sonnet-4.6 | 74.8% | 77.7% | 100% | 79.2% | $2.48 | 1.8-2.6 s |
| gpt-5.4-mini | 74.0% | 66.1% | 99.8% | 73.1% | $0.69 | 660-710 ms |
| gpt-5.4-nano | 70.9% | 67.3% | 93.2% | 68.6% | $0.19 | 684-782 ms |
| keyword baseline | 29.3% | 54.0% | 100% | 38.0% | $0 | 0 ms |
| majority baseline | 1.3% | 87.7% | 8.0% | 2.0% | $0 | 0 ms |
| random baseline | 1.7% | 53.7% | 5.5% | 2.7% | $0 | 0 ms |

Read the table before the claims:

- **Quality: no class wins.** The best banking-intent score belongs to
  gpt-oss-120b (81.3%) and glm-5.3 (80.4%); jev lands mid-pack (76.3%).
  On spam, the GLM models beat everything (94.9%, 91.4%); jev is close
  (93.0%); the OpenAI models sit at 66-67%, below the majority
  baseline's 87.7%. A decision model is not a quality upgrade over a
  well-pinned LLM. It is also not a downgrade.
- **Speed: fastest measured, not 40-200x.** jev's p50 is 264-276 ms
  across all five suites, flat from 2 to 255 options. That is 1.2x
  faster than gpt-oss-120b on Cerebras (331 ms on S1), 2.4x faster
  than gpt-5.4-mini (660 ms), and 10-16x faster than the thinking-mode
  models (glm-5.3 up to 5.6 s p50, haiku up to 4.4 s). The 40-200x
  claim holds only against LLMs left in their slowest default mode.
- **The 255 cap is real and sharp.** At 254 and 255 options jev answers
  with 100% accuracy and 0.97 mean confidence. At 256, 384, and 512 it
  rejects the request: `400 Too many choices. Must have at most 255
  choices.` Those cells count as measured failures, not errors of the
  harness. Every LLM handles 512 options; gpt-5.4-nano is the only one
  that loses accuracy on the way (93.2% overall, degrading with N).
- **Schema: the zero-violation claim holds.** Of 4,125 jev requests,
  3,900 returned a valid, in-range decision and 225 were rejected by
  the provider (the 256-option cap above). Not one jev reply violated
  the schema. The LLMs are close: malformed replies are under 1%
  everywhere except nano on S5 (6.2%), where it answers "none" on
  no-good items.
- **Honesty: jev is the only contender that bluffs.** S5 has items with
  no correct option. A calibrated decision-maker must return low
  confidence there. Every LLM admits ignorance on 97.3-100% of those
  items (confidence <= 0.5; deepseek's mean confidence on them is
  0.07). jev admits on 49.7%, with mean confidence 0.54 — it asserts
  more often than it hedges when nothing is right. Its S5 calibration
  error is the worst measured (ECE 0.246; the LLMs range 0.039-0.122).
  On S1, where answers exist, jev's ECE is mid-pack (0.083; glm-5.3
  0.043, mini worst at 0.226). "Calibrated confidence" is true where
  the question is answerable and false exactly where calibration
  matters most for a decision system.
- **Order stability: the position-bias probe.** Rerun the same banking
  items with permuted option order. jev is the most stable: 13% of base
  items change their mapped choice. gpt-oss-120b (15%) and glm-5.3
  (16%) match it. The Anthropic models flip on 30-34% of items;
  gpt-5.4-mini is the worst at 37%. The random baseline flips 100%;
  majority 0%. Flip rate is a real, publishable axis that vendor evals
  do not measure.
- **Cost: cheapest, by ten times or more.** At list prices, jev costs
  $0.07 per 1,000 decisions on the banking suite. nano ($0.19),
  glm-flash ($0.21), deepseek ($0.27), and gpt-oss-120b ($0.32) are
  next. sonnet ($2.48) and glm-5.3 ($2.42) cost the most. The vendor's
  per-token price is a claim on an invoice we have not seen; the
  measured field here is usage-based cost per decision, and on that
  basis jev is the cheapest live contender.

## What the tables cost

$28.34 of measured provider spend across both releases ($28.14 for the
LLM release, $0.21 for jev), 2.9 hours of wall time for v1 plus 5
minutes for v1.1, and 49,500 logged decision rows. The full manifests
pin item-file hashes, the price-table hash, library versions, and every
protocol deviation. Nothing was cherry-picked; the reports are
generated, not written.

## Protocol deviations, recorded not hidden

- Z.ai GLM models cannot disable thinking; the runs pin the provider's
  minimum level ("low") so suites fit the wall-time budget.
- The Anthropic endpoint here is a gateway. It serves thinking-enabled
  output by default and ignores the forced tool call on about 44% of
  replies; the adapter pins thinking off (provider minimum) and parses
  the text answer when the tool call does not arrive, tagged per row in
  the raw log.
- DeepSeek list prices are peak-hour prices; off-peak is half.
- S2 option order is shuffled per item. The first smoke run exposed a
  fixed-order artifact: with `["ham", "spam"]` always in that order,
  position bias and label skill could not be separated. The suite was
  fixed and re-frozen before the full run; the other four suites
  rebuilt byte-identical.

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
  position bias and bluffing get their own suites instead.

## Reproduce it

Clone the repository, set API keys, run `uv run dmb build`, `uv run dmb
run --run-id v1`, and `uv run dmb report runs/v1 --out results`. The
item files rebuild byte-identical from a pinned seed. Compare the hashes
in your manifest against the published ones.
