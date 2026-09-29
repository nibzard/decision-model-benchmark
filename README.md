# DMB: decision-model benchmark

DMB compares APIs that choose one option from a list and return a confidence
score. It measures accuracy, latency, cost, failures, and how confidence relates
to correctness. The contenders include TypeSafe AI's jev, LLMs with structured
output, and deterministic baselines.

In the recent pilot, jev scored 80.5% on banking and 98% on spam, with median
latency around 0.27 seconds. GPT-6 Luna was the cheapest LLM, and Gemini 3.8 Flash
scored 88.3% on banking and 100% on spam. Historical runs put jev and Cerebras
close on latency.
These findings apply to the tested endpoints, settings, and datasets. Small
samples and differences between studies limit broader comparisons.

## Recent findings

The September 26, 2026 pilot planned 256 decisions per model across five suites,
with one repeat and a shared $10 cap. It initially tested six LLMs, then added
jev on the exact same frozen items and current protocol. Astra was excluded.
Five LLMs returned valid decisions on all items. jev returned 244 valid decisions
and rejected 12 requests above its option limit; Gemini Pro reached account
quotas before completing its rerun. Total known list-price cost was **$1.86**,
including probes, retries, both Pro runs, and jev. Provider invoices were not
reconciled.

| Model | Banking accuracy (77 items) | Spam accuracy (50 items) | p50 latency across suites | Known decision cost | Coverage |
|---|---:|---:|---:|---:|---|
| jev 1.13 | 80.5% | 98% | 0.26–0.28 s | $0.014* | 244/256 valid; 12 option-limit rejections |
| GPT-6 Luna | 80.5% | 90% | 0.85–0.99 s | $0.021 | Complete |
| GPT-6 Sol | 85.7% | 94% | 1.12–1.33 s | $0.426 | Complete |
| GPT-5.6 Terra | 88.3% | 76% | 0.88–1.01 s | $0.437 | Complete |
| Gemini 3.8 Flash | 88.3% | 100% | 1.33–1.98 s | $0.216 | Complete |
| Gemini 3.5 Flash-Lite | 77.9% | 66% | 0.74–0.78 s | $0.073 | Complete |
| Gemini 3.1 Pro Preview | 88.3% | 100% of 37 valid items | 3.07–3.12 s | Not comparable | Partial |

Each range spans the per-suite medians, not all request times. Pilot latency
covers each complete decision, including retries and backoff. Pro has latency
data for two suites only; its rerun included pacing between requests. Its speed
is not directly comparable to the other models.

