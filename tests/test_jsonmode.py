"""Parsing and usage-extraction tests (SPEC.md T2.6, T3.2).

Usage fixtures are recorded response shapes from each provider's API
documentation.
"""

import pytest

from dmb.contenders.base import MalformedReply, TransportError
from dmb.contenders.jsonmode import (
    JSONModeContender,
    anthropic_usage,
    extract_json_object,
    jev_usage,
    openai_style_usage,
    parse_decision_payload,
    validate_decision_dict,
)


class TestParsing:
    def test_plain_json(self):
        assert parse_decision_payload('{"choice_index": 1, "confidence": 0.7}', 3) == (1, 0.7)

    def test_json_in_prose(self):
        text = 'Sure! Here is the decision: {"choice_index": 0, "confidence": 0.55} hope that helps'
        assert parse_decision_payload(text, 3) == (0, 0.55)

    def test_markdown_fenced(self):
        text = '```json\n{"choice_index": 2, "confidence": 1.0}\n```'
        assert parse_decision_payload(text, 3) == (2, 1.0)

    def test_integer_confidence_accepted(self):
        assert parse_decision_payload('{"choice_index": 0, "confidence": 1}', 2) == (0, 1.0)

    @pytest.mark.parametrize(
        "payload",
        [
            '{"choice_index": 3, "confidence": 0.5}',  # out of bounds
            '{"choice_index": -1, "confidence": 0.5}',  # negative
            '{"choice_index": 0.5, "confidence": 0.5}',  # not an integer
            '{"choice_index": true, "confidence": 0.5}',  # boolean
            '{"choice_index": 0}',  # confidence missing
            '{"choice_index": 0, "confidence": 1.4}',  # confidence out of bounds
            '{"choice_index": 0, "confidence": "high"}',  # not a number
            "no json here at all",
            "[1, 2, 3]",
        ],
    )
    def test_malformed_payloads(self, payload):
        with pytest.raises(MalformedReply):
            parse_decision_payload(payload, 3)

    def test_extract_object_first_brace_wins(self):
        assert extract_json_object('prefix {"a": 1} suffix') == {"a": 1}


class TestUsageExtraction:
    def test_openai_style(self):
        response = {
            "choices": [{"message": {"content": '{"choice_index": 0, "confidence": 0.9}'}}],
            "usage": {"prompt_tokens": 312, "completion_tokens": 12},
        }
        assert openai_style_usage(response) == (312, 12)

    def test_openai_style_with_cache_fields(self):
        response = {
            "usage": {
                "prompt_tokens": 312,
                "completion_tokens": 12,
                "prompt_tokens_details": {"cached_tokens": 256},
            }
        }
        assert openai_style_usage(response) == (312, 12)

    def test_openai_style_missing_usage(self):
        assert openai_style_usage({}) == (None, None)

    def test_anthropic(self):
        response = {"usage": {"input_tokens": 521, "output_tokens": 63}}
        assert anthropic_usage(response) == (521, 63)

    def test_jev(self):
        response = {"usage": {"input_tokens": 312, "output_tokens": 48}}
        assert jev_usage(response) == (312, 48)

    def test_validate_dict_rejects_bool(self):
        with pytest.raises(MalformedReply):
            validate_decision_dict({"choice_index": 1, "confidence": True}, 3)


class TestNegotiationAndBilling:
    """Finding 2: probe usage is reported; failed parses still bill."""

    def _contender(self) -> JSONModeContender:
        return JSONModeContender("probe:test", "test-model", "http://invalid", {})

    def test_negotiate_returns_probe_usage(self):
        contender = self._contender()
        contender._post = lambda body: {
            "choices": [{"message": {"content": '{"choice_index": 1, "confidence": 1.0}'}}],
            "usage": {"prompt_tokens": 42, "completion_tokens": 7},
        }
        assert contender.negotiate() == {"input_tokens": 42, "output_tokens": 7}

    def test_negotiate_without_usage_reports_none(self):
        contender = self._contender()
        contender._post = lambda body: {
            "choices": [{"message": {"content": '{"choice_index": 0, "confidence": 1.0}'}}],
            "usage": {},
        }
        assert contender.negotiate() is None

    def test_negotiate_failure_returns_none_with_note(self):
        contender = self._contender()

        def down(_body):
            raise TransportError("connection refused")

        contender._post = down
        assert contender.negotiate() is None
        assert any("negotiation probe failed" in note for note in contender.notes)

    def test_malformed_reply_attaches_usage_and_response(self):
        contender = self._contender()
        response = {
            "choices": [{"message": {"content": "no json object here"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }
        contender._post = lambda body: response
        with pytest.raises(MalformedReply) as excinfo:
            contender._decide("state", ["a", "b"])
        assert excinfo.value.input_tokens == 10
        assert excinfo.value.output_tokens == 2
        assert excinfo.value.response is response


class TestAnthropicTextFallback:
    """The gateway sometimes answers in text instead of calling the tool."""

    def _contender(self):
        import os

        os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "test-token")
        from dmb.contenders.llm_anthropic import AnthropicContender

        return AnthropicContender("claude-haiku-4-5")

    def test_tool_use_block_preferred(self):
        contender = self._contender()
        response = {
            "content": [
                {"type": "text", "text": "thinking out loud"},
                {"type": "tool_use", "name": "record_decision",
                 "input": {"choice_index": 1, "confidence": 0.5}},
            ]
        }
        payload, source = contender._extract_tool_input(response)
        assert source == "tool_use"
        assert payload == {"choice_index": 1, "confidence": 0.5}

    def test_text_block_parsed_when_no_tool_use(self):
        contender = self._contender()
        response = {
            "content": [
                {"type": "text", "text": '{"choice_index": 0, "confidence": 0.9}'},
            ]
        }
        payload, source = contender._extract_tool_input(response)
        assert source == "text_fallback"
        assert payload == {"choice_index": 0, "confidence": 0.9}

    def test_both_paths_failing_is_malformed(self):
        from dmb.contenders.base import MalformedReply

        contender = self._contender()
        response = {"content": [{"type": "text", "text": "no json here"}]}
        with pytest.raises(MalformedReply):
            contender._extract_tool_input(response)

    def test_truncation_is_malformed(self):
        from dmb.contenders.base import MalformedReply

        contender = self._contender()
        contender._post = lambda body: {"stop_reason": "max_tokens"}
        with pytest.raises(MalformedReply):
            contender._decide("state text", ["a", "b"])
