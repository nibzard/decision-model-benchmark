"""Parsing and usage-extraction tests (SPEC.md T2.6, T3.2).

Usage fixtures are recorded response shapes from each provider's API
documentation.
"""

import pytest

from dmb.contenders.base import MalformedReply
from dmb.contenders.jsonmode import (
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
