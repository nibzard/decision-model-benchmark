# Measuring typed decisions across jev and constrained LLM endpoints

DMB compares jev, eight constrained LLM configurations, and three
baselines on banking intent, SMS spam, planted code words at increasing
option counts, option-order stability, and forced uncertainty. The
[v3 report](../results/v3/v3.md) and its
[correction note](../results/v3/CORRECTIONS.md) supersede the earlier
interpretations. Publication on nibzard.com remains a manual step.

The measurements concern these endpoints and configurations. They
cannot settle whether an entire model class is faster, more accurate,
or more honest. The historical runs used three repeats per item;
repeats are grouped with their item when computing uncertainty.

## Accuracy, latency, and cardinality

Banking accuracy is highest for Cerebras gpt-oss-120b (81.3%) and
glm-5.3 (80.4%); jev scores 76.3%. On SMS spam, glm-5.3 scores 94.9%,
jev 93.0%, and glm-5.3-flash 91.4%. The majority baseline scores 87.7%.
The two OpenAI endpoints fall below that baseline on this particular
spam sample. Dataset composition and prompt choices limit generalization.

jev's median request latency ranges from 264 to 276 ms across suites,
with Cerebras at 303–346 ms. The other tested endpoints range more
widely. These observations do not substantiate a general 40–200× speed
claim. Historical latency covers a request, while new protocol v3
latency includes the full decision and retry backoff.

The clearest behavioral boundary is jev's choice cap. It solves the
planted code-word task at 254 and 255 choices, then rejects requests at
256, 384, and 512. The tested LLM endpoints produce valid decisions at
512 choices. Provider rejections remain measured failure outcomes;
accuracy alone excludes them, so coverage must accompany accuracy.

## Confidence needs a qualified interpretation

Historical S5 confidence behavior differs across endpoints. LLM
confidence is a prompted correctness probability, whereas jev exposes
a provider-defined score whose semantics are not documented as that
probability. The historical prompts also gave unequal uncertainty
instructions. Those measurements cannot support the former claim that
“only jev bluffs,” nor a ranking of model honesty.

S5 has two distinct subsets. No-good-option items have no valid answer;
v3 reports their confidence distribution and the prespecified fraction
at or below 0.5. Underdetermined items contain an unobservable randomly
planted reference answer. Their correctness calibration is descriptive
of that synthetic construction. The quoted jev ECE of 0.246 belonged
to this second subset and excluded every no-good-option item.

The current prompts share an uncertainty instruction. This change does
not repair historical observations. The report retains their provenance
and labels native scores separately. It adds descriptive risk/coverage
curves; deploying a confidence threshold needs separate held-out
selection and validation data.

## Option order and repeats both affect stability

The original pooled S4 statistic changes on 13% of jev base items and
37% of mini base items. It pools nine observations per base item across
three permutations and three repeats. That conflates changes caused by
order with repeat variability.

Even with the same option order, choices change on 5% of jev base
items and 25% of mini base items. v3 separates within-order repeat
disagreement from matched across-order disagreement and states the
observation counts and missing-data rules. Across-order disagreement
still includes stochastic variation; this design does not identify a
causal positional preference.

## Costs are qualified historical calculations

The former “ten times cheaper” claim was arithmetically wrong. The
unrounded nominal S1 cost ratio of nano to jev is approximately 2.49×.
That is a ratio under the original list-price calculation, not a
verified invoice comparison.

Historical final-attempt usage loses some retry spend, and the original
price snapshots have no cache rates. v3 preserves those snapshots and
reports known cost subtotals with missing components. It never uses
today's cache prices to reprice old runs. Current snapshots now include
verified OpenAI and Anthropic cache-read list prices; gateway bills,
unverified cache writes, and the vendor-claimed jev rate remain explicit
limitations.

## Corrections and reproducibility

v2 corrected majority-baseline prior provenance, macro-F1 labels,
baseline zero cost, retry-cost qualifications, and latency scope. v3
adds archive sanitization, verified price-snapshot hashes and protocol
versions, corrected S4/S5 definitions, item-cluster uncertainty, and
qualified accounting. Historical numeric reports remain accessible
with prominent correction links. SMS text and quoted reasoning were
removed from all published archives without changing recorded decisions.

Banking77 inputs are pinned to a complete repository commit with CSV
and license checksums. SMS ZIP bytes and the exact extracted member are
checked too, including on cache hits. All five suite item hashes match
the original release. S4 derives from Banking77 and retains its
attribution; the current UCI SMS page lists CC BY 4.0, while DMB keeps
message text local as its own publication policy.

Run `uv run dmb build` and `uv run python scripts/regenerate_corrections.py`
to reproduce the corrected report without provider calls. The README
also documents new-run setup, spending stops, interruption handling,
and the exact artifacts written.
