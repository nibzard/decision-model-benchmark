"""Provider price table. Cost is computed from usage, never estimated.

Prices are list prices in USD per 1M tokens. ``checked_on`` is the date the
price was verified against the source URL. The price table hash goes into
every run manifest; if you change a price, change ``checked_on``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    """List price for one model, USD per 1M tokens."""

    contender: str
    model: str
    input_per_mtok: float
    output_per_mtok: float
    checked_on: str
    source: str

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        """Cost of one call from usage fields."""
        return (
            input_tokens / 1_000_000 * self.input_per_mtok
            + output_tokens / 1_000_000 * self.output_per_mtok
        )


# Verified 2026-09-18 against each provider's pricing page. See sources.
PRICES: tuple[Price, ...] = (
    Price("openai:gpt-5.4-nano", "gpt-5.4-nano", 0.06, 0.24,
          "2026-09-18", "https://platform.openai.com/docs/pricing"),
    Price("openai:gpt-5.4-mini", "gpt-5.4-mini", 0.40, 1.60,
          "2026-09-18", "https://platform.openai.com/docs/pricing"),
    Price("anthropic:claude-haiku-4-5", "claude-haiku-4-5", 1.00, 5.00,
          "2026-09-18", "https://www.anthropic.com/pricing"),
    Price("anthropic:claude-sonnet-4-6", "claude-sonnet-4-6", 3.00, 15.00,
          "2026-09-18", "https://www.anthropic.com/pricing"),
    Price("zai:glm-5.3-flash", "glm-5.3-flash", 0.11, 0.44,
          "2026-09-18", "https://z.ai/blog/announcing-glm-5.3"),
    Price("zai:glm-5.3", "glm-5.3", 0.60, 2.20,
          "2026-09-18", "https://z.ai/blog/announcing-glm-5.3"),
    Price("deepseek:deepseek-chat", "deepseek-chat", 0.27, 1.10,
          "2026-09-18", "https://api-docs.deepseek.com/quick_start/pricing"),
    Price("cerebras:llama-3.3-70b", "llama-3.3-70b", 0.85, 1.20,
          "2026-09-18", "https://inference-docs.cerebras.ai/support/pricing"),
    # Vendor-claimed pricing from the TypeSafe AI launch post; treat as
    # unverified until an invoice confirms it.
    Price("typesafe:jev", "jev-latest", 0.042, 0.0,
          "2026-09-18", "https://typesafe.ai (launch post claim)"),
    # Deterministic baselines cost nothing.
    Price("baseline:random", "-", 0.0, 0.0, "2026-09-18", "n/a"),
    Price("baseline:majority", "-", 0.0, 0.0, "2026-09-18", "n/a"),
    Price("baseline:keyword", "-", 0.0, 0.0, "2026-09-18", "n/a"),
)

_PRICE_MAP = {p.contender: p for p in PRICES}


def price_for(contender: str) -> Price:
    """Price entry for a contender name. KeyError for unknown contenders."""
    return _PRICE_MAP[contender]


def prices_table_hash() -> str:
    """SHA-256 over the serialized price table, pinned in run manifests."""
    payload = json.dumps(
        [p.__dict__ for p in PRICES], sort_keys=True, indent=2
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
