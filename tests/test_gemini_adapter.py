"""Native Gemini requests, accounting, failure capture and publication round trips."""

import json
import tarfile

import httpx
import pytest

from dmb.contenders.base import AuthError, MalformedReply, RateLimited
from dmb.contenders.jsonmode import usage_details
from dmb.contenders.llm_gemini import GeminiContender, gemini_usage
from dmb.contenders.llm_openai import openai_contender
from dmb.prices import price_for
from dmb.report import archive_runs


def response():
    return {
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {
                    "parts": [
                        {"thought": True, "text": "private reasoning"},
                        {"text": '{"choice_index":1,"confidence":0.8}'},
                    ]
                },
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 1000,
            "candidatesTokenCount": 20,
            "thoughtsTokenCount": 80,
            "totalTokenCount": 1100,
            "cachedContentTokenCount": 800,
        },
    }


def contender(monkeypatch, handler, model="gemini-3.8-flash"):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-secret")
    c = GeminiContender(model)
    c._client.close()
    c._client = httpx.Client(transport=httpx.MockTransport(handler))
    return c


def test_native_schema_request_counts_thinking_and_cache(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=response())

    c = contender(monkeypatch, handler)
    try:
        decision = c.decide("state", ["a", "b"])
        body = json.loads(requests[0].content)
        assert requests[0].headers["x-goog-api-key"] == "fake-secret"
        assert "fake-secret" not in str(requests[0].url)
        assert body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        assert decision.choice_index == 1 and decision.output_tokens == 100
        assert decision.usage_details["cache_read_input_tokens"] == 800
        cost = price_for(c.name).account_usage(
            decision.input_tokens, decision.output_tokens, decision.usage_details
        )
        assert cost.complete
        assert cost.known_cost_usd == pytest.approx((200 * 0.75 + 800 * 0.075 + 100 * 3.75) / 1e6)
    finally:
        c.close()


@pytest.mark.parametrize("status,exception", [(401, AuthError), (429, RateLimited)])
def test_http_failures_preserve_usage(monkeypatch, status, exception):
    c = contender(monkeypatch, lambda request: httpx.Response(status, json=response()))
    try:
        with pytest.raises(exception) as caught:
            c.decide("state", ["a", "b"])
        assert caught.value.output_tokens == 100
    finally:
        c.close()


@pytest.mark.parametrize("failure", ["truncated", "out_of_range", "blocked", "non_json"])
def test_malformed_reply_keeps_billed_usage(monkeypatch, failure):
    payload = response()
    if failure == "truncated":
        payload["candidates"][0]["finishReason"] = "MAX_TOKENS"
    elif failure == "blocked":
        payload["candidates"] = []
    else:
        payload["candidates"][0]["content"]["parts"] = [
            {"text": '{"choice_index":9,"confidence":0.8}' if failure == "out_of_range" else "no"}
        ]
    c = contender(monkeypatch, lambda request: httpx.Response(200, json=payload))
    try:
        with pytest.raises(MalformedReply) as caught:
            c.decide("state", ["a", "b"])
        assert caught.value.input_tokens == 1000 and caught.value.output_tokens == 100
    finally:
        c.close()


def test_gemini_usage_total_includes_unreported_thoughts():
    payload = response()
    del payload["usageMetadata"]["thoughtsTokenCount"]
    assert gemini_usage(payload)[1] == 100
    assert gemini_usage({}) == (None, None, {})
    payload["usageMetadata"]["promptTokenCount"] = True
    assert gemini_usage(payload)[0] is None


@pytest.mark.parametrize(
    "model,effort,temp",
    [
        ("gpt-6-astra", "low", None),
        ("gpt-6-sol", "none", 0.0),
        ("gpt-6-luna", "none", 0.0),
        ("gpt-5.6-terra", "none", 0.0),
    ],
)
def test_openai_modern_configuration(monkeypatch, model, effort, temp):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-secret")
    c = openai_contender(model)
    try:
        configuration = c.effective_configuration()
        assert configuration["temperature"] == temp
        assert configuration["extra_body"]["reasoning_effort"] == effort
        assert price_for(c.name).cache_write_per_mtok is not None
    finally:
        c.close()


def test_modern_cache_writes_are_not_billed_twice():
    details = usage_details(
        {
            "usage": {
                "prompt_tokens_details": {
                    "cached_tokens": 500,
                    "cache_write_tokens": 300,
                }
            }
        }
    )
    account = price_for("openai:gpt-6-astra").account_usage(1000, 10, details)
    assert account.complete
    assert account.known_cost_usd == pytest.approx(
        (200 * 10 + 500 * 1 + 300 * 12.5 + 10 * 50) / 1e6
    )


def test_gemini_usage_and_configuration_survive_archive(monkeypatch, tmp_path):
    c = contender(monkeypatch, lambda request: httpx.Response(200, json=response()))
    try:
        run = tmp_path / "run"
        run.mkdir()
        row = {
            "contender": c.name,
            "suite": "s2_spam",
            "item_id": "s2-0001",
            "input_tokens": 1000,
            "output_tokens": 100,
            "usage_details": gemini_usage(response())[2],
        }
        (run / "results.jsonl").write_text(json.dumps(row) + "\n")
        (run / "manifest.json").write_text(
            json.dumps(
                {
                    "contenders": [
                        {
                            "name": c.name,
                            "effective_configuration": c.effective_configuration(),
                            "configuration_fingerprint": c.configuration_fingerprint,
                        }
                    ]
                }
            )
        )
        archive = archive_runs([run], tmp_path / "public.tar.gz")
        with tarfile.open(archive) as tar:
            saved = json.load(tar.extractfile("run/results.jsonl"))
            manifest = json.load(tar.extractfile("run/manifest.json"))
        assert saved["usage_details"]["thoughtsTokenCount"] == 80
        assert manifest["contenders"][0]["effective_configuration"] == c.effective_configuration()
    finally:
        c.close()