Per-model costs above cover the 256-item sample's recorded decision attempts and
exclude setup probes. *jev's figure is incomplete: rejected requests did not
report token usage. Its price uses the [provider's published rate](https://docs.typesafe.ai/models),
not a reconciled invoice.
Pro's spam score uses only its 37 valid rerun responses; it is not a result on
all 50 items. The total includes costs from the original Pro run even where the
rerun replaced its observations in the merged report.

- jev matched Luna's banking accuracy and scored 98% on spam, versus Luna's 90%.
  Its median latency was 0.26 to 0.28 seconds across suites, faster than every
  tested LLM in this pilot. Its known decision cost was lower, with the rejected
  request accounting gap noted above.
- Luna was the cheapest LLM at about two cents for all 256 decisions, with 90%
  spam accuracy.
- Flash matched Terra's banking score and answered all 50 spam items correctly
  at roughly half Terra's decision cost.
- Flash-Lite was the fastest LLM, with median latency around 0.75 seconds per
  decision
  across suites, but scored only 66% on spam.
- All five complete LLMs scored 100% on the planted code-word task, including
  lists of up to 512 options. This sample did not distinguish them on that task.
  jev answered all 32 accepted items correctly through 255 options and rejected
  all 12 items at 256, 384, and 512 options.
- When every option was wrong, average confidence ranged from 5.8% for Terra to
  22% for Flash. Those scores describe responses to the uncertainty prompt;
  they do not establish general calibration.
  jev's mean native score on the same no-good-option items was 53.7%. Its
  provider-defined score has not been established as a probability of correctness,
  so the confidence values need different interpretations.

One repeat cannot establish repeat stability. The banking sample has only one
item per intent, and the order suite has 15 base items. Use these scores to
choose follow-up evaluations. A comparison with
older models on uncertainty needs fresh S5 runs using the corrected common
prompt.

Read the [pilot report](results/recent-pilot-2026-09-26/recent-pilot-2026-09-26.md)
or open its [HTML version](results/recent-pilot-2026-09-26/recent-pilot-2026-09-26.html).
The [study note](results/recent-pilot-2026-09-26/STUDY.md) explains sampling and Pro
coverage. The [original-run report](results/recent-pilot-2026-09-26/original-run/original-run.md)
retains the first Pro observations.

<details>
<summary>Pilot sampling, settings, and Pro quotas</summary>

The fixed sample contains 77 banking items, one per intent; 50 SMS items at
approximately the source class prior; four items at each of 11 option counts;
all three permutations of 15 banking items; and 20 items from each S5 subset.
Sample IDs and source/sample hashes are recorded in
[study.json](results/recent-pilot-2026-09-26/study.json). The original full-suite
files are unchanged. Different item hashes prevent merging the pilot with
historical full-suite reports.

OpenAI Sol, Luna, and Terra use reasoning effort `none`. Gemini Flash uses
thinking `low`, Flash-Lite uses `minimal`, and Pro uses `low`. Gemini requests
native JSON Schema output. Thinking tokens count as output usage; cache reads
and OpenAI cache writes have separate rates. Price snapshots use September 26,
2026 standard list rates. Gemini 3.8 Flash's promotional rates expire after
December 31, 2026.

The first Pro run returned 135 valid decisions across the five suites while
hitting a 25 requests/minute quota. A rerun used one worker and requests at least
three seconds apart. It returned 114 valid decisions, including all 77 banking
items and 37 SMS items, then hit a separate 250 requests/day quota. Further calls
were stopped. Pro rerun latency includes pacing and is not directly comparable
to the other models' latency.

The merged report replaces each Pro cell with the rerun's whole cell, including
interrupted or unstarted cells. It does not select individual favorable answers.
Both Pro runs and the added jev run remain in the sanitized archive. Rate-limit
and jev option-limit responses without usage leave accounting gaps. The $1.86
subtotal covers known usage; the provider bill has not been verified.

jev uses pinned `jev-1.13.0`, one repeat, the same frozen item hashes, and the
shared semantic uncertainty instructions. Its native request format differs
from the LLM JSON format. Its confidence remains labeled provider-defined.
The run cost $0.01354 in known usage including probes, at the provider's
published input rate of $0.042/MTok with free output. All 12 rejected requests
are retained as failures in coverage, rather than counted as successful answers.

</details>

## Historical findings

The [v3 report](results/v3/v3.md) is the corrected report of record for the
historical full-suite runs. Its [correction note](results/v3/CORRECTIONS.md)
explains the interpretation and accounting changes. The historical runs used
different items and protocols from the recent pilot, so their scores belong in
a separate comparison.

| Question | Historical finding | Limit |
|---|---|---|
| How accurate are the endpoints? | LLM banking accuracy ranged from 70.9% to 81.3%; jev scored 76.3%. On spam, glm-5.3 scored 94.9%, jev 93.0%, and glm-5.3-flash 91.4%. | Some cells have partial valid coverage; see the report. |
| How fast is jev? | Median request latency was 264 to 276 ms for jev and 303 to 346 ms for Cerebras gpt-oss-120b across suites. | These measurements do not support a general 40 to 200 times speed claim. |
| How many options work? | jev succeeded through 255 choices and rejected 256, 384, and 512. Every tested LLM endpoint returned valid decisions with 512 choices. | The task is planted code-word retrieval. |
| Do answers change? | Pooled S4 instability was 13% for jev and 37% for mini. Identical-order repeats changed on 5% and 25% of base items, respectively. | Pooled instability combines repeat variation and option permutations. |
| Can confidence and cost be compared directly? | Historical S5 prompts gave unequal uncertainty instructions; historical cost records omit some retry usage and cache rates. | Fresh common-prompt runs and complete usage are needed for those comparisons. |

Historical p50 latency by model, shown as the range of per-suite medians:

| Model | p50 latency across suites |
|---|---:|
| jev | 264–276 ms |
| gpt-oss-120b (Cerebras) | 303–346 ms |
| gpt-5.4-mini | 660–710 ms |
| gpt-5.4-nano | 684–782 ms |
| deepseek-chat | 731–776 ms |
| claude-sonnet-4.6 | 1.8–2.6 s |
| glm-5.3-flash | 2.0–3.5 s |
| claude-haiku-4.5 | 2.4–4.4 s |
| glm-5.3 | 2.4–5.6 s |

Historical cells come from mixed protocol versions, so latency scope can differ.
The [v3 latency table](results/v3/v3.md#latency-p50-ms) gives each suite value.

![Historical accuracy and latency versus option count](results/v3/cardinality.svg)

<details>
<summary>Confidence, stability, and historical cost corrections</summary>

S4 reports repeat disagreement separately from disagreement across option
orders. The historical experiment does not isolate a causal position bias.

jev's confidence score is provider-defined; LLMs are prompted for the probability
that their chosen option is correct. Historical prompts gave different
uncertainty instructions, so the original "only jev bluffs" verdict was
unsupported. The reported S5 expected calibration error (ECE) of 0.246 concerns
the underdetermined half,
whose reference answer was randomly planted. It excludes items with no good
option.

The original nominal S1 cost ratio of nano to jev was about 2.49 times, using
unrounded values. Exact historical bills are unknown because retry usage is
incomplete and snapshots omit cache rates. v3 separates known cost subtotals
from historical nominal calculations. jev's price remains a vendor claim, and
gateway invoices were not reconciled.

The report includes machine-readable metrics, paired item-cluster uncertainty
intervals, descriptive risk/coverage curves, plots, and sanitized observations.
Confidence curves do not select a deployment threshold. Choose a threshold on
separate held-out data and evaluate its coverage and error rate.

</details>

## What the benchmark measures

Each item supplies a state and a list of options. A contender returns
`{choice_index: int, confidence: float}`. DMB validates the response and records
provider usage and failures alongside the decision.

| Suite | Task | Main measurement |
|---|---|---|
| S1: banking intent | Choose among 77 Banking77 intent labels | Classification accuracy and confidence |
| S2: SMS spam | Choose spam or ham (a legitimate message) | Binary classification accuracy and confidence |
| S3: cardinality | Find a planted code word as option count grows | Accuracy, latency, and accepted option counts |
| S4: option order | Classify banking items with reordered options | Answer disagreement across repeats and orders |
| S5: uncertainty | Choose when no option is correct or evidence is insufficient | Confidence under forced choice |

This is a single-decision benchmark. It does not evaluate prose quality, agent
workflows, tool use, or fine-tuning. [SPEC.md](SPEC.md) defines the suites,
metrics, contenders, and execution rules.

## Get started without paid calls

Install Python 3.12 and `uv`, then run:

```bash
uv sync --locked
uv run dmb build
```

The build downloads source data and creates frozen suite files. Downloads,
including cached bytes, must match pinned checksums. Banking77 is pinned to a
complete repository commit containing its license. All five released item
hashes match the rebuilt files.

<details>
<summary>Regenerate the historical correction and run checks</summary>

```bash
uv run python scripts/regenerate_corrections.py
uv run pytest
uv run ruff check .
```

The correction script reads the three published archives, checks source
snapshots and numeric observations, regenerates v3, and sanitizes every archive.
It makes no provider calls.

</details>

## Run a paid benchmark

For the six-model pilot, set `OPENAI_API_KEY` and `GEMINI_API_KEY` in your
environment, then choose a new study ID:

```bash
uv run python scripts/run_recent_sample.py --study-id my-recent-pilot \
  --profile six --hard-cap 10
```

This plans 1,536 decisions, excludes Astra, and counts probes and retries toward
the shared cap. `--profile cheap --hard-cap 2` selects only Luna and Flash-Lite.
Add `--prepare-only` to create the sample without provider calls.

Local items and full logs go to ignored `.benchmark-studies/<study-id>`;
reports, sample provenance, and sanitized archives go to `results/<study-id>`.
Existing study IDs are refused.

To add jev to an existing terminal pilot, set `TYPESAFE_API_KEY` and run:

```bash
uv run python scripts/run_recent_sample.py --study-id my-recent-pilot \
  --add-jev --hard-cap 10
```

This uses the existing frozen items, pins Jev 1.13, and deducts all prior runs'
known spend from the original cap. It adds jev to the report and sanitized
archive without rerunning the LLMs. The full 256-item sample includes option
counts above jev's supported limit so those rejections remain visible.

<details>
<summary>Recover a Pro run after a minute quota</summary>

Check the API error first. Pacing can help a minute quota; it cannot bypass a
daily quota. Once the original pilot is terminal, run:

```bash
uv run python scripts/run_recent_sample.py --study-id my-recent-pilot \
  --repair-pro --hard-cap 10
```

This attempts all 256 Pro decisions with one worker and requests at least three
seconds apart. It deducts the original known spend from the shared cap, retains
both runs, and labels Pro latency as including pacing. It cannot raise the
original cap or guarantee enough account quota to finish.

</details>

<details>
<summary>Run a selected model on the full suite or a smoke subset</summary>

A smoke run uses the first 50 items per suite and one repeat. Unlike the pilot,
it does not create a balanced sample.

```bash
uv run dmb run --run-id smoke --smoke \
  --contenders openai:gpt-6-luna --hard-cap 2
uv run dmb report runs/smoke --out results/smoke
```

Omit `--smoke` for the full suite. The default full run uses three repeats.
Explicitly select contenders and a budget; the registry includes Astra even
though the pilot excludes it. See `uv run dmb run --help` for suite, item-limit,
repeat, and contender options.

| Provider | Required environment variable |
|---|---|
| OpenAI | `OPENAI_API_KEY` |
| Gemini | `GEMINI_API_KEY` |
| Anthropic | `ANTHROPIC_AUTH_TOKEN` |
| Z.ai | `ZAI_API_KEY` |
| DeepSeek | `DEEPSEEK_API_KEY` |
| Cerebras | `CEREBRAS_API_KEY` |
| TypeSafe AI | `TYPESAFE_API_KEY` |

The general runner records skips for missing keys; the pilot requires all
selected credentials before probes start. Cerebras defaults to the priced
`gpt-oss-120b`. Selecting an unpriced model fails before setup calls.

</details>

<details>
<summary>Budget accounting, partial runs, and exit codes</summary>

New runs use protocol v3. Bounded setup probes record every outcome and reported
usage. Effective configuration is frozen before scoring, and every scored
attempt retains provider usage, including cache fields.

The general runner's default $40 soft target is advisory; its default $60 hard
cap stops new work based on priced known usage and separately labeled safe
estimates. The pilot overrides these defaults with its smaller cap. Requests
already in flight can overshoot a cap and finish after a stop. Their results and
usage are retained. Unknown rates or usage remain explicit accounting gaps;
unreported usage is not counted as a measured zero.

Run IDs name new directories. Existing runs are never resumed or overwritten.
Stopped runs retain terminal manifests and all started attempts.

| Exit code | Meaning |
|---|---|
| 0 | Complete |
| 2 | Invalid configuration or run ID |
| 3 | Execution failure |
| 4 | Budget or accounting stop |
| 5 | Authentication stop |
| 6 | Deadline stop |
| 130 | Interrupted |

</details>

## Data, artifacts, and publication

S1 uses Banking77 under CC BY 4.0 with attribution; S4 is its permutation
derivative. The [UCI SMS Spam Collection page](https://archive.ics.uci.edu/dataset/228/sms+spam+collection)
lists CC BY 4.0. DMB's privacy policy keeps SMS text local. Published S2 records
contain approved metadata only; sanitization also covers logs, errors,
reasoning, and probe files. S3 and S5 use generated data from seed 20260918.

DMB has no vendor sponsorship. Negative results and missing measurements remain
visible.

<details>
<summary>Where files live and how reports verify them</summary>

| Path | Contents |
|---|---|
| `data/suites/*.jsonl`, `hashes.json` | Rebuilt items and source checksums |
| `runs/<id>/manifest.json` | Protocol, effective settings, hashes, status, coverage, accounting, deviations, and skips |
| `runs/<id>/prices.snapshot.json` | Dated price table, verified by hash |
| `runs/<id>/results.jsonl`, `raw/*.jsonl` | Local results and attempt/probe logs |
| [results/v3](results/v3/) | Corrected historical report and sanitized archive |
| [results/recent-pilot-2026-09-26](results/recent-pilot-2026-09-26/) | Recent pilot reports, sample provenance, and sanitized archives |
| `results/v1*`, `results/v1.1*`, `results/v2` | Historical numeric reports with correction notices and sanitized archives |
| [blog/v1-post.md](blog/v1-post.md) | Revised narrative; publication is manual |

Reports validate item files, rows, price snapshots, and protocol provenance
before merging runs. Later runs replace earlier cells whole, including failed
cells. Protocol mixing requires `--allow-protocol-mix` and appears in the report.
Different item hashes cannot be merged.

</details>
