# -*- coding: utf-8 -*-
"""The reasoning menu's context and cache readout, and the KV cache under it.

The readout shows a session's last decision request (its input against the
budget, how much the provider served from cache, and how long that cache
lasts). These tests pin:

* recording it changes nothing that is sent (payloads stay byte-identical);
* it is recorded on the session path and on the stateless path alike;
* every provider whose profile accumulates a session history takes the
  session path (a hand-kept subset of them used to re-send the whole prompt
  every turn);
* cache lifetimes exist only where the provider documents one;
* the wire payloads the menu reads.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from agent_core.core.impl.llm import LLMCallType
from agent_core.core.models.cache_lifetime import CacheLifetimeKind, cache_lifetime
from agent_core.core.models.catalog import ModelLimits
from agent_core.core.models.reasoning import ReasoningChoice
from agent_core.core.models.registry import session_cc_providers

from .golden.conftest import build_interface

ACTION = LLMCallType.ACTION_SELECTION


# ──────────────────────── recording the last request ────────────────────────


def test_the_usage_key_changes_nothing_sent(monkeypatch):
    iface, recorder = build_interface(monkeypatch, "openai", "gpt-6-luna")
    asyncio.run(iface.generate_response_async("SYSTEM", "USER"))
    asyncio.run(
        iface.generate_response_async("SYSTEM", "USER", usage_key=f"s1:{ACTION}")
    )
    first, second = recorder.calls
    assert first["payload"] == second["payload"]


def test_a_stateless_decision_records_its_request(monkeypatch):
    iface, _ = build_interface(monkeypatch, "openai", "gpt-6-luna")
    asyncio.run(iface.generate_response_async("SYSTEM", "USER"))
    assert iface.last_request("s1", ACTION) is None  # no key: not a decision

    before = time.time()
    asyncio.run(
        iface.generate_response_async("SYSTEM", "USER", usage_key=f"s1:{ACTION}")
    )
    usage = iface.last_request("s1", ACTION)
    assert usage is not None
    assert (usage.input_tokens, usage.cached_tokens) == (100, 0)
    assert (usage.surface, usage.model) == ("openai", "gpt-6-luna")
    assert before <= usage.started_at <= time.time()


def test_a_session_decision_records_its_request(monkeypatch):
    iface, _ = build_interface(monkeypatch, "openai", "gpt-6-luna")
    iface.create_session_cache("s1", ACTION, "SYSTEM")
    iface.generate_response_with_session("s1", ACTION, "USER", log_response=False)
    usage = iface.last_request("s1", ACTION)
    assert usage is not None and usage.input_tokens == 100


def test_the_record_outlives_a_fold_but_not_the_session(monkeypatch):
    iface, _ = build_interface(monkeypatch, "openai", "gpt-6-luna")
    iface.create_session_cache("s1", ACTION, "SYSTEM")
    iface.generate_response_with_session("s1", ACTION, "USER", log_response=False)
    iface.reset_session_history("s1", ACTION)  # a fold
    assert iface.last_request("s1", ACTION) is not None
    iface.end_all_session_caches("s1")
    assert iface.last_request("s1", ACTION) is None


# ─────────────────────────── the session path ───────────────────────────


@pytest.mark.parametrize("provider", sorted(session_cc_providers()))
def test_every_accumulating_provider_takes_the_session_path(monkeypatch, provider):
    iface, _ = build_interface(monkeypatch, provider, "some-model")
    iface.create_session_cache("s1", ACTION, "SYSTEM")
    assert iface.has_session_cache("s1", ACTION)


# ─────────────────────────── cache lifetimes ───────────────────────────


def test_cache_lifetimes_exist_only_where_documented():
    luna = cache_lifetime("openai", "gpt-6-luna")
    assert luna.kind is CacheLifetimeKind.EXACT and luna.seconds == 30 * 60
    older = cache_lifetime("openai", "gpt-5.2-2025-12-11")
    assert older.kind is CacheLifetimeKind.TYPICAL and older.seconds == 5 * 60
    claude = cache_lifetime("anthropic", "claude-sonnet-4-6")
    assert claude.kind is CacheLifetimeKind.EXACT and claude.seconds == 60 * 60
    # Undocumented or best-effort: no countdown.
    assert cache_lifetime("openai", "gpt-6.1-sol", "subscription") is None
    assert cache_lifetime("grok", "grok-4.3") is None
    assert cache_lifetime("deepseek", "deepseek-v4-pro") is None
    assert cache_lifetime("openrouter", "openai/gpt-6.1-sol") is None
    assert cache_lifetime("lmstudio", "openai/gpt-oss-20b") is None


def test_the_gpt_5_6_rows_name_real_models():
    from agent_core.core.models.cache_lifetime import _MODEL_LIFETIMES
    from agent_core.core.models.catalog import model_limits

    for model in _MODEL_LIFETIMES["openai"]:
        assert model_limits("openai", model) is not None, model


def test_bedrock_counts_down_only_for_models_that_take_cache_points(monkeypatch):
    claude, _ = build_interface(
        monkeypatch, "bedrock", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    )
    assert claude.prompt_cache_lifetime().seconds == 5 * 60
    llama, _ = build_interface(monkeypatch, "bedrock", "meta.llama3-70b-instruct-v1:0")
    assert llama.prompt_cache_lifetime() is None


def test_providers_that_never_report_cached_tokens_say_so(monkeypatch):
    fireworks, _ = build_interface(monkeypatch, "fireworks", "some-model")
    openai, _ = build_interface(monkeypatch, "openai", "gpt-6-luna")
    assert fireworks.reports_cached_tokens() is False
    assert openai.reports_cached_tokens() is True


# ─────────────────────────── budgets per choice ───────────────────────────


def test_each_choice_has_its_own_budget(monkeypatch):
    import agent_core.core.impl.llm.interface as interface_module

    iface, _ = build_interface(monkeypatch, "openai", "gpt-6-luna")
    iface.max_tokens = 8000
    monkeypatch.setattr(
        interface_module, "model_limits", lambda *a: ModelLimits(200_000)
    )
    assert iface.input_budget_for(ReasoningChoice.OFF) == 192_000
    assert iface.input_budget_for(ReasoningChoice.HIGH) == 168_000
    assert iface.input_budget_for(ReasoningChoice.XHIGH) == 136_000


# ─────────────────────────── wire payloads ───────────────────────────


def _adapter(llm, sent):
    async def send_to(_ws, message):
        sent.append(message)

    return SimpleNamespace(
        _controller=SimpleNamespace(agent=SimpleNamespace(llm=llm)),
        _send_to=send_to,
    )


def test_the_reasoning_options_carry_budgets_and_cache_facts(monkeypatch):
    from app.ui_layer.adapters.browser_adapter import BrowserAdapter

    iface, _ = build_interface(monkeypatch, "openai", "gpt-6-luna")
    sent = []
    asyncio.run(
        BrowserAdapter._handle_reasoning_options_get(_adapter(iface, sent), None)
    )
    data = sent[0]["data"]
    assert data["surface"] == "openai"
    assert set(data["inputBudgets"]) == {choice.value for choice in ReasoningChoice}
    assert data["cacheLifetime"] == {"kind": "exact", "seconds": 1800}
    assert data["reportsCachedTokens"] is True


def test_the_session_info_carries_the_last_request(monkeypatch):
    from agent_core.core.session import Session
    from app.ui_layer.adapters.browser_adapter import BrowserAdapter

    iface, _ = build_interface(monkeypatch, "openai", "gpt-6-luna")
    adapter = _adapter(iface, [])
    session = Session.from_dict({"id": "s1"})
    assert BrowserAdapter._session_info(adapter, session)["lastRequest"] is None

    asyncio.run(
        iface.generate_response_async("SYSTEM", "USER", usage_key=f"s1:{ACTION}")
    )
    last = BrowserAdapter._session_info(adapter, session)["lastRequest"]
    assert last["inputTokens"] == 100 and last["cachedTokens"] == 0
    assert last["surface"] == "openai" and last["model"] == "gpt-6-luna"
    assert abs(last["startedAt"] - time.time() * 1000) < 60_000
