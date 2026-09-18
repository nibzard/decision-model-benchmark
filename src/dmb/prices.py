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


# Verified 2026-09-18. Sources: provider pricing pages and announcements
# (see each entry). Notes: DeepSeek shows peak pricing (off-peak is half);
# the Anthropic models are reached through a gateway here, so billed rates
# may differ from Anthropic list prices.
PRICES: tuple[Price, ...] = (
    Price("openai:gpt-5.4-nano", "gpt-5.4-nano", 0.20, 1.25,
          "2026-09-18", "https://developers.openai.com/pricing"),
    Price("openai:gpt-5.4-mini", "gpt-5.4-mini", 0.75, 4.50,
          "2026-09-18", "https://developers.openai.com/pricing"),
    Price("anthropic:claude-haiku-4-5", "claude-haiku-4-5", 1.00, 5.00,
          "2026-09-18", "https://www.anthropic.com/pricing (list; via gateway)"),
    Price("anthropic:claude-sonnet-4-6", "claude-sonnet-4-6", 3.00, 15.00,
          "2026-09-18", "https://www.anthropic.com/pricing (list; via gateway)"),
    Price("zai:glm-5.3-flash", "glm-5.3-flash", 0.15, 0.50,
          "2026-09-18", "https://z.ai/pricing"),
    Price("zai:glm-5.3", "glm-5.3", 1.40, 4.40,
          "2026-09-18", "https://z.ai/pricing"),
    Price("deepseek:deepseek-chat", "deepseek-chat", 0.30, 1.20,
          "2026-09-18", "https://api-docs.deepseek.com/quick_start/pricing (peak; off-peak half)"),
    Price("cerebras:gpt-oss-120b", "gpt-oss-120b", 0.25, 0.69,
          "2026-09-18", "https://www.cerebras.ai/pricing"),
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
