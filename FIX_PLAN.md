# Benchmark correction plan

Status: proposed implementation plan. No fixes are implemented by this document.

This plan addresses the 12 findings from the full project review. It covers code changes, regression tests, and published artifacts.

The current baseline passes 84 tests and `uv run ruff check .`. Those checks do not cover the reviewed failures.

## Objectives

- Calculate metrics from the intended labels and observations.
- Record every attempted request, including failures and retries.
- Stop new work promptly after budget, deadline, or authentication failures.
- Preserve existing runs and identify incomplete runs.
- Verify every input used to combine reports.
- Correct published claims without concealing earlier results.

## Scope and constraints

- Keep the current retry count unless a new protocol version explicitly changes it.
- Keep the concurrency limit of four requests per provider.
- Use local fixtures and mocked transports for regression tests.
- Do not make paid provider calls during implementation tests.
- Preserve existing run directories and published archives before changing artifacts.
- Distinguish recovered measurements from values that require a new run.
- Do not estimate missing historical usage or retry latency.
- Record changes to prompts, scoring, and execution semantics in a protocol version.

A hard budget cannot prevent charges for requests already running. The runner must stop new requests and account for all completed requests.

## Finding map and implementation order

P1 means a high-priority correction. P2 means a normal-priority correction.

| Phase | Review finding | Priority | Main files |
| --- | --- | --- | --- |
| 1 | 4: Existing run IDs overwrite results | P1 | `src/dmb/runner.py`, `src/dmb/cli.py` |
| 2 | 2: Retry records lose usage, responses, and time | P1 | `src/dmb/contenders/base.py`, provider adapters, `src/dmb/runner.py` |
| 3 | 3: Abort handling loses requests and usage | P1 | `src/dmb/runner.py` |
| 3 | 6: Authentication failures drain queued work | P2 | `src/dmb/runner.py` |
| 3 | 7: Provider thread failures do not propagate | P2 | `src/dmb/runner.py`, `src/dmb/cli.py` |
| 4 | 1: Macro-F1 uses option positions | P1 | `src/dmb/report.py`, `src/dmb/metrics.py` |
| 4 | 11: S4 overwrites the S1 majority prior | P2 | `src/dmb/contenders/baselines.py`, `src/dmb/cli.py` |
| 5 | 9: Extra runs bypass hash checks | P2 | `src/dmb/report.py` |
| 5 | 10: Extra runs append instead of replacing cells | P2 | `src/dmb/report.py` |
| 6 | 8: Report fields do not match object fields | P2 | `src/dmb/report.py` |
| 6 | 12: Reports hide incomplete coverage | P2 | `src/dmb/report.py` |
| 7 | 5: Confidence instructions differ | P1 | Prompt renderer, jev adapter, `SPEC.md` |
| 8 | Correct and qualify published results | — | `results/`, `blog/v1-post.md`, `README.md` |

Phase 7 is required before any new comparative provider run. Phase 8 depends on the corrected metrics and report pipeline.

## Phase 1: Protect existing runs

### Finding 4: Refuse an existing run directory

The runner currently accepts an existing directory and truncates its result file. Old raw files can remain beside new results.

Implementation:

1. Validate the run ID before constructing contenders or making negotiation calls.
2. Restrict the ID to a single directory name. Reject absolute paths and parent-directory traversal.
3. Create the run directory exclusively. Fail with a clear message when it already exists.
4. Keep the exclusive creation check in the runner, even when the command line performs an earlier check.
5. Write an initial manifest before starting work. Mark it as running.
6. Update the manifest through a temporary file and atomic replacement.

Do not add resume or overwrite behavior in this correction. Both require separate rules for reconciling existing requests and records.

Tests and acceptance:

- A second run with the same ID fails before any provider call.
- Existing manifest, result, and raw files remain byte-identical.
- Two competing attempts to reserve one run ID cannot both succeed.
- Invalid IDs cannot write outside the configured run directory.
- An interrupted run retains a readable manifest without a false completion status.

