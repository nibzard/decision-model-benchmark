"""Baseline behavior tests, including prior provenance (finding 11)."""

import pytest

from dmb.contenders.baselines import (
    PRIOR_SOURCES,
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


def _permuted_s4_items() -> list[DecisionItem]:
    """S4-style items: same option set as the majority items, permuted."""
    return [
        DecisionItem(
            item_id=f"p-{i}",
            suite="s4_order",
            state="msg",
            options=["spam", "ham"] if i % 2 else ["ham", "spam"],
            gold_index=i % 2,
            base_item_id=f"x-{i}",
            meta={"perm": [0, 1]},
        )
        for i in range(4)
    ]


def test_majority_table_uses_class_prior():
    table, provenance = build_majority_table({"s2_spam": _items_for_majority()})
    assert table[frozenset({"ham", "spam"})] == ("ham", 0.8)


def test_majority_picks_prior_option_stably():
    table, _provenance = build_majority_table({"s2_spam": _items_for_majority()})
    contender = MajorityBaseline(table)
    decision = contender.decide("any state", ["spam", "ham"])  # permuted order
    assert decision.choice_index == 1
    assert decision.confidence == 0.8


def test_majority_falls_back_without_prior():
    contender = MajorityBaseline({})
    decision = contender.decide("state", ["a", "b", "c", "d"])
    assert decision.choice_index == 0
    assert decision.confidence == 0.25


def test_majority_baselines_report_measured_zero_usage():
    table, _provenance = build_majority_table({"s2_spam": _items_for_majority()})
    for contender in (
        RandomBaseline(), MajorityBaseline(table), KeywordBaseline(),
    ):
        decision = contender.decide("state", ["a", "b"])
        assert decision.input_tokens == 0
        assert decision.output_tokens == 0


def test_s4_prior_is_the_frozen_s1_prior():
    """Finding 11: S4 must use the S1 prior even when both suites load.

    The old table build let the later-loaded suite overwrite an option
    set's entry, so S1 and S4 collided and one of them silently used the
    other's prior. The frozen mapping resolves the collision by
    construction: both target the S1 source.
    """
    majority = _items_for_majority()
    s4 = _permuted_s4_items()
    table, provenance = build_majority_table(
        {"s1_intent77": majority, "s4_order": s4, "s2_spam": majority},
        source_hashes={"s1_intent77": "hash-s1", "s2": "hash-s2"},
    )
    entry = table[frozenset({"ham", "spam"})]
    assert entry == ("ham", 0.8)
    assert provenance["s4_order"]["source_suite"] == "s1_intent77"
    assert provenance["s4_order"]["source_sha256"] == "hash-s1"
    assert provenance["s1_intent77"]["source_suite"] == "s1_intent77"
    assert provenance["s2_spam"]["source_suite"] == "s2_spam"


def test_conflicting_priors_for_one_option_set_rejected():
    """Two sources claiming one option set with different priors fail."""
    other = [
        DecisionItem(
            item_id=f"y-{i}", suite="u", state="m",
            options=["ham", "spam"], gold_index=1 if i < 9 else 0,  # spam 0.9
        )
        for i in range(10)
    ]
    with pytest.raises(ValueError, match="two prior sources"):
        build_majority_table(
            {"s2": _items_for_majority(), "s4_order": other},
            prior_sources={"s2": "s2", "s4_order": "s4_order"},
        )


def test_prior_sources_mapping_is_explicit_for_every_label_suite():
    assert set(PRIOR_SOURCES) == {"s1_intent77", "s2_spam", "s4_order"}
    assert PRIOR_SOURCES["s4_order"] == "s1_intent77"


def test_table_independent_of_suite_load_order():
    """The build order cannot change the table (sorted source order)."""
    majority = _items_for_majority()
    s4 = _permuted_s4_items()
    first, _ = build_majority_table(
        {"s1_intent77": majority, "s4_order": s4, "s2": majority}
    )
    second, _ = build_majority_table(
        {"s1_intent77": majority, "s2_spam": majority, "s4_order": s4}
    )
    assert first == second


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
