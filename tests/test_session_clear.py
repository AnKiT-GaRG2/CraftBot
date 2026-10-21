# -*- coding: utf-8 -*-
"""/clear starts a session over as a new session.

It used to clear only the conversation on screen plus the event stream and
todos: the session kept its loaded skills, token totals, title, NOTE.md and
EVENT.md (both named in every prompt), the LLM interface kept its history
(the cleanup called a method that does not exist), and a run in flight
carried on. These tests pin what a clear resets and what it must not touch:
other sessions' streams and caches shared between sessions.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_core.core.impl.event_stream.manager import EventStreamManager
from agent_core.core.impl.llm import LLMCallType
from agent_core.core.impl.llm.interface import LLMInterface, RequestUsage
from agent_core.core.impl.session.manager import SessionManager
from agent_core.core.session import MAIN_SESSION_ID, Session, SessionType
from agent_core.core.state.session import StateSession
from app.models.factory import ModelFactory

ACTION = LLMCallType.ACTION_SELECTION


def _llm(provider="grok", model="grok-4.3"):
    ctx = {
        "provider": provider,
        "model": model,
        "client": object(),
        "gemini_client": None,
        "remote_url": None,
        "byteplus": None,
        "anthropic_client": None,
        "bedrock_client": None,
        "initialized": True,
        "auth_mode": "api_key",
    }
    with patch.object(ModelFactory, "create", return_value=ctx):
        return LLMInterface(provider=provider, model=model, api_key="k", base_url="")


def _manager(tmp_path, llm=None, own_streams=True):
    streams = EventStreamManager(llm=None)
    manager = SessionManager(
        event_stream_manager=streams,
        llm_interface=llm,
        context_engine=SimpleNamespace(make_prompt=lambda **kw: ("SYSTEM", "")),
        workspace_root=tmp_path,
        on_stream_create=(
            (lambda sid, wd: streams.create_stream(sid, wd)) if own_streams else None
        ),
    )
    return manager, streams


def _talked(manager, streams, llm, session):
    """A session mid-conversation: events, todos, a skill, LLM history."""
    stream = streams.get_stream_by_id(session.id)
    stream.log("action_end", "old work " + "x " * 50)
    stream.head_summary = "SUMMARY OF OLD WORK"
    manager.update_todos(session.id, [{"content": "Collect: old", "status": "pending"}])
    session.selected_skills = ["old-skill"]
    session.total_input_tokens = 5000
    session.title = "Weather in Fukuoka"
    if llm is not None:
        key = f"{session.id}:{ACTION}"
        llm._session_histories[key] = [{"role": "user", "content": "old"}]
        llm._last_input_tokens[key] = 5000
        llm._last_requests[key] = RequestUsage(
            5000, 4000, time.time(), "grok", "grok-4.3"
        )


def test_a_cleared_session_starts_over_as_a_new_one(tmp_path):
    llm = _llm()
    manager, streams = _manager(tmp_path, llm)
    session = manager.create_session()
    _talked(manager, streams, llm, session)
    properties_before = StateSession.get(session.id).agent_properties
    try:
        assert manager.clear_session(session.id)

        stream = streams.get_stream_by_id(session.id)
        assert stream.tail_events == [] and stream.head_summary is None
        assert session.todos == [] and session.selected_skills == []
        assert session.total_input_tokens == 0
        assert session.title == "New chat"  # titled again from its next request
        assert StateSession.get(session.id).agent_properties is not properties_before
        # The LLM forgot the conversation but still serves it on the session path.
        key = f"{session.id}:{ACTION}"
        assert key not in llm._session_histories
        assert llm.last_input_tokens(session.id, ACTION) is None
        assert llm.last_request(session.id, ACTION) is None
        assert llm.has_session_cache(session.id, ACTION)
    finally:
        StateSession.end(session.id)


def test_a_clear_keeps_the_sessions_identity(tmp_path):
    manager, streams = _manager(tmp_path)
    session = manager.restore_session(
        Session.from_dict(
            {
                "id": "lui_app",
                "type": SessionType.AGENT_APP,
                "title": "Command Center",
                "agent_app_project_id": "9a07529e",
                "reasoning_effort": "high",
            }
        )
    )
    streams.create_stream(session.id, Path(session.workspace_dir))
    try:
        manager.clear_session(session.id)
        assert session.title == "Command Center"  # only chats are retitled
        assert session.agent_app_project_id == "9a07529e"
        assert session.reasoning_effort == "high"
    finally:
        StateSession.end(session.id)


def test_a_clear_never_touches_another_sessions_stream(tmp_path):
    # get_stream_by_id falls back to the main stream for a session with none.
    manager, streams = _manager(tmp_path, own_streams=False)
    streams.get_main_stream().log("action_end", "main's own work")
    session = manager.create_session()
    try:
        manager.clear_session(session.id)
        assert streams.get_main_stream().tail_events
    finally:
        StateSession.end(session.id)


def test_a_clear_keeps_caches_other_sessions_share(tmp_path):
    # Gemini's explicit cache is keyed by system prompt, shared by every
    # session: deleting it would cost all of them their cached prefix.
    llm = _llm()
    deleted = []
    llm._gemini_cache_manager = SimpleNamespace(
        invalidate_cache=lambda *a: deleted.append(a)
    )
    llm.provider = "gemini"
    llm.create_session_cache("s1", ACTION, "SYSTEM")
    llm.restart_session("s1")
    assert deleted == []


# ─────────────────────────── workspace files ───────────────────────────


def test_a_clear_resets_the_files_named_in_every_prompt(tmp_path):
    from app.session.session_manager import _NOTE_SCRATCHPAD_HEADER
    from app.session.session_manager import SessionManager as AppSessionManager

    streams = EventStreamManager(llm=None)
    manager = AppSessionManager(event_stream_manager=streams)
    manager.workspace_root = tmp_path  # never the real agent workspace
    manager._on_session_persist = None  # never the real session storage
    session = manager.create_session()
    workspace = Path(session.workspace_dir)
    try:
        (workspace / "NOTE.md").write_text("old plan", encoding="utf-8")
        (workspace / "EVENT.md").write_text("old events", encoding="utf-8")
        (workspace / "EVENT_UNPROCESSED.md").write_text("for memory", encoding="utf-8")
        (workspace / "report.pdf").write_bytes(b"user file")

        assert manager.clear_session(session.id)

        assert (workspace / "NOTE.md").read_text(
            encoding="utf-8"
        ) == _NOTE_SCRATCHPAD_HEADER
        assert (workspace / "EVENT.md").read_text(encoding="utf-8") == ""
        # Long-term memory still learns from the cleared conversation.
        assert (workspace / "EVENT_UNPROCESSED.md").read_text(
            encoding="utf-8"
        ) == "for memory"
        assert (workspace / "report.pdf").read_bytes() == b"user file"
    finally:
        StateSession.end(session.id)


# ─────────────────────────── the agent entry ───────────────────────────


def test_a_run_in_flight_is_stopped_before_the_clear_and_the_reset_is_saved():
    from app.agent_base import AgentBase

    calls = []

    async def request_run_stop(session_id):
        calls.append(("stop", session_id))
        return True

    agent = SimpleNamespace(
        busy_sessions={"s1"},
        request_run_stop=request_run_stop,
        session_manager=SimpleNamespace(
            clear_session=lambda session_id: calls.append(("clear", session_id)) or True
        ),
        # The saved LLM state goes too, or a restart would bring it back.
        _persist_llm_session=lambda session_id: calls.append(("save", session_id)),
    )
    assert asyncio.run(AgentBase.clear_session(agent, "s1"))
    assert calls == [("stop", "s1"), ("clear", "s1"), ("save", "s1")]

    calls.clear()
    agent.busy_sessions = set()
    asyncio.run(AgentBase.clear_session(agent, "s1"))
    assert calls == [("clear", "s1"), ("save", "s1")]  # idle: nothing to stop


def test_the_main_session_keeps_its_title(tmp_path):
    manager, _ = _manager(tmp_path)
    main = manager.ensure_main()
    try:
        manager.clear_session(MAIN_SESSION_ID)
        assert main.title == "Main"
    finally:
        StateSession.end(MAIN_SESSION_ID)
