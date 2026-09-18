"""S3 cardinality: planted-answer sweep over option-list size.

Synthetic, seeded. Each item plants exactly one correct code word in a
short state text among N distractor code words. N sweeps
{2, 8, 32, 64, 128, 192, 254, 255, 256, 384, 512}, 25 items per step,
alternating difficulty tiers:

- ``easy``: distractors share no first letter with the planted word
  (dissimilar).
- ``hard``: distractors come from the same first-syllable cluster as the
  planted word, then single-character mutants of it, then the same
  first-letter pool (similar).

The vocabulary is generated deterministically from syllables, so the same
seed produces byte-identical items. This suite measures how accuracy and
latency scale with cardinality - including the 255 boundary the jev
claims talk about.
"""

from __future__ import annotations

import random
from itertools import product

from .items import DMB_SEED, DecisionItem

SUITE_ID = "s3-cardinality"
N_SWEEP = [2, 8, 32, 64, 128, 192, 254, 255, 256, 384, 512]
ITEMS_PER_STEP = 25
VOCAB_SIZE = 2048

ONSETS = list("bcdfghjklmnprstvz")
NUCLEI = list("aeiou")
CODAS = ["", "n", "r", "s", "t"]

_TEMPLATES = [
    'Gate {k} opens only for the access code "{w}". Which code is authorized?',
    'Package {k} is held at the depot under hold token "{w}". Select the matching token.',
    'The manifest for order {k} lists verification word "{w}". Choose the correct word.',
]

_ALPHABET = "abcdefghijklmnopqrstuvwxyz"


def build_vocab() -> list[str]:
    """Deterministic pronounceable code words, spread over all onsets.

    The full syllable cross-product is 36k+ words; an even-stride sample
    keeps the vocabulary spread across every first letter instead of
    exhausting one letter first.
    """
    full: list[str] = []
    for onset1, nucleus1, coda1 in product(ONSETS, NUCLEI, CODAS):
        for onset2, nucleus2 in product(ONSETS, NUCLEI):
            full.append(onset1 + nucleus1 + coda1 + onset2 + nucleus2)
    step = max(1, len(full) // VOCAB_SIZE)
    return full[::step][:VOCAB_SIZE]


def _mutants(word: str, exclude: set[str]) -> list[str]:
    """Single-character substitutions of ``word``, deterministic order."""
    out = []
    for position in range(len(word)):
        for letter in _ALPHABET:
            if letter == word[position]:
                continue
            candidate = word[:position] + letter + word[position + 1 :]
            if candidate not in exclude:
                out.append(candidate)
    return out


def _pick_planted(rng: random.Random, vocab: list[str]) -> str:
    return rng.choice(vocab)


def _easy_distractors(rng: random.Random, vocab: list[str], planted: str, count: int) -> list[str]:
    """Distractors with a different first letter than the planted word."""
    pool = [w for w in vocab if w[0] != planted[0]]
    rng.shuffle(pool)
    return pool[:count]


def _hard_distractors(rng: random.Random, vocab: list[str], planted: str, count: int) -> list[str]:
    """Distractors as close to the planted word as the vocabulary allows.

    Fills from the same first-syllable cluster, then single-character
    mutants, then the same first letter, then any remaining word. Similar
    degrades gracefully as the close pools run out at large N.
    """
    first_syllable = planted[:3]
    cluster = [w for w in vocab if w[:3] == first_syllable and w != planted]
    rng.shuffle(cluster)
    chosen = cluster[:count]
    taken = {planted, *chosen}
    if len(chosen) < count:
        mutant_pool = [m for m in _mutants(planted, taken) if m not in taken]
        rng.shuffle(mutant_pool)
        chosen += mutant_pool[: count - len(chosen)]
        taken.update(chosen)
    if len(chosen) < count:
        same_letter = [w for w in vocab if w[0] == planted[0] and w not in taken]
        rng.shuffle(same_letter)
        chosen += same_letter[: count - len(chosen)]
        taken.update(chosen)
    if len(chosen) < count:
        rest = [w for w in vocab if w not in taken]
        rng.shuffle(rest)
        chosen += rest[: count - len(chosen)]
    return chosen


def build_items(vocab: list[str] | None = None) -> list[DecisionItem]:
    """Generate the full sweep: 25 items per N, tiers alternating."""
    vocab = vocab or build_vocab()
    items: list[DecisionItem] = []
    for n in N_SWEEP:
        for i in range(ITEMS_PER_STEP):
            rng = random.Random(f"{DMB_SEED}:s3:{n}:{i}")
            tier = "easy" if i % 2 == 0 else "hard"
            planted = _pick_planted(rng, vocab)
            template = rng.choice(_TEMPLATES)
            state = template.format(k=rng.randrange(1000, 9999), w=planted)
            if tier == "easy":
                distractors = _easy_distractors(rng, vocab, planted, n - 1)
            else:
                distractors = _hard_distractors(rng, vocab, planted, n - 1)
            options = distractors + [planted]
            rng.shuffle(options)
            items.append(
                DecisionItem(
                    item_id=f"s3-n{n}-{i + 1:02d}",
                    suite=SUITE_ID,
                    state=state,
                    options=options,
                    gold_index=options.index(planted),
                    kind="cardinality",
                    meta={"N": n, "tier": tier},
                )
            )
    return items
