"""DeepSeek contender, JSON mode."""

from __future__ import annotations

import os

from .jsonmode import JSONModeContender


def deepseek_contender(model: str = "deepseek-chat") -> JSONModeContender:
    """One DeepSeek contender: ``deepseek:<model>``."""
    return JSONModeContender(
        name=f"deepseek:{model}",
        model=model,
        url="https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY']}"},
        response_format={"type": "json_object"},
        temperature=0.0,
        max_tokens=1024,
    )
