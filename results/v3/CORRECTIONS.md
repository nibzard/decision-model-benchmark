# v3 correction notice — 2026-09-26

v3 recomputes reports from the original `v1`, `v1.1`, and
`v2-majority-fix` observations. It makes no new paid measurements. All
five rebuilt suite hashes match the historical manifests. Earlier numeric
reports retain their original values with correction links. All published
raw archives have been sanitized in place.

| Correction | Historical defect | v3 treatment |
|---|---|---|
| SMS archive privacy | Reasoning and arbitrary response/error strings escaped key-based redaction; shared logs bypassed filename checks. | Publish approved S2 metadata only across every archive member. Preserve numeric decision observations. |
| Cost scope | Retry attempts were incompletely recorded; original snapshots lack cached-token prices; gateway invoices and jev's vendor price were unverified. | Verify exact historical snapshots and retain missing rates. Show known subtotals, missing components and nominal historical calculations separately. No current cache rate is applied to old runs. |
| Protocol provenance | Reports ignored top-level protocol versions and could miss mixed execution rules. | Validate protocol versions and fingerprints; explicitly mark the v1/v2 mix. v3 is a report version, not a relabeling of historical executions. |
| S4 stability | All three orders and three repeats were pooled as “position bias.” | Separate within-order repeat disagreement and matched across-order disagreement with counts. Keep pooled instability as a descriptive statistic; order comparisons still contain stochastic variation. |
| S5 confidence | ECE excluded no-good items yet was cited as evidence about them. Prompt instructions differed and jev's native score was treated as a correctness probability. | Report no-good confidence separately from underdetermined correctness calibration. Qualify native scores and historical prompt differences; withdraw the “only jev bluffs” verdict. |
| Headline arithmetic | “Ten times cheaper” and “both GLMs lead spam” contradicted the numbers. | Original nominal nano/jev S1 ratio is 2.486×, about 2.49×. Spam ranks glm-5.3 94.9%, jev 93.0%, flash 91.4%. These costs are not reconciled bills. |
| Uncertainty and threshold use | Repeats could imply independent evidence and confidence plots suggested deployment readiness. | Cluster uncertainty by item (S4 base item) and show descriptive risk/coverage. Select deployment thresholds on separate held-out data. |
| Input provenance and licenses | Mutable Banking77 master, unchecked cached bytes, S4 incorrectly called wholly synthetic, inaccurate SMS license prohibition. | Pin full Banking77 commit and CSV/license hashes, SMS ZIP/member hashes, and verify cache bytes. Retain Banking77 attribution on S4. Current UCI page lists CC BY 4.0; DMB keeps SMS text local as its privacy policy. |

The original pooled S4 changes on 13/100 jev and 37/100 mini base items.
Identical-order repeats also change on 5/100 and 25/100, respectively.
These figures illustrate why the old pooled result cannot identify causal
position bias. Corrected matched denominators and descriptive comparisons
are in the generated report and machine-readable metrics.

S5 no-good-option items have no correct answer. Underdetermined items have
randomly planted reference answers that the state does not reveal. The
historical jev ECE of 0.246 covers only the latter subset. Low-confidence
fractions describe behavior under the historical prompt; they do not prove
honesty or establish a deployable calibrated threshold.

The script `scripts/regenerate_corrections.py` verifies source and price
hashes, captures numerical observations, regenerates v3 and all sanitized
archives, and checks preserved observations before replacement. It never
reads credentials or calls model providers. Source datasets remain local;
public source provenance consists of pinned identifiers and checksums.