## Phase 2: Preserve every attempt

### Finding 2: Introduce explicit attempt records

Current failures contain only an error string. Successful retries contain only the last call's usage and latency.

Implementation:

1. Define an attempt record with outcome, elapsed time, usage, response, and error fields.
2. Capture provider responses and usage before parsing or validating a decision.
3. Attach available response metadata to classified adapter errors.
4. Measure each attempt with a monotonic clock, including attempts that raise exceptions.
5. Measure decision elapsed time from the first attempt through the final outcome, including retry backoff.
6. Send every completed attempt to the accounting and persistence path exactly once.
7. Store decision results separately from the attempt records that produced them.

The attempt record should include:

| Field | Purpose |
| --- | --- |
| Run, contender, suite, item, repeat, attempt | Identify one request uniquely |
| Start time and elapsed milliseconds | Reconstruct request timing |
| Outcome and error category | Separate schema, transport, authentication, and provider failures |
| Input and output tokens | Preserve reported usage |
| Usage completeness | Distinguish unknown usage from zero usage |
| Provider response | Retain the available response for audit |
| Request configuration or its version | Identify the settings used for this attempt |

Keep credentials and authorization headers out of records. Retain the existing SMS redaction policy when publishing the new record format.

Decision rows must state whether latency covers one request or the complete decision. New rows should use complete decision latency.

Preserve the current maximum of two attempts per decision. Do not introduce separate retry loops that multiply the allowed attempts.

Negotiation calls also consume tokens. Track their usage separately from scored decisions and include known costs in total run spend.

Construct and negotiate only the contenders selected by the command line. Apply filters before making provider calls.

Tests and acceptance:

- A malformed response followed by success retains two responses and both usage records.
- Two malformed responses retain both attempts and one malformed decision result.
- A rate-limit retry includes backoff in decision latency.
- A successful first attempt is accounted for once.
- Unknown usage stays unknown; it does not become a measured zero.
- A parsing error after a valid HTTP response retains that response's usage.
- SMS redaction tests cover the new attempt format and all published result files that can contain error text.

Use a fake clock and controlled transport responses. Avoid tests that depend on exact real-time sleeps.

Historical limitation:

The v1 logs contain 270 malformed attempts. Their discarded responses and usage cannot be recovered from those records. Label affected historical totals as incomplete.

## Phase 3: Correct scheduling and failure handling

### Finding 3: Stop scheduling and collect running requests

The current runner submits the entire cell before processing results. It then abandons completed responses after a budget or deadline stop.

Implementation:

1. Replace full-cell submission with a bounded set of pending requests.
2. Check run, provider, and cell stop signals before each submission and retry.
3. Record attempt usage before deciding whether the budget requires a stop.
4. Set the shared budget stop signal immediately when known spend crosses the limit.
5. Use timed waits so deadlines are detected even when no request completes.
6. Cancel work that has not started, then collect and persist every request already running.
7. Finalize the cell after collecting those requests. Keep the original stop reason.

Use separate stop scopes:

- Run stop: budget exhaustion or an unrecoverable execution failure.
- Provider stop: invalid credentials shared by contenders in that provider.
- Cell stop: the suite deadline.

Define deadline behavior explicitly. Stop new work at the deadline, keep results from requests already running, and mark the cell incomplete.

Bound individual request timeouts. Document that collecting running requests can extend total cell wall time beyond the scheduling deadline.

Record the observed budget overshoot. Never claim an exact spending ceiling when the provider bills concurrent requests after submission.

Persist completed records during the cell. A crash must not discard all progress since the previous cell.

Tests and acceptance:

- The request that crosses the budget appears in both raw records and accounting.
- Responses arriving after cancellation remain recorded and billed usage remains counted.
- No new request or retry starts after a worker observes a stop signal.
- Pending work stays bounded by the configured concurrency.
- A deadline triggers while all current requests are still running.
- A shared budget stop reaches every provider lane.
- Recorded spend equals the sum of all known attempt costs, including drained requests.

