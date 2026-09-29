# Jev expanded benchmark results

Run date: September 29, 2026. Model: `jev-latest`.
All 40,226 validation and test decisions completed with valid responses.
The runs use one repeat and all items in each split.

## Test results

| Suite | Main score | Additional score | Latency p50 / p95 |
|---|---|---|---|
| Banking77 | 79.2% accuracy | 3,080 messages | 316 / 570 ms |
| CLINC150 | 88.6% accuracy | Out-of-scope precision 90.3%; recall 81.2% | 279 / 416 ms |
| NLU++ | 48.3% micro intent F1 | Macro intent F1 58.1%; complete-message accuracy 4.3% | 267 / 333 ms |

NLU++ contains 302 test messages and 13,712 binary intent decisions.
Its binary accuracy is 91.4%, but this includes many negative labels.
Complete-message accuracy requires every intent label to be correct.
Macro intent F1 counts absent intents as zero.

## Frozen thresholds

Thresholds use validation results only. The selection target is at most
5% empirical error with at least 100 accepted decisions or complete messages.

| Suite | Threshold | Test coverage | Test error among accepted |
|---|---|---|---|
| Banking77 | 0.96 | 51.2% | 3.68% |
| CLINC150 | 0.62 | 88.3% | 6.65% |
| NLU++ | No qualifying threshold | 0% | Not applicable |

CLINC150 exceeds the validation error target on test data.
The validation target is not a guarantee of test error.
Jev confidence is a provider-defined score, not a calibrated probability.

## Cost and execution

Recorded validation cost: $0.63822612. Recorded test cost: $1.07135381.
Total: $1.70957993, using the repository's recorded rates and token usage.
The jev rate comes from a vendor claim and is not invoice-verified.
Each run had a $5 cap.

Validation and test ran concurrently, with four workers in each run.
Latency reflects that load and includes retries where applicable.
Threshold fitting used only validation results; test results did not change
the thresholds.

NLU++ uses one binary Choice request per intent. Its latency is per request,
not per complete message. Native Noul and batched questions are not tested.
The selected folds and binary task differ from published NLU++ evaluations.

## Artifacts

- [Test report](test/expanded-test-jev-20260929.md)
- [Validation report](validation/expanded-validation-jev-20260929.md)
- [Frozen thresholds](thresholds.json)
- [Threshold evaluation](threshold-evaluation.json)

Each report directory contains HTML, cell metrics, plots, and a raw archive.
See the [benchmark guide](../../docs/expanded-benchmarks.md) for data sources,
licenses, split rules, and commands.
