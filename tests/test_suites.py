"""Suite generator tests (SPEC.md T1.3, T1.4, T1.5)."""

import random

import pytest

from dmb.suites import s1_intent77, s2_spam, s3_cardinality, s4_order, s5_confidence
from dmb.suites.items import DecisionItem


def _s1_items(n_labels: int = 77, per_label: int = 6) -> tuple[list[DecisionItem], list[str]]:
    labels = [f"label_{i:02d}" for i in range(n_labels)]
    rows = [
        (f"utterance about {label} number {k}", label)
        for label in labels
        for k in range(per_label)
    ]
    items = s1_intent77.build_items(rows, labels)
    return items, labels


class TestS3:
    def test_every_item_has_exactly_one_planted_answer(self):
        vocab = s3_cardinality.build_vocab()
        items = s3_cardinality.build_items(vocab)
        assert len(items) == len(s3_cardinality.N_SWEEP) * s3_cardinality.ITEMS_PER_STEP
        for item in items:
            planted = item.options[item.gold_index]
            assert item.options.count(planted) == 1
            assert planted in item.state, "gold option must appear in the state"
            others = [o for o in item.options if o != planted]
            assert len(others) == len(item.options) - 1
            assert len(set(item.options)) == len(item.options), "options must be unique"
            # No distractor text leaks a duplicate of the planted word.
            for option in others:
                assert option != planted

    def test_n_sweep_matches_spec(self):
        items = s3_cardinality.build_items()
        observed = sorted({item.meta["N"] for item in items})
        assert observed == sorted(s3_cardinality.N_SWEEP)
        for item in items:
            assert len(item.options) == item.meta["N"]

    def test_regeneration_is_byte_identical(self):
        first = [item.model_dump_json() for item in s3_cardinality.build_items()]
        second = [item.model_dump_json() for item in s3_cardinality.build_items()]
        assert first == second

    def test_tiers_alternate_and_differ(self):
        items = s3_cardinality.build_items()
        step = [item for item in items if item.meta["N"] == 64]
        tiers = [item.meta["tier"] for item in step]
        assert tiers[0] == "easy" and tiers[1] == "hard"

    def test_easy_distractors_avoid_first_letter(self):
        vocab = s3_cardinality.build_vocab()
        rng = random.Random("test")
        planted = "kanel"
        pool = s3_cardinality._easy_distractors(rng, vocab, planted, 20)
        assert len(pool) == 20
        assert all(word[0] != planted[0] for word in pool)


class TestS4:
    def test_gold_maps_through_permutation(self):
        items, _ = _s1_items()
        spare = [item for item in items if item.meta["split"] == "spare"]
        s4 = s4_order.build_items(items)
        assert len(s4) == len(spare) * s4_order.PERMUTATIONS
        base_by_id = {item.item_id: item for item in spare}
        for item in s4:
            base = base_by_id[item.base_item_id]
            assert item.state == base.state
            assert sorted(item.options) == sorted(base.options)
            assert item.options[item.gold_index] == base.options[base.gold_index]
            perm = item.meta["perm"]
            assert sorted(perm) == list(range(len(perm))), "perm must be a bijection"
            # Mapping a permuted choice back to the original index works.
            new_choice = item.gold_index
            assert perm[new_choice] == base.gold_index

    def test_requires_100_spare_items(self):
        items, _ = _s1_items()
        for item in items:
            item.meta["split"] = "eval"
        with pytest.raises(ValueError):
            s4_order.build_items(items)


class TestS5:
    def test_no_good_option_items(self):
        items = s5_confidence.build_items()
        no_good = [i for i in items if i.kind == "no_good_option"]
        under = [i for i in items if i.kind == "underdetermined"]
        assert len(no_good) == 100 and len(under) == 100
        for item in no_good:
            assert item.gold_index == -1
            assert item.meta["planted"] not in item.options
            assert len(set(item.options)) == len(item.options)

    def test_underdetermined_items(self):
        items = s5_confidence.build_items()
        for item in items:
            if item.kind == "underdetermined":
                assert item.options[item.gold_index] == item.meta["planted"]
                assert 0 <= item.gold_index < len(item.options)

    def test_regeneration_is_identical(self):
        first = [i.model_dump_json() for i in s5_confidence.build_items()]
        second = [i.model_dump_json() for i in s5_confidence.build_items()]
        assert first == second


class TestS1S2:
    def test_s1_stratified_and_sized(self):
        items, labels = _s1_items()
        assert len(items) == 300
        assert items[0].options == labels
        assert len(set(labels)) == 77
        eval_labels = {i.meta["label"] for i in items if i.meta["split"] == "eval"}
        assert len(eval_labels) == 77, "every label appears in the eval split"

    def test_s2_prior_preserved_and_sized(self):
        rows = [("ham", f"hello there friend {k}") for k in range(870)]
        rows += [("spam", f"win a free prize now {k}") for k in range(130)]
        items = s2_spam.build_items(rows)
        assert len(items) == 300
        spam_share = sum(1 for i in items if i.meta["label"] == "spam") / len(items)
        assert spam_share == pytest.approx(0.13, abs=0.02)
        for item in items:
            assert sorted(item.options) == ["ham", "spam"]
            assert item.gold_index in (0, 1)
            assert item.options[item.gold_index] == item.meta["label"]

    def test_s2_rows_drop_duplicates_and_junk(self, tmp_path):
        path = tmp_path / "collection"
        path.write_text("ham\tgood msg\nspam\tbuy now\nham\tgood msg\njunkline\nham\tx\n")
        rows = s2_spam.load_rows(path)
        assert ("ham", "good msg") in rows
        assert len([r for r in rows if r[1] == "good msg"]) == 1
        assert ("ham", "x") not in rows  # single-word text dropped
        assert ("junkline",) not in rows
