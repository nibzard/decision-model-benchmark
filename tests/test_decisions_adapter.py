"""Decisions API adapter test against the published contract (no network).

Covers plan 002 step 3: valid answers, invalid identifiers, malformed
responses, refusals, authentication errors, rate limits, usage, frozen
settings, pricing, and the accounting stop.
"""

import json

import httpx
import pytest

from dmb.contenders import build_contenders
from dmb.contenders.base import (
    AuthError,
    MalformedReply,
    ProviderRejected,
    RateLimited,
    TransportError,
)
from dmb.contenders.openai_decisions import OpenAIDecisionsContender
from dmb.contenders.render import render_choice_instructions
from dmb.prices import price_for
from dmb.runner import SpendTracker

FIXTURE_RESPONSE = {
    "model": "gpt-6-luna",
    "answers": [
        {
            "type": "choice",
            "name": "decision",
            "choice": "billing",
            "probabilities": [
                {"value": "technical", "probability": 0.03},
                {"value": "billing", "probability": 0.94},
                {"value": "shipping", "probability": 0.03},
            ],
            "confidence": 0.91,
        }
    ],
    "usage": {"input_tokens": 312, "output_tokens": 0},
}

OPTIONS = ["technical", "billing", "shipping"]


def contender(monkeypatch, handler) -> OpenAIDecisionsContender:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    c = OpenAIDecisionsContender()
    headers = dict(c._client.headers)
    c._client.close()
    c._client = httpx.Client(transport=httpx.MockTransport(handler), headers=headers)
    return c


def decide_with(monkeypatch, response, options=None):
    """Run one decide against a scripted response; returns or raises."""
    c = contender(monkeypatch, lambda request: httpx.Response(200, json=response))
    try:
        return c.decide("some state", options or OPTIONS)
    finally:
        c.close()


def test_request_shape_and_choice_mapping(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=FIXTURE_RESPONSE)

    c = contender(monkeypatch, handler)
    try:
        decision = c.decide("  I was charged twice.  ", OPTIONS)
        body = json.loads(requests[0].content)
        assert str(requests[0].url) == "https://api.openai.com/v1/decisions"
        assert requests[0].headers["authorization"] == "Bearer test-key"
        assert body["model"] == "gpt-6-luna"
        assert body["input"] == "I was charged twice."
        (question,) = body["questions"]
        assert question["name"] == "decision"
        assert question["type"] == "choice"
        assert question["instructions"] == render_choice_instructions()
        assert question["choices"] == [
            {"value": option, "description": option} for option in OPTIONS
        ]
        assert decision.choice_index == 1
        assert decision.confidence == pytest.approx(0.91)
        assert decision.input_tokens == 312
        assert decision.output_tokens == 0
        assert decision.ok and not decision.malformed
    finally:
        c.close()


def test_choice_mapped_by_value_not_position(monkeypatch):
    response = {
        "answers": [{"type": "choice", "name": "decision", "choice": "shipping", "confidence": 0.4}]
    }
    decision = decide_with(monkeypatch, response)
    assert decision.choice_index == 2
    assert decision.confidence == pytest.approx(0.4)


@pytest.mark.parametrize(
    "answer",
    [
        {"type": "choice", "name": "decision", "choice": "nope", "confidence": 0.5},
        {"type": "choice", "name": "decision", "choice": "billing", "confidence": 1.5},
        {"type": "choice", "name": "decision", "choice": "billing", "confidence": True},
        {"type": "choice", "name": "decision", "choice": "billing"},
        {"type": "refusal", "name": "decision"},
        {"type": "choice", "name": "other_question", "choice": "billing", "confidence": 0.5},
    ],
)
def test_invalid_answers_are_malformed(monkeypatch, answer):
    with pytest.raises(MalformedReply):
        decide_with(monkeypatch, {"answers": [answer]})


@pytest.mark.parametrize(
    "response",
    [
        {"answers": {"decision": {"choice": "billing", "confidence": 0.5}}},
        {"model": "gpt-6-luna"},
        {"answers": []},
        {"answers": ["not-an-object"]},
    ],
)
def test_unexpected_response_shapes_are_malformed(monkeypatch, response):
    with pytest.raises(MalformedReply):
        decide_with(monkeypatch, response)


def test_malformed_answer_keeps_billed_usage(monkeypatch):
    response = {
        "answers": [{"type": "choice", "name": "decision", "choice": "nope", "confidence": 0.5}],
        "usage": {"input_tokens": 312, "output_tokens": 0},
    }
    with pytest.raises(MalformedReply) as caught:
        decide_with(monkeypatch, response)
    assert caught.value.input_tokens == 312
    assert caught.value.output_tokens == 0


