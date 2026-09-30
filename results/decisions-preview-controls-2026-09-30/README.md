# Controls for the Decisions API benchmark

This run tests ordinary GPT-6 Luna and jev on September 30, 2026.
It does not test the Decisions API. A public preview contract is not found.
See the [implementation plan](../../plans/002-decisions-api.md) for the evidence
and remaining work.

The frozen sample contains 256 decisions per contender. Each contender uses
one repeat and one worker. The shared cap is $2, including probes and retries.
The run completes all 512 decisions. Luna returns 256 valid answers. Jev returns
244 valid answers and rejects 12 option lists above 255 choices.

Known spend is $0.03489273: $0.02135092 for Luna and $0.01354181 for jev.
The 12 rejected jev requests omit usage. This subtotal is incomplete and is not
an invoice total. Rates come from the repository's frozen price snapshot.

| Control | Banking accuracy | Spam accuracy | Banking median latency |
|---|---:|---:|---:|
| GPT-6 Luna | 84.4% | 90.0% | 1,362 ms |
| jev 1.13.0 | 83.1% | 98.0% | 264 ms |

This small sample does not establish general model rankings. The banking suite
has one item per intent. The spam suite has 50 items. Native confidence from
jev and prompted confidence from Luna have different meanings.

Open the [full report](decisions-preview-controls-2026-09-30.md) or its
[HTML version](decisions-preview-controls-2026-09-30.html).
The report includes option-count results, option-order results, uncertainty
diagnostics, failures, paired comparisons, and confidence limitations.
`study.json` records the selected item identifiers and hashes.

Full texts and logs remain in the ignored local study directory:
`.benchmark-studies/decisions-preview-2026-09-30`.
The generated archive follows the repository's SMS publication rules.

Reuse the exact frozen sample for the Decisions API once its schema, access,
and pricing are verified. Keep native multiple-question requests in a separate
experiment. Measure their latency and cost per complete message.
