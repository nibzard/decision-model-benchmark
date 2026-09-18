"""jev adapter test against the documented response schema (no network)."""

import pytest

from dmb.contenders.base import MalformedReply, ProviderRejected
from dmb.contenders.jev import JevContender

FIXTURE_RESPONSE = {
    "model": "jev-latest",
    "answers": {
        "decision": {
            "type": "choice",
            "choice": "billing",
            "probabilities": {"billing": 0.84, "technical": 0.159, "sales": 0.001},
            "confidence": 0.596,
        }
    },
    "usage": {"input_tokens": 312, "output_tokens": 48},
}


@pytest.fixture()
def jev(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    return JevContender()


def test_maps_choice_key_to_index(jev, monkeypatch):
    monkeypatch.setattr(jev, "_post", lambda body: FIXTURE_RESPONSE)
    decision = jev.decide("some state", ["technical", "billing", "sales"])
    assert decision.choice_index == 1
    assert decision.confidence == pytest.approx(0.596)
    assert decision.input_tokens == 312
    assert decision.output_tokens == 48
    assert decision.raw["response"]["answers"]["decision"]["probabilities"]


def test_unknown_choice_key_is_malformed(jev, monkeypatch):
    broken = {"answers": {"decision": {"choice": "nope", "confidence": 0.5}}}
    monkeypatch.setattr(jev, "_post", lambda body: broken)
    with pytest.raises(MalformedReply):
        jev.decide("state", ["a", "b"])


def test_out_of_bounds_confidence_is_malformed(jev, monkeypatch):
    broken = {"answers": {"decision": {"choice": "a", "confidence": 1.5}}}
    monkeypatch.setattr(jev, "_post", lambda body: broken)
    with pytest.raises(MalformedReply):
        jev.decide("state", ["a", "b"])


def test_provider_rejection_is_measured_not_malformed(jev, monkeypatch):
    def reject(body):
        raise ProviderRejected("400: too many criteria (limit 255)")

    monkeypatch.setattr(jev, "_post", reject)
    with pytest.raises(ProviderRejected):
        jev.decide("state", ["a", "b"])


def test_request_body_shape(jev, monkeypatch):
    seen = {}

    def capture(body):
        seen.update(body)
        return {
            "answers": {"decision": {"choice": "kanel", "confidence": 0.7}},
            "usage": {"input_tokens": 20, "output_tokens": 4},
        }

    monkeypatch.setattr(jev, "_post", capture)
    jev.decide("gate 12 opens", ["kanel", "torba"])
    assert seen["model"] == "jev-latest"
    question = seen["questions"]["decision"]
    assert question["type"] == "choice"
    assert question["criteria"] == {"kanel": "kanel", "torba": "torba"}
    assert seen["state"] == "gate 12 opens"
