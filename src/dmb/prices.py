"""Provider price table. Costs are explicit when usage or cache rates are unknown.

Prices are list prices in USD per 1M tokens. ``checked_on`` is the date the
price was verified against the source URL. The price table hash goes into
every run manifest; if you change a price, change ``checked_on``.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CostAccounting:
    known_cost_usd: float | None
    complete: bool
    reason: str | None
    budget_estimate_usd: float | None


@dataclass(frozen=True)
class Price:
    """List price for one model, USD per 1M tokens."""

    contender: str
    model: str
    input_per_mtok: float
    output_per_mtok: float
    checked_on: str
    source: str
    cache_read_per_mtok: float | None = None
    cache_write_per_mtok: float | None = None

    def __post_init__(self) -> None:
        for value in (
            self.input_per_mtok,
            self.output_per_mtok,
            self.cache_read_per_mtok,
            self.cache_write_per_mtok,
        ):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError("price rates must be finite and nonnegative")

    def account_usage(
        self,
        input_tokens: int | None,
        output_tokens: int | None,
        usage_details: dict[str, Any] | None = None,
    ) -> CostAccounting:
        """Cost from measured usage; unverified cache rates stay unknown.

        A missing cache rate supplies no guaranteed spending bound. No current
        rate is substituted for a historical snapshot. known_cost_usd is a
        subtotal when incomplete, never a claim about the final bill.
        """
        details = usage_details or {}
        values = [
            input_tokens,
            output_tokens,
            details.get("cache_read_input_tokens", 0),
            details.get("cache_write_input_tokens", 0),
        ]
        if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in values):
            return CostAccounting(None, False, "missing or invalid token usage", None)
        inp, out, read, write = values
        include = details.get("input_tokens_include_cache", True)
        ordinary = inp - read - write if include else inp
        if ordinary < 0:
            return CostAccounting(None, False, "cache tokens exceed input total", None)
        known = self.cost_usd(ordinary, out)
        missing = []
        for count, rate, name in (
            (read, self.cache_read_per_mtok, "cache read"),
            (write, self.cache_write_per_mtok, "cache write"),
        ):
            if count and rate is None:
                missing.append(name)
            elif rate is not None:
                known += count * rate / 1_000_000
        reason = "unverified historical " + ", ".join(missing) + " rate" if missing else None
        return CostAccounting(known, not missing, reason, None if missing else known)

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        """Cost of one call from usage fields."""
        return (
            input_tokens / 1_000_000 * self.input_per_mtok
            + output_tokens / 1_000_000 * self.output_per_mtok
        )


# Base prices originally verified 2026-09-18; OpenAI/Anthropic cache reads
# reverified 2026-09-26. Cache write TTL and historical prices are unknown.
# Sources: provider pricing pages and announcements
# (see each entry). Notes: DeepSeek shows peak pricing (off-peak is half);
# the Anthropic models are reached through a gateway here, so billed rates
# may differ from Anthropic list prices.
PRICES: tuple[Price, ...] = (
    Price(
        "openai:gpt-6-astra",
        "gpt-6-astra",
        10.0,
        50.0,
        "2026-09-26",
        "https://developers.openai.com/api/docs/pricing (standard; <=272K input)",
        cache_read_per_mtok=1.0,
        cache_write_per_mtok=12.5,
    ),
    Price(
        "openai:gpt-6-sol",
        "gpt-6-sol",
        2.0,
        10.0,
        "2026-09-26",
        "https://developers.openai.com/api/docs/pricing (standard; <=272K input)",
        cache_read_per_mtok=0.2,
        cache_write_per_mtok=2.5,
    ),
    Price(
        "openai:gpt-6-luna",
        "gpt-6-luna",
        0.1,
        0.5,
        "2026-09-26",
        "https://developers.openai.com/api/docs/pricing (standard; <=272K input)",
        cache_read_per_mtok=0.01,
        cache_write_per_mtok=0.125,
    ),
    Price(
        "openai:gpt-5.6-terra",
        "gpt-5.6-terra",
        2.0,
        12.0,
        "2026-09-26",
        "https://developers.openai.com/api/docs/pricing (standard; <=272K input)",
        cache_read_per_mtok=0.2,
        cache_write_per_mtok=2.5,
    ),
    Price(
        "gemini:gemini-3.8-flash",
        "gemini-3.8-flash",
        0.75,
        3.75,
        "2026-09-26",
        "https://ai.google.dev/gemini-api/docs/pricing "
        "(standard paid tier; promo through 2026-12-31)",
        cache_read_per_mtok=0.075,
    ),
    Price(
        "gemini:gemini-3.5-flash-lite",
        "gemini-3.5-flash-lite",
        0.3,
        2.5,
        "2026-09-26",
        "https://ai.google.dev/gemini-api/docs/pricing (standard paid tier)",
        cache_read_per_mtok=0.03,
    ),
    Price(
        "gemini:gemini-3.1-pro-preview",
        "gemini-3.1-pro-preview",
        2.0,
        12.0,
        "2026-09-26",
        "https://ai.google.dev/gemini-api/docs/pricing (standard paid tier; <=200K input)",
        cache_read_per_mtok=0.2,
    ),
    Price(
        "openai:gpt-5.4-nano",
        "gpt-5.4-nano",
        0.20,
        1.25,
        "2026-09-26",
        "https://developers.openai.com/api/docs/models/gpt-5.4-nano",
        cache_read_per_mtok=0.02,
    ),
    Price(
        "openai:gpt-5.4-mini",
        "gpt-5.4-mini",
        0.75,
        4.50,
        "2026-09-26",
        "https://developers.openai.com/api/docs/models/gpt-5.4-mini",
        cache_read_per_mtok=0.075,
    ),
    Price(
        "anthropic:claude-haiku-4-5",
        "claude-haiku-4-5",
        1.00,
        5.00,
        "2026-09-26",
        "https://platform.claude.com/docs/en/about-claude/pricing (list; gateway bill unknown)",
        cache_read_per_mtok=0.1,
    ),
    Price(
        "anthropic:claude-sonnet-4-6",
        "claude-sonnet-4-6",
        3.00,
        15.00,
        "2026-09-26",
        "https://platform.claude.com/docs/en/about-claude/pricing (list; gateway bill unknown)",
        cache_read_per_mtok=0.3,
    ),
    Price("zai:glm-5.3-flash", "glm-5.3-flash", 0.15, 0.50, "2026-09-18", "https://z.ai/pricing"),
    Price("zai:glm-5.3", "glm-5.3", 1.40, 4.40, "2026-09-18", "https://z.ai/pricing"),
    Price(
        "deepseek:deepseek-chat",
        "deepseek-chat",
        0.30,
        1.20,
        "2026-09-18",
        "https://api-docs.deepseek.com/quick_start/pricing (peak; off-peak half)",
    ),
    Price(
        "cerebras:gpt-oss-120b",
        "gpt-oss-120b",
        0.25,
        0.69,
        "2026-09-18",
        "https://www.cerebras.ai/pricing",
    ),
    # Vendor-claimed pricing from the TypeSafe AI launch post; treat as
    # unverified until an invoice confirms it.
    Price(
        "typesafe:jev",
        "jev-latest",
        0.042,
        0.0,
        "2026-09-18",
        "https://typesafe.ai (launch post claim)",
    ),
    # Decisions API list price from the published guide: input tokens only,
    # no cache-read, cache-write, or output charges. Regional premiums and
    # long-context multipliers are not encoded here.
    Price(
        "openai-decisions:gpt-6-luna",
        "gpt-6-luna",
        0.10,
        0.0,
        "2026-10-07",
        "https://developers.openai.com/api/docs/guides/decisions (input tokens only)",
        cache_read_per_mtok=0.0,
        cache_write_per_mtok=0.0,
    ),
    # Deterministic baselines cost nothing.
    Price("baseline:random", "-", 0.0, 0.0, "2026-09-18", "n/a"),
    Price("baseline:majority", "-", 0.0, 0.0, "2026-09-18", "n/a"),
    Price("baseline:keyword", "-", 0.0, 0.0, "2026-09-18", "n/a"),
)

_PRICE_MAP = {p.contender: p for p in PRICES}


def price_for(contender: str) -> Price:
    """Price entry for a contender name. KeyError for unknown contenders."""
    return _PRICE_MAP[contender]


def snapshot_hash(payload: list[dict]) -> str:
    """Hash exact snapshot bytes canonically, without filling new defaults."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True, indent=2).encode()).hexdigest()


def prices_table_hash() -> str:
    return snapshot_hash(snapshot_payload())


def snapshot_payload() -> list[dict]:
    """Serialize only populated optional rates, preserving legacy hashes."""
    return [{k: v for k, v in p.__dict__.items() if v is not None} for p in PRICES]


def load_snapshot(payload: list[dict]) -> dict[str, Price]:
    """Parse a stored price snapshot into ``{contender: Price}``."""
    return {
        entry["contender"]: Price(
            contender=entry["contender"],
            model=entry["model"],
            input_per_mtok=float(entry["input_per_mtok"]),
            output_per_mtok=float(entry["output_per_mtok"]),
            checked_on=entry["checked_on"],
            source=entry["source"],
            cache_read_per_mtok=(
                float(entry["cache_read_per_mtok"])
                if entry.get("cache_read_per_mtok") is not None
                else None
            ),
            cache_write_per_mtok=(
                float(entry["cache_write_per_mtok"])
                if entry.get("cache_write_per_mtok") is not None
                else None
            ),
        )
        for entry in payload
    }
