# Recent-model pilot

one per Banking77 intent; SMS prior preserved; four per cardinality; 15 S4 bases/all three orders; 20 items from each S5 half.

small stratified pilot, one repeat; no repeat-stability inference; S1 macro-F1 has one reference item per intent; S4 has only 15 bases; not mergeable with historical full-suite runs.

Minimum supported reasoning settings are pinned per endpoint. Costs use dated standard paid-tier list rates; Gemini account tier/invoices are not reconciled. No tools, grounding, or explicit cache storage are requested. Astra is excluded. All started attempts and probes count toward the shared cap.

Gemini Pro originally exceeded this account's 25 RPM quota. Its five cells are replaced by a rerun attempt with one worker and requests at least 3 seconds apart. Pro latency includes pacing and is not a like-for-like speed comparison. Both runs are retained in the archive. Original known spend: $1.54354597; rerun known spend: $0.30424000; combined: $1.84778597. Usage-less 429 responses leave an accounting qualification; these figures are known list-price costs, not an invoice reconciliation.

The paced Pro rerun was interrupted after hitting a separate 250 requests/day quota (API retry delay about six hours). It yielded 114 valid decisions, including all 77 banking items and 37 SMS items. The other five models each completed all 256 decisions successfully. The first Pro run yielded 135 valid decisions across the five suites; those original observations remain available in the original-run report and archive. The merged report replaces whole Pro cells with the rerun, including interrupted or unstarted cells, rather than selecting favorable individual answers. No claim of a completed six-model comparison is made.

Jev was added on the exact same frozen sample with one repeat and the shared uncertainty instructions, using pinned jev-1.13.0. Its native confidence is provider-defined, so probability calibration comparisons remain qualified. Input price $0.042/MTok, output free, reconfirmed at https://docs.typesafe.ai/models on 2026-09-26; invoices not reconciled. Jev known spend including probes: $0.01354181; all runs combined: $1.86132778.

Jev returned 244/256 valid decisions: banking accuracy 80.5%, spam accuracy 98%, and median request latency 0.262 to 0.277 seconds across suites. The 12 cardinality failures were provider rejections at N=256, 384, and 512; accepted cardinalities through 255 were all correct. Rejected calls lacked usage, so Jev cost is a known subtotal. On 20 no-good-option items, mean native confidence was 0.5365; this score is provider-defined and is not assumed to be the same probability requested from LLMs.
