"""Metrics for open-set routing and grouped binary intent decisions."""

from __future__ import annotations

from collections import defaultdict

from .suites.items import DecisionItem


def decision_units(
    rows: list[dict], items: dict[str, DecisionItem], repeats: int = 1
) -> list[dict]:
    """One unit per item, or one complete message per NLU++ label group.

    Missing and failed answers remain in the denominator. A grouped unit
    needs every intent answer; its threshold score is the lowest label score.
    """
    indexed = {(row["item_id"], row["repeat"]): row for row in rows}
    if len(indexed) != len(rows):
        raise ValueError("duplicate item/repeat results")
    groups = defaultdict(list)
    for item in items.values():
        groups[item.base_item_id or item.item_id].append(item)
    units = []
    for group in groups.values():
        expected_size = group[0].meta.get("group_size", 1)
        for repeat in range(repeats):
            observed = [indexed.get((item.item_id, repeat)) for item in group]
            valid = len(group) == expected_size and all(r and r["ok"] for r in observed)
            units.append(
                {
                    "valid": bool(valid),
                    "correct": bool(valid and all(r["correct"] for r in observed)),
                    "score": min(r["confidence"] for r in observed) if valid else None,
                }
            )
    return units


def task_metrics(rows: list[dict], items: dict[str, DecisionItem], repeats: int = 1) -> dict:
    if not items:
        return {}
    units = decision_units(rows, items, repeats)
    valid = [row for row in rows if row["ok"]]
    expected = len(items) * repeats
    result = {
        "success_all_requested": sum(row["correct"] for row in valid) / expected,
    }
    example = next(iter(items.values()))
    if example.suite.startswith("s7_clinc150"):
        predicted_oos = [
            r for r in valid if r["choice_index"] == len(items[r["item_id"]].options) - 1
        ]
        correct_oos = sum(items[r["item_id"]].meta["out_of_scope"] for r in predicted_oos)
        expected_oos = sum(i.meta["out_of_scope"] for i in items.values()) * repeats
        expected_in = expected - expected_oos
        result.update(
            {
                "out_of_scope_precision": correct_oos / len(predicted_oos)
                if predicted_oos
                else None,
                "out_of_scope_recall": correct_oos / expected_oos if expected_oos else None,
                "in_scope_success": sum(
                    r["correct"] for r in valid if not items[r["item_id"]].meta["out_of_scope"]
                )
                / expected_in
                if expected_in
                else None,
            }
        )
    if example.suite.startswith("s8_nlupp"):
        expected_positive = sum(i.gold_index == 1 for i in items.values()) * repeats
        positive = [r for r in valid if r["choice_index"] == 1]
        tp = sum(r["gold_index"] == 1 for r in positive)
        denominator = expected_positive + len(positive)
        label_scores = []
        for label in sorted({i.meta["intent"] for i in items.values()}):
            label_ids = {i.item_id for i in items.values() if i.meta["intent"] == label}
            gold_count = sum(items[item_id].gold_index == 1 for item_id in label_ids) * repeats
            guesses = [r for r in positive if r["item_id"] in label_ids]
            hits = sum(r["gold_index"] == 1 for r in guesses)
            label_scores.append(
                2 * hits / (gold_count + len(guesses)) if gold_count + len(guesses) else 0.0
            )
        result.update(
            {
                "micro_intent_f1": 2 * tp / denominator if denominator else 0.0,
                "macro_intent_f1": sum(label_scores) / len(label_scores),
                "complete_message_accuracy": sum(u["correct"] for u in units) / len(units),
                "complete_message_coverage": sum(u["valid"] for u in units) / len(units),
                "expected_messages": len(units),
            }
        )
    return result