@pytest.mark.parametrize(
    "status,exception",
    [
        (401, AuthError),
        (403, AuthError),
        (429, RateLimited),
        (400, ProviderRejected),
        (500, TransportError),
        (503, TransportError),
    ],
)
def test_http_failures_classified(monkeypatch, status, exception):
    c = contender(
        monkeypatch, lambda request: httpx.Response(status, json={"error": {"message": "x"}})
    )
    try:
        with pytest.raises(exception):
            c.decide("state", OPTIONS)
    finally:
        c.close()


def test_rate_limit_preserves_reported_usage(monkeypatch):
    c = contender(monkeypatch, lambda request: httpx.Response(429, json=FIXTURE_RESPONSE))
    try:
        with pytest.raises(RateLimited) as caught:
            c.decide("state", OPTIONS)
        assert caught.value.input_tokens == 312
    finally:
        c.close()


def test_non_json_success_is_malformed(monkeypatch):
    c = contender(monkeypatch, lambda request: httpx.Response(200, text="<html>"))
    try:
        with pytest.raises(MalformedReply):
            c.decide("state", OPTIONS)
    finally:
        c.close()


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": 10, "output_tokens": 2},
        {"prompt_tokens": 10, "completion_tokens": 2},
    ],
)
def test_usage_accepts_both_documented_field_names(monkeypatch, usage):
    response = {
        "answers": [{"type": "choice", "name": "decision", "choice": "billing", "confidence": 0.9}],
        "usage": usage,
    }
    decision = decide_with(monkeypatch, response)
    assert decision.input_tokens == 10
    assert decision.output_tokens == 2


def test_missing_usage_stays_unknown(monkeypatch):
    response = {
        "answers": [{"type": "choice", "name": "decision", "choice": "billing", "confidence": 0.9}]
    }
    decision = decide_with(monkeypatch, response)
    assert decision.input_tokens is None
    assert decision.output_tokens is None


def test_price_entry_is_input_only_and_accounts_exactly():
    price = price_for("openai-decisions:gpt-6-luna")
    assert price.input_per_mtok == 0.10
    assert price.output_per_mtok == 0.0
    assert price.checked_on == "2026-10-07"
    accounting = price.account_usage(1_000_000, 0, {"input_tokens_include_cache": True})
    assert accounting.complete
    assert accounting.known_cost_usd == pytest.approx(0.10)


def test_accounting_stop_blocks_unknown_usage_before_scored_requests():
    name = "openai-decisions:gpt-6-luna"
    tracker = SpendTracker(prices={name: price_for(name)})
    tracker.record(name, 312, 0)
    assert tracker.accounting_blocker is None
    tracker.record(name, None, None, block_incomplete=True)
    assert "cannot enforce hard cap" in (tracker.accounting_blocker or "")


def test_configuration_frozen_after_negotiation(monkeypatch):
    c = contender(monkeypatch, lambda request: httpx.Response(200, json=FIXTURE_RESPONSE))
    try:
        result = c.negotiate()
        assert result["effective_configuration"] == {
            "model": "gpt-6-luna",
            "confidence_semantics": "provider-defined confidence",
        }
        assert result["input_tokens"] == 312  # the probe is billed
        assert c.configuration_fingerprint == result["configuration_fingerprint"]
        with pytest.raises(ValueError):
            c.negotiate()  # renegotiation is refused after freezing
    finally:
        c.close()


def test_negotiation_records_auth_failure(monkeypatch):
    c = contender(monkeypatch, lambda request: httpx.Response(401, json={"error": "bad key"}))
    try:
        result = c.negotiate()
        assert result["attempts"][0]["ok"] is False
        assert result["attempts"][0]["category"] == "auth"
        assert any("negotiation probe failed" in note for note in c.notes)
    finally:
        c.close()


def test_registry_gates_on_openai_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    contenders, skipped, _ = build_contenders(
        include_decisions=True, negotiate=False, wanted=["openai-decisions"]
    )
    assert contenders == []
    assert skipped == [
        {"contender": "openai-decisions:gpt-6-luna", "reason": "env OPENAI_API_KEY not set"}
    ]


def test_registry_builds_decisions_contender_when_selected(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    contenders, skipped, _ = build_contenders(
        include_decisions=True, negotiate=False, wanted=["openai-decisions"]
    )
    assert [c.name for c in contenders] == ["openai-decisions:gpt-6-luna"]
    assert skipped == []


def test_decisions_never_runs_by_default(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    contenders, skipped, _ = build_contenders(negotiate=False, wanted=["openai-decisions"])
    assert contenders == []
    assert skipped == []
