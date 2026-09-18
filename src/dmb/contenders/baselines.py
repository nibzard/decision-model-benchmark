"""Deterministic baselines: random, majority, keyword.

- ``random``: uniform guess. Confidence is exactly 1/N - a random guesser
  knows its own accuracy - so the floor is calibrated by construction.
  The draw is seeded from the state plus the options *in order*, so it is
  stable across repeats and reshuffles under option permutation.
- ``majority``: the class-prior argmax. Looks up the majority option by
  the option *set* (permutation-stable) from a table built out of the
  frozen suite files at construction time. When no prior applies (S3, S5)
  it falls back to a fixed-index guess with confidence 1/N.
- ``keyword``: token-overlap heuristic. Scores each option by the number
  of state words it contains; picks the argmax, confidence is the best
  option's share of all positive scores. The "do you even need a model"
  control.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Any

from .base import Contender, Decision

_TOKEN_RE = re.compile(r"[a-z0-9']+")


class RandomBaseline(Contender):
    """Uniform random floor with honest 1/N confidence."""

    def __init__(self, name: str = "baseline:random") -> None:
        self.name = name
        self.provider = "baseline"

    def _decide(self, state: str, options: list[str]) -> Decision:
        payload = (state + "\x00" + "\x00".join(options)).encode("utf-8")
        seed = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
        # A cheap deterministic stand-in for random.Random(seed).randrange.
        choice = seed % len(options)
        return Decision(
            choice_index=choice,
            confidence=1.0 / len(options),
            input_tokens=0,
            output_tokens=0,
        )


class MajorityBaseline(Contender):
    """Class-prior argmax, permutation-stable via the option set."""

    def __init__(
        self,
        majority: dict[frozenset[str], tuple[str, float]],
        name: str = "baseline:majority",
    ) -> None:
        """``majority`` maps an option set to (majority option, prior)."""
        self.name = name
        self.provider = "baseline"
        self._majority = majority

    def _decide(self, state: str, options: list[str]) -> Decision:
        entry = self._majority.get(frozenset(options))
        zero = {"input_tokens": 0, "output_tokens": 0}
        if entry is None:
            return Decision(choice_index=0, confidence=1.0 / len(options), **zero)
        option, prior = entry
        if option in options:
            return Decision(
                choice_index=options.index(option), confidence=prior, **zero
            )
        return Decision(choice_index=0, confidence=1.0 / len(options), **zero)


class KeywordBaseline(Contender):
    """Token-overlap heuristic."""

    def __init__(self, name: str = "baseline:keyword") -> None:
        self.name = name
        self.provider = "baseline"

    def _decide(self, state: str, options: list[str]) -> Decision:
        state_tokens = set(_TOKEN_RE.findall(state.lower()))
        scores = [
            len(state_tokens & set(_TOKEN_RE.findall(option.lower())))
            for option in options
        ]
        total = sum(scores)
        if total == 0:
            return Decision(
                choice_index=0, confidence=0.1, input_tokens=0, output_tokens=0
            )
        best = max(range(len(options)), key=lambda i: scores[i])
        confidence = min(0.99, max(0.1, scores[best] / total))
        return Decision(
            choice_index=best, confidence=confidence, input_tokens=0, output_tokens=0
        )


# Which frozen suite defines each suite's majority prior. S4 (permuted S1
# items) uses the S1 prior so the order experiment holds the prior fixed;
# the old lookup let S4 replace S1's prior when both suites loaded.
PRIOR_SOURCES: dict[str, str] = {
    "s1_intent77": "s1_intent77",
    "s2_spam": "s2_spam",
    "s4_order": "s1_intent77",
}


def build_majority_table(
    suite_items: dict[str, list[Any]],
    prior_sources: dict[str, str] | None = None,
    source_hashes: dict[str, str] | None = None,
) -> tuple[dict[frozenset[str], tuple[str, float]], dict[str, dict]]:
    """Build the majority lookup from frozen suite files.

    ``prior_sources`` maps each suite to the suite whose frozen items
    define its prior (see ``PRIOR_SOURCES``). ``suite_items`` must contain
    the source suites' items, including when the source itself is not
    being run - an S4-only run still loads the S1 source. Suites whose
    items do not share one option list (S3, S5) get no prior; the baseline
    falls back to a fixed-index guess.

    Returns ``(table, provenance)``. ``table`` maps an option set to
    ``(majority option, prior)``. ``provenance`` maps each suite to its
    source suite, chosen option, prior, and the source file hash; it goes
    into the run manifest.

    The priors derive from frozen evaluation data, not from a held-out
    training split. That choice predates this function; changing it is a
    protocol decision, not a bug fix.
    """
    sources = prior_sources or dict(PRIOR_SOURCES)
    hashes = source_hashes or {}

    # Build each source suite's prior exactly once, in sorted order, so
    # suite load order cannot change the result.
    priors: dict[str, tuple[str, float]] = {}
    for source_suite in sorted(set(sources.values())):
        items = suite_items.get(source_suite)
        if not items:
            continue
        counts = Counter(
            item.options[item.gold_index]
            for item in items
            if item.gold_index >= 0
        )
        if not counts:
            continue
        option, count = counts.most_common(1)[0]
        priors[source_suite] = (option, count / len(items))

    table: dict[frozenset[str], tuple[str, float]] = {}
    provenance: dict[str, dict] = {}
    for target_suite in sorted(sources):
        source_suite = sources[target_suite]
        prior = priors.get(source_suite)
        if prior is None:
            continue
        option_sets = {frozenset(item.options) for item in suite_items.get(target_suite, [])}
        if len(option_sets) != 1:
            continue
        key = next(iter(option_sets))
        if key in table and table[key] != prior:
            raise ValueError(
                "two prior sources define different priors for one option set "
                f"({prior!r} vs {table[key]!r}); "
                "prior_sources must give one source per option set"
            )
        table[key] = prior
        provenance[target_suite] = {
            "source_suite": source_suite,
            "option": prior[0],
            "prior": round(prior[1], 6),
            "source_sha256": hashes.get(source_suite),
        }
    return table, provenance
