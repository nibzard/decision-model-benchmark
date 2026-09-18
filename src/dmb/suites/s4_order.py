"""S4 order-stability: S1 spare items with permuted option order.

The 100 spare S1 items (``meta.split == "spare"``) rerun with 3 seeded
permutations of the 77 intent labels. Gold labels map through the
permutation: for permutation ``p`` (new position -> old position),
``new_gold = p.index(old_gold)``. The permutation itself is stored in
``meta.perm`` so any choice can be mapped back to the original option.

This suite measures flip rate (same item, different option positions,
different choice) and confidence drift. Items must be built after S1.
"""

from __future__ import annotations

import random

from .items import DMB_SEED, DecisionItem

SUITE_ID = "s4-order-stability"
PERMUTATIONS = 3


def permute_item(base: DecisionItem, permutation_index: int) -> DecisionItem:
    """One S1 item under one seeded permutation of its options."""
    rng = random.Random(f"{DMB_SEED}:s4:{base.item_id}:p{permutation_index}")
    old_order = list(range(len(base.options)))
    rng.shuffle(old_order)  # old_order[new_index] = old_index
    options = [base.options[old_index] for old_index in old_order]
    gold_index = old_order.index(base.gold_index)
    return DecisionItem(
        item_id=f"{base.item_id}-p{permutation_index + 1}",
        suite=SUITE_ID,
        state=base.state,
        options=options,
        gold_index=gold_index,
        kind="order",
        meta={
            "perm": old_order,
            "base_label": base.meta.get("label"),
        },
        base_item_id=base.item_id,
        permutation=permutation_index,
    )


def build_items(s1_items: list[DecisionItem]) -> list[DecisionItem]:
    """Permute each spare S1 item ``PERMUTATIONS`` times."""
    spare = [item for item in s1_items if item.meta.get("split") == "spare"]
    if len(spare) != 100:
        raise ValueError(f"expected 100 spare S1 items, found {len(spare)}")
    items: list[DecisionItem] = []
    for base in spare:
        for permutation_index in range(PERMUTATIONS):
            items.append(permute_item(base, permutation_index))
    return items