### Finding 6: Stop a provider after authentication failure

Implementation:

1. Convert an authentication failure into a recorded terminal attempt.
2. Set the provider stop signal immediately.
3. Cancel requests that have not started and suppress further retries for that provider.
4. Collect successful and failed requests already running.
5. Mark later cells in the provider lane as skipped with the authentication reason.

Tests and acceptance:

- A 20-item cell with invalid credentials does not execute all 20 queued requests.
- The test permits requests that were already running when authentication failed.
- Successful partial results remain available when a later request fails authentication.
- Other provider lanes continue unless a run-wide stop also applies.
- The manifest records every skipped cell and retains the original failure reason.

### Finding 7: Surface unexpected worker failures

Implementation:

1. Collect provider-lane results through futures or another explicit exception channel.
2. Record an unexpected exception as an execution failure with contender and suite context.
3. Stop new work, collect running requests, and finalize available records.
4. Write a terminal failed status to the manifest.
5. Return a nonzero command-line exit code for execution, budget, deadline, and authentication failures.

Keep measured provider rejections separate from runner failures. For example, a recorded option-count rejection is a benchmark outcome.

Tests and acceptance:

- An unexpected adapter `ValueError` cannot produce a successful run status.
- A persistence failure propagates to the caller and stops scheduling.
- An empty failed run still has initialized spend, cell, and status fields.
- Successful lanes and previously persisted records survive another lane's failure.
- Command-line exit behavior is documented and tested.

## Phase 4: Correct scoring and baseline inputs

### Finding 1: Map option indices to stable labels

Macro-averaged F1 measures per-class precision and recall. Option positions are not classes when options move between items.

Implementation:

1. Resolve each valid prediction and gold index through its frozen item's option list.
2. Define stable class labels for S1, S2, and S4.
3. Compute macro-F1 using those labels and a fixed class universe for each suite.
4. Keep absent-class handling explicit and consistent with the documented metric.
5. Mark macro-F1 as not applicable for S3 and S5 unless a meaningful class definition is specified.
6. Validate row references and prediction bounds before aggregation.

S3 code words and S5 alternatives vary between items. Do not treat option positions as shared classes in those suites.

Tests and acceptance:

- Permuting an item's options does not change semantic macro-F1.
- S2's majority baseline scores approximately `0.467140`, rather than the current `0.876358`.
- S4 predictions for one label remain one class across permutations.
- Invalid item references and out-of-range predictions fail with an actionable error.
- Unchanged predictions retain their existing accuracy values.
- Reports explain where macro-F1 is not applicable.

### Finding 11: Prevent majority-prior collisions

S1 and S4 share the same option set. The current lookup lets S4 replace S1's prior.

Use one frozen S1 prior for both S1 and S4. This keeps the baseline constant during the order experiment.

Implementation:

1. Define an explicit source suite for each majority prior.
2. Load the S1 source when S4 is selected, including S4-only runs.
3. Build each prior once and prevent later suites from replacing it.
4. Record prior provenance and source hashes in the manifest.
5. Document that the existing baseline derives its prior from frozen evaluation data.

A separate training-data prior would change the experiment. Treat that change as a future protocol decision.

Tests and acceptance:

- S1-only and full-grid runs select the same majority label and confidence.
- S4-only and full-grid runs use the same S1 prior.
- Suite ordering does not affect the prior.
- Option permutations change only the returned index, not the chosen label.
- Historical baseline outputs are not silently rewritten as corrected baseline outputs.

## Phase 5: Verify and combine runs consistently

### Finding 9: Verify every manifest

Implementation:

1. Verify item-file hashes for every supplied run.
2. Build the union of verified suites across those runs.
3. Reject conflicting hashes for the same suite.
4. Validate each row's suite and item against the verified data.
5. Validate protocol and record-format compatibility before combining runs.
6. Load a stored price snapshot for new runs. For older runs, require the available price table to match the recorded hash.

