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
        return Decision(choice_index=choice, confidence=1.0 / len(options))


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
        if entry is None:
            return Decision(choice_index=0, confidence=1.0 / len(options))
        option, prior = entry
        if option in options:
            return Decision(choice_index=options.index(option), confidence=prior)
        return Decision(choice_index=0, confidence=1.0 / len(options))


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
            return Decision(choice_index=0, confidence=0.1)
        best = max(range(len(options)), key=lambda i: scores[i])
        confidence = min(0.99, max(0.1, scores[best] / total))
        return Decision(choice_index=best, confidence=confidence)


def build_majority_table(
    suite_items: dict[str, list[Any]],
) -> dict[frozenset[str], tuple[str, float]]:
    """Build the majority lookup from frozen suite files.

    Each suite contributes one entry: (most frequent gold option string,
    its share of items). Suites whose items do not share one option list
    (S3, S5) are skipped - the baseline falls back to a fixed-index guess.
    """
    table: dict[frozenset[str], tuple[str, float]] = {}
    for items in suite_items.values():
        if not items:
            continue
        option_sets = {frozenset(item.options) for item in items}
        if len(option_sets) != 1:
            continue
        counts = Counter(item.options[item.gold_index] for item in items if item.gold_index >= 0)
        if not counts:
            continue
        option, count = counts.most_common(1)[0]
        table[option_sets.pop()] = (option, count / len(items))
    return table
