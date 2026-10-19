# -*- coding: utf-8 -*-
"""How long a provider keeps the prompt cache of a session's last request.

No provider reports an expiry time in its responses, so the cache of a
session's last request can only be timed from the lifetime the provider
documents, counted from the start of that request. Only providers that
document a lifetime have a row here:

* EXACT: the provider guarantees at least this long after a request that
  wrote or read the cache (OpenAI GPT-5.6+, Anthropic, Bedrock).
* TYPICAL: the provider only documents a usual figure and may drop the
  cache sooner (OpenAI models before GPT-5.6).

Every other provider caches best-effort or not at all (xAI evicts at any
time, DeepSeek within hours to days, local servers until the model unloads,
OpenRouter depends on the upstream it picks), so it has no row and no
countdown is shown for it.

Keys follow the reasoning table: ``reasoning_surface(provider, auth_mode)``
and the EXACT model id (no prefix or substring matching).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Mapping, Optional

from agent_core.core.models.reasoning import reasoning_surface


class CacheLifetimeKind(str, Enum):
    EXACT = "exact"
    TYPICAL = "typical"


@dataclass(frozen=True)
class CacheLifetime:
    """How long a request's prompt cache stays usable after it starts."""

    kind: CacheLifetimeKind
    seconds: int
    #: Where the figure comes from.
    source: str


_OPENAI_DOCS = "https://developers.openai.com/api/docs/guides/prompt-caching"

#: prompt_cache_options.ttl defaults to "30m", its only value: a prefix stays
#: usable 30 minutes after its most recent write or reuse (a minimum).
_OPENAI_30_MIN = CacheLifetime(CacheLifetimeKind.EXACT, 30 * 60, _OPENAI_DOCS)

#: Before GPT-5.6, in_memory entries "typically remain active for around 5 to
#: 10 minutes of inactivity" and 24h entries "around 30 minutes"; which one
#: applies depends on the organization, so the shorter figure is shown.
_OPENAI_TYPICAL = CacheLifetime(CacheLifetimeKind.TYPICAL, 5 * 60, _OPENAI_DOCS)

#: The session path asks for ttl="1h" (transports/anthropic_messages.py);
#: every hit or write refreshes it, timed from the start of the request.
_ANTHROPIC_1_HOUR = CacheLifetime(
    CacheLifetimeKind.EXACT,
    60 * 60,
    "https://platform.claude.com/docs/en/build-with-claude/prompt-caching",
)

#: cachePoint is sent without a ttl: five minutes, reset by each cache hit.
_BEDROCK_5_MIN = CacheLifetime(
    CacheLifetimeKind.EXACT,
    5 * 60,
    "https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html",
)

#: Lifetime that applies to every model of a surface.
_SURFACE_LIFETIMES: Mapping[str, CacheLifetime] = {
    "openai": _OPENAI_TYPICAL,
    "anthropic": _ANTHROPIC_1_HOUR,
    "bedrock": _BEDROCK_5_MIN,
}


def _models(lifetime: CacheLifetime, *model_ids: str) -> Dict[str, CacheLifetime]:
    return {model_id: lifetime for model_id in model_ids}


#: Models whose lifetime differs from their surface's.
_MODEL_LIFETIMES: Mapping[str, Mapping[str, CacheLifetime]] = {
    "openai": _models(
        _OPENAI_30_MIN,
        "gpt-5.6",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-6-sol",
        "gpt-6-luna",
        "gpt-6-astra",
        "gpt-6.1-sol",
    ),
}


def cache_lifetime(
    provider: Optional[str], model: Optional[str], auth_mode: str = "api_key"
) -> Optional[CacheLifetime]:
    """The documented prompt-cache lifetime of ``model``, or None if the
    provider documents none."""
    if not provider or not model:
        return None
    surface = reasoning_surface(provider, auth_mode)
    by_model = _MODEL_LIFETIMES.get(surface, {}).get(model)
    if by_model is not None:
        return by_model
    return _SURFACE_LIFETIMES.get(surface)