The price snapshot closes a related reproducibility gap. A current price table must not silently change an old report's costs.

Tests and acceptance:

- An invalid hash in an extra run causes report generation to fail.
- A suite present only in an extra run receives full item validation.
- Conflicting item files produce a message naming both runs and the suite.
- Incompatible prompt or latency definitions cannot be merged without an explicit, visible policy.
- Historical reports reject mismatched price tables instead of silently repricing results.

### Finding 10: Apply replacement semantics once

The existing documentation says later runs replace earlier cells. Keep that behavior and implement it consistently.

Implementation:

1. Group each run's rows by contender and suite.
2. Select the last supplied run for each cell, using manifest cells as well as rows.
3. Replace the whole cell; do not append overlapping rows.
4. Feed the selected rows to tables, cardinality plots, and reliability plots.
5. Record each selected cell's source run in the report summary.
6. Distinguish total spend across source runs from spend associated with selected cells.

A later failed cell with zero rows still replaces an earlier cell. Otherwise the report silently selects the earlier success.

Tests and acceptance:

- Adding the same run twice does not double rows, plotted observations, or total source-run spend.
- A replacement cell with different predictions uses only the later predictions.
- Unrelated earlier cells remain present.
- A zero-row failed replacement is visible and does not expose an earlier score.
- Tables and plots use identical selected observations.
- Duplicate item-repeat keys inside one cell are rejected.

## Phase 6: Show computed values and completion status

### Finding 8: Use one metric field mapping

Implementation:

1. Choose one report representation: object attributes or `CellMetrics.as_dict()`.
2. Make every table specification use keys from that representation.
3. Replace missing-attribute fallbacks with explicit validation for configured metric keys.
4. Keep valid zero values distinct from unavailable values.

Tests and acceptance:

- A nonzero calculated cost appears in both Markdown and HTML.
- A free baseline shows a zero cost, not a missing value.
- A known S4 confidence range appears in both formats.
- Machine-readable and visible report values agree before display rounding.
- An unknown configured metric key fails a test instead of rendering a dash.

### Finding 12: Report incomplete coverage

Implementation:

1. Join aggregate metrics with the selected manifest cell.
2. Include status, stop reason, expected decisions, completed decisions, valid decisions, malformed decisions, and failed decisions.
3. Display a coverage table in Markdown and HTML.
4. Mark partial scores when a cell stops early, even if every completed decision is valid.
5. Include malformed decisions when identifying reduced valid coverage.
6. Include empty failed or skipped cells in report ordering and machine-readable output.

Define denominators explicitly:

- Accuracy and calibration use the documented valid, scored decisions.
- Completion coverage uses completed decisions divided by expected decisions.
- Valid coverage uses valid decisions divided by expected decisions.
- Malformed and failure rates state whether the denominator is completed or expected decisions.
- Cost states which decisions and attempt charges it includes.

Show incomplete usage accounting separately from decision coverage. Unknown cost must not look like a measured zero.

Tests and acceptance:

- A deadline-stopped cell with one correct result out of 100 expected results displays 1% completion coverage.
- That cell retains its conditional accuracy but receives a clear partial-result marker.
- A fully attempted cell with provider rejections shows complete execution and reduced valid coverage.
- A cell with more than 5% malformed decisions receives the same coverage treatment as other invalid decisions.
- Empty failed and skipped cells appear with reasons rather than disappearing.

## Phase 7: Align confidence instructions

### Finding 5: Make the comparison explicit

The language-model prompt requests low confidence under uncertainty. Jev's question instructions omit that requirement.

Implementation:

1. Extract shared semantic instructions for choice selection and uncertainty.
2. Use those instructions in both the language-model renderer and the jev question.
3. Keep provider-specific formatting separate from shared semantic instructions.
4. Verify the provider's documented meaning of native confidence before treating it as probability of correctness.
5. Record prompt hashes, confidence definitions, and a new protocol version in manifests.
6. Update `SPEC.md`, renderer tests, and adapter request tests.

