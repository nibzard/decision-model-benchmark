# Corrections in v2

This report supersedes `v1.md` and `v1.1.md`. The original files stay
unchanged in this repository; the raw run directories are untouched. This
note maps each corrected number to the defect that produced the old one.

## What changed

| Metric | v1 (old) | v2 (corrected) | Defect |
|---|---|---|---|
| Majority baseline, S4 accuracy | 2.0% | 1.0% | The S4 order suite silently used its own frozen prior (`topping_up_by_card`, 6/300) instead of the S1 prior the protocol pins. The prior table let the last-loaded suite overwrite an option set shared by S1 and S4. |
| Majority baseline, S4 macro-F1 | 0.000509 | 0.000257 | Same prior defect, scored with the corrected prior and stable option-text labels. |
| Majority baseline, S4 confidence | 0.02 | 0.013333 | The recorded prior itself was wrong; the baseline reports the prior as its confidence. |
| Macro-F1 on S1, S2, S4 (all contenders) | positions as classes | option texts as classes | v1 macro-F1 averaged F1 over option positions. On S4 the options are permuted per item, so positions are not classes and the score was meaningless there. v2 uses stable option-text labels; the v1 numbers on S1 and S2 stay comparable because their option lists are fixed. |
| Macro-F1 on S3, S5 | numeric | n/a | S3 code words and S5 alternatives vary between items, so macro-F1 does not apply. v1 printed position-based numbers there anyway. |
| S2 majority macro-F1 | not published | 0.467140 | The majority baseline answers `ham` on every S2 item (prior 0.877). With the absent `spam` class scoring 0, macro-F1 is 0.467140. This is the corrected definition, not a run change. |
| Baseline cost | unknown (`-`) | $0.00 measured | v1 rows carried no usage for baselines, so cost rendered as unknown. Deterministic baselines make no billable calls; v2 records a measured zero. |
| LLM cost where retries happened | full number | marked `†` lower bound | v1 rows kept only the final attempt's usage, so retried decisions undercounted cost. v2 runs record every attempt; for v1 cells the report now marks the number as a lower bound instead of presenting it as exact. |
| Latency | per request | per decision (v2 cells) | v1 latency covered one request. Protocol v2 covers the whole decision, including retry backoff. The scope is stated per cell; v1 cells keep the request scope. |

## Where the corrected S4 cells come from

Run `v2-majority-fix` (protocol v2, included in this report's raw archive):
the majority baseline over S1 and S4 with the pinned S1 prior
(`apple_pay_or_google_pay`, 4/300 = 0.013333, source hash in the run
manifest). It replaces the v1 `baseline:majority` cells for S1 and S4
whole. Every other cell still comes from runs `v1` and `v1.1`; the
coverage table names the source run for each cell.

The v1 majority cells for S1 were also wrong in v1 (the same collision
gave S1 the S4 prior). The corrected S1 accuracy rounds to the same 1.3%
because the corrected prior option is almost never the gold answer
either; the confidence column changes from 0.02 to 0.013333, which is the
visible difference.

## Protocol versions merged

Runs `v1` and `v1.1` predate protocol v2; run `v2-majority-fix` records
protocol v2. They are merged here by explicit policy
(`--allow-protocol-mix`). The mix affects only the replaced
`baseline:majority` cells: their latency scope is the whole decision
rather than one request, and their usage is a measured zero rather than
unknown. No LLM cell was re-run.

## What was not fixed retroactively

The v1 measurement gaps stay gaps: per-attempt usage and retry latency
for v1 cells were never recorded, so v2 reports them as unknown or
lower-bound values instead of estimating them. Recoverable numbers
(accuracy, macro-F1, coverage, calibration) were recomputed from the
existing rows; missing measurements were not invented.
