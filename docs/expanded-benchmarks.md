# Expanded decision benchmarks

Build the optional suites with:

```bash
uv run dmb build --expanded
```

This downloads pinned public dataset files and verifies their byte hashes,
including cached files. It writes six new files under `data/suites`, plus
`expanded-hashes.json` and `data/EXPANDED-LICENSES.md`. It makes no model calls.
The original S1–S5 files and their published hashes stay unchanged. The default
`dmb build` and default run suite selection still use S1–S5.

## Datasets and splits

| Family | Validation | Test | Decision |
|---|---|---|---|
| `s6_banking77` | 770 messages: ten per intent, sampled from official training data | All 3,080 official test messages | Select one of 77 intent labels |
| `s7_clinc150` | 3,100 official validation queries, including 100 outside the supported intents | 5,500 official test queries, including 1,000 outside the supported intents | Select one of 150 intents or the explicit out-of-scope option |
| `s8_nlupp` | 310 messages, 14,064 binary decisions; folds 16–17 | 302 messages, 13,712 binary decisions; folds 18–19 | Answer each applicable intent question with yes or no |

Append `_validation` or `_test` to a family name to select its suite.

Banking77 test records retain their official order and labels. Its validation
sample excludes normalized test texts and duplicate training texts before
sampling. CLINC150 and NLU++ remove validation messages also present in test.
Normalization ignores letter case and repeated whitespace. The same NLU++
message never contributes label questions to both splits.

NLU++ includes banking and hotel messages. Each message receives every intent
question from its domain, including the shared general intents. Question
wording comes from the pinned ontology. Labels and slots are never included in
the model input. An unannotated intent is a negative label. Slot extraction is
outside this suite.

NLU++ uses the existing shared Choice interface: one binary request per intent.
It does not measure native Noul probabilities or multiple questions in a single
request. Each request has its own latency and cost. Its fixed folds and binary
formulation are an adaptation, not a reproduction of published NLU++ scores.

The benchmark does not train the evaluated models. These splits keep threshold
selection separate from test scoring; they do not establish which public texts
an external model encountered during training.

## Metrics

The usual accuracy, failures, coverage, cost, latency and confidence diagnostics
remain available. Expanded reports also include:

- Correct decisions divided by all requested decisions. Failed, malformed, and
  unstarted decisions contribute no correct answer.
- CLINC150 out-of-scope precision and recall, plus success on supported intents.
  Recall includes every requested out-of-scope query in its denominator.
- NLU++ micro and macro intent F1. Positive labels are the target; missing
  positive answers count as missed positives. Macro F1 gives every intent equal
  weight and counts absent intents as zero.
- NLU++ complete-message accuracy and coverage. A message is correct only when
  every applicable intent receives a valid, correct answer. A partial label
  group cannot count as a correct complete message.

NLU++ binary accuracy can be high for a model that always answers no. Compare
intent F1 and complete-message accuracy as well. Confidence intervals group
NLU++ questions by source message rather than treating each label as an
independent example.

Expanded majority baselines use validation labels for their priors, including
when running test suites. S1's historical majority prior follows its original
protocol. If combining S1 and S6 creates different priors for the same option
set, the existing prior conflict check rejects the run; run those suites
separately.

## Local smoke run

This example runs only a deterministic local baseline. It makes no paid calls.
A limit of 96 covers two complete banking messages in the NLU++ suite.

```bash
uv run dmb run --run-id expanded-smoke \
  --suites s6_banking77_test,s7_clinc150_test,s8_nlupp_test \
  --contenders baseline:random --repeats 1 --item-limit 96 --no-negotiate
uv run dmb report runs/expanded-smoke --out results/expanded-smoke
```

Use a new run ID each time. An item limit counts binary decisions for NLU++,
not messages. `--smoke` uses 50 decisions and can cut a label group. Reports
mark that group incomplete; threshold analysis rejects such a group.

## Fit and evaluate a threshold

The following model runs require credentials and make paid requests. Each run
has its own $5 cap. A stopped validation run cannot fit a threshold.

1. Run the validation suites with one repeat:

   ```bash
   uv run dmb run --run-id expanded-validation \
     --suites s6_banking77_validation,s7_clinc150_validation,s8_nlupp_validation \
     --contenders typesafe:jev --repeats 1 --hard-cap 5
   ```

2. Fit thresholds using validation results only:

   ```bash
   uv run dmb fit-thresholds runs/expanded-validation \
     --max-error 0.05 --min-accepted 100 \
     --out results/expanded-thresholds.json
   ```

3. Run the test suites with the same contender settings:

   ```bash
   uv run dmb run --run-id expanded-test \
     --suites s6_banking77_test,s7_clinc150_test,s8_nlupp_test \
     --contenders typesafe:jev --repeats 1 --hard-cap 5
   ```

4. Apply the frozen thresholds to test results:

   ```bash
   uv run dmb evaluate-thresholds runs/expanded-test \
     --thresholds results/expanded-thresholds.json \
     --out results/expanded-threshold-evaluation.json
   ```

5. Generate the standard test report:

   ```bash
   uv run dmb report runs/expanded-test --out results/expanded-test
   ```

Threshold fitting selects the largest observed validation coverage whose error
rate meets the target and whose accepted count reaches the minimum. Equal
scores stay together. If no threshold qualifies, the frozen threshold is null
and the test evaluation accepts no decisions. Output files cannot overwrite
existing files.

For Banking77 and CLINC150, the unit is one decision. For NLU++, the unit is
one entire message. Its score is the minimum of all intent confidence scores;
its outcome is correct only when all intent answers are correct. Failed or
missing answers remain in coverage denominators. Threshold fitting requires
all validation decisions to finish, but completed failures are retained.

The threshold file records validation item hashes, source text hashes, model
configuration and protocol. Evaluation checks those settings and rejects
validation/test text overlap. Neither command changes thresholds using test
outcomes. Test error can exceed the validation target: empirical selection is
not a statistical error guarantee. Jev's native confidence is treated as a
ranking score, not as a probability of correctness.

## Sources and licenses

- Banking77: Casanueva et al. (2020), PolyAI, CC BY 4.0.
  https://github.com/PolyAI-LDN/task-specific-datasets
- CLINC150: Larson et al. (2019), CC BY 3.0.
  https://github.com/clinc/oos-eval
- NLU++: Casanueva et al. (2022), PolyAI, CC BY 4.0.
  https://github.com/PolyAI-LDN/task-specific-datasets/tree/master/nlupp

`src/dmb/suites/expanded_sources.json` pins repository commits and SHA-256
hashes. Builds cache the upstream license files with the source data.