Do not assume native confidence is controlled by question text. If the provider defines another quantity, label it separately.

Tests and acceptance:

- Request fixtures show equivalent uncertainty instructions for every contender.
- Provider-specific output formatting does not remove the shared instructions.
- A prompt change produces a different recorded prompt hash.
- Reports identify historical runs that used unequal instructions.
- Comparative S5 conclusions require either a compatible confidence definition or a clearly stated limitation.

Local tests verify requests and metadata. They cannot verify model behavior. A new provider run is a separate paid execution step.

## Phase 8: Correct published artifacts

### Recovery limits

| Finding | Recoverable from existing records? | Publication action |
| --- | --- | --- |
| Macro-F1 labels | Yes, using matching frozen items | Recompute and publish corrected values |
| Missing cost and confidence-range table values | Yes, within existing accounting limits | Regenerate tables and label incomplete costs |
| Retry usage and elapsed time | Generally no | Disclose missing measurements; use a new run for complete values |
| Responses discarded after abort | No, unless another log retained them | Mark affected runs incomplete |
| Unequal confidence instructions | No | Qualify conclusions and use a new protocol for future comparisons |
| Majority-prior collision | Recorded outputs remain historical facts | Identify the old prior; run the corrected baseline separately |
| Run replacement and hash validation | Yes, when source data remains available | Regenerate after validation |
| Coverage status | Yes, when manifests retain counts and reasons | Publish coverage tables |

Publication steps:

1. Preserve the original reports, manifests, and archives under their existing version identities.
2. Generate corrected artifacts under a distinct correction version or directory.
3. Include a correction note mapping changed metrics to the reviewed defects.
4. Revise `blog/v1-post.md` so every retained claim matches corrected results and stated limitations.
5. Update `README.md` with run-ID behavior, merge semantics, exit statuses, and correction commands.
6. Record any measurements that need a new paid run without claiming they have been repaired.

Do not mix new measurements into an old report without recording their source runs and protocol differences.

## Verification strategy

Use focused regression tests during each phase. Run the full suite once the integrated changes are ready.

| Test area | Main cases |
| --- | --- |
| Attempt recording | Malformed responses, usage extraction, retry latency, unknown usage |
| Scheduling | Bounded concurrency, shared budget stop, deadlines, draining running work |
| Failure handling | Authentication stop, unexpected exceptions, persistence failures, exit codes |
| Run safety | Existing IDs, concurrent reservation, initial and terminal manifests |
| Metrics | Stable labels, permutation invariance, fixed class universes |
| Baselines | Stable S1 prior across suite selection and order |
| Merging | Every hash verified, whole-cell replacement, consistent tables and plots |
| Rendering | Cost values, confidence ranges, coverage, failed and skipped cells |
| Protocol | Shared semantic instructions, prompt hashes, confidence definitions |
| Historical artifacts | Recoverable values, explicit missing data, preservation of originals |

Final local checks:

1. Run `uv run pytest -q`.
2. Run `uv run ruff check .`.
3. Execute a temporary mock grid containing success, retry, rejection, and stop scenarios.
4. Generate Markdown, HTML, plots, machine-readable metrics, and an archive from that grid.
5. Reconcile attempt counts, decision counts, usage totals, selected cells, and manifest status.
6. Verify that original runs and published archives remain unchanged.

Use mocked HTTP responses for adapter integration tests. Never depend on provider availability or spend for routine verification.

## Completion criteria

- Each of the 12 findings has an implementation change and a meaningful regression test.
- Every known attempt charge is counted once and remains traceable to a record.
- Stopped or failed runs cannot appear complete.
- Existing runs cannot be overwritten by an ordinary run command.
- Reports verify all source data and apply one consistent cell-selection policy.
- Metrics use stable labels where classes exist.
- Report values, coverage, and machine-readable output agree.
- Historical corrections distinguish recoverable data from missing measurements.
- Prompt differences and confidence definitions are visible in the protocol.
- All local checks pass without paid provider calls.
