"""OpenAI contenders with strict json_schema structured output."""

from __future__ import annotations

import os

from .jsonmode import JSONModeContender

DECISION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "choice_index": {"type": "integer"},
        "confidence": {"type": "number"},
    },
    "required": ["choice_index", "confidence"],
    "additionalProperties": False,
}


def openai_contender(model: str) -> JSONModeContender:
    """One OpenAI contender: ``openai:<model>``."""
    modern = model.startswith(("gpt-6-", "gpt-5.6-"))
    effort = "low" if model == "gpt-6-astra" else "none"
    contender = JSONModeContender(
        name=f"openai:{model}",
        model=model,
        url="https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "dmb_decision",
                "strict": True,
                "schema": DECISION_SCHEMA,
            },
        },
        temperature=None if modern and effort != "none" else 0.0,
        max_tokens=4000,
        max_tokens_param="max_completion_tokens",
        extra_body={"reasoning_effort": effort, "service_tier": "default"} if modern else None,
    )
    if modern:
        contender.deviation = f"{contender.name}: reasoning_effort={effort}, standard service tier"
    return contender
