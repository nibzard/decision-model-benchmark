"""Usage accounting never substitutes unverified historical cache rates."""

from dataclasses import replace

import pytest

from dmb.prices import load_snapshot, price_for, prices_table_hash, snapshot_hash, snapshot_payload


def test_optional_fields_preserve_legacy_price_hash():
    # Old snapshots retain their hash after parsing rather than gaining defaults.
    legacy = [
        {
            "contender": "old",
            "model": "model",
            "input_per_mtok": 1.0,
            "output_per_mtok": 2.0,
            "checked_on": "2026-09-18",
            "source": "fixture",
        }
    ]
    original = snapshot_hash(legacy)
    prices = load_snapshot(legacy)
    serialized = [
        {k: v for k, v in price.__dict__.items() if v is not None} for price in prices.values()
    ]
    assert original == snapshot_hash(serialized)
    assert snapshot_hash(snapshot_payload()) == prices_table_hash()
    assert price_for("openai:gpt-5.4-mini").cache_read_per_mtok == 0.075


def test_exact_cache_accounting_openai_and_anthropic():
    price = replace(
        price_for("openai:gpt-5.4-mini"), cache_read_per_mtok=0.1, cache_write_per_mtok=1.0
    )
    details = {
        "cache_read_input_tokens": 60,
        "cache_write_input_tokens": 10,
        "input_tokens_include_cache": True,
    }
    inclusive = price.account_usage(100, 20, details)
    details["input_tokens_include_cache"] = False
    exclusive = price.account_usage(30, 20, details)
    assert inclusive == exclusive
    assert inclusive.complete
    assert inclusive.known_cost_usd == pytest.approx((30 * 0.75 + 60 * 0.1 + 10 + 20 * 4.5) / 1e6)


def test_unknown_cache_rate_is_not_exact_cost_or_safe_budget():
    result = replace(price_for("openai:gpt-5.4-mini"), cache_read_per_mtok=None).account_usage(
        100, 20, {"cache_read_input_tokens": 60}
    )
    assert not result.complete
    assert result.known_cost_usd == pytest.approx((40 * 0.75 + 20 * 4.5) / 1e6)
    assert result.budget_estimate_usd is None
    assert result.reason == "unverified historical cache read rate"


@pytest.mark.parametrize(
    "inp,out,details",
    [
        (None, 2, {}),
        (True, 2, {}),
        (-1, 2, {}),
        (2, 2, {"cache_read_input_tokens": 3}),
        (2, 2, {"cache_read_input_tokens": "1"}),
    ],
)
def test_unknown_or_invalid_usage_is_not_zero_cost(inp, out, details):
    result = price_for("openai:gpt-5.4-mini").account_usage(inp, out, details)
    assert result.known_cost_usd is None
    assert not result.complete
    assert result.budget_estimate_usd is None


def test_snapshot_round_trip_optional_rates():
    payload = snapshot_payload()
    payload[0]["cache_read_per_mtok"] = 0.012
    assert load_snapshot(payload)[payload[0]["contender"]].cache_read_per_mtok == 0.012
    payload[0]["input_per_mtok"] = float("nan")
    with pytest.raises(ValueError):
        load_snapshot(payload)


def test_cerebras_explicit_unknown_price_rejected(monkeypatch):
    from dmb.contenders.llm_cerebras import resolve_model

    monkeypatch.setenv("CEREBRAS_MODEL", "llama-3.3-70b")
    with pytest.raises(KeyError):
        resolve_model()
