"""Baseline behavior tests."""

from dmb.contenders.baselines import (
    KeywordBaseline,
    MajorityBaseline,
    RandomBaseline,
    build_majority_table,
)
from dmb.suites.items import DecisionItem


def _items_for_majority() -> list[DecisionItem]:
    options = ["ham", "spam"]
    items = []
    for i in range(10):
        items.append(
            DecisionItem(
                item_id=f"x-{i}",
                suite="t",
                state="msg",
                options=options,
                gold_index=0 if i < 8 else 1,  # ham prior 0.8
            )
        )
    return items


def test_majority_table_uses_class_prior():
    table = build_majority_table({"s2": _items_for_majority()})
    assert table[frozenset({"ham", "spam"})] == ("ham", 0.8)


def test_majority_picks_prior_option_stably():
    table = build_majority_table({"s2": _items_for_majority()})
    contender = MajorityBaseline(table)
    decision = contender.decide("any state", ["spam", "ham"])  # permuted order
    assert decision.choice_index == 1
    assert decision.confidence == 0.8


def test_majority_falls_back_without_prior():
    contender = MajorityBaseline({})
    decision = contender.decide("state", ["a", "b", "c", "d"])
    assert decision.choice_index == 0
    assert decision.confidence == 0.25


def test_random_is_deterministic_and_honest():
    contender = RandomBaseline()
    first = contender.decide("state", ["a", "b", "c"])
    second = contender.decide("state", ["a", "b", "c"])
    assert first.choice_index == second.choice_index
    assert first.confidence == 1 / 3
    permuted = contender.decide("state", ["c", "b", "a"])
    assert permuted.choice_index in (0, 1, 2)


def test_keyword_matches_state_words():
    contender = KeywordBaseline()
    decision = contender.decide(
        "I still have not received my card", ["card payment", "transfer timing"]
    )
    assert decision.choice_index == 0  # "card" overlaps option 0


def test_keyword_zero_overlap_picks_first_with_low_confidence():
    contender = KeywordBaseline()
    decision = contender.decide("nothing relevant here", ["alpha", "beta"])
    assert decision.choice_index == 0
    assert decision.confidence == 0.1
