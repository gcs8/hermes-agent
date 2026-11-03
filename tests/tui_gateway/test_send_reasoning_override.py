"""Session effort survives Send, child reuse and legacy model adoption.

Real gateway and AIAgent switch implementations, with synthetic config, clients,
transport and persistence. No provider request or application startup is needed.
"""
from __future__ import annotations

import copy
import io
import threading
from types import SimpleNamespace

import pytest

from hermes_constants import parse_reasoning_effort, resolve_reasoning_config
from run_agent import AIAgent
from tui_gateway import server
from tui_gateway.compute_host import ComputeHost


def _agent(model="old"):
    agent = AIAgent.__new__(AIAgent)
    agent.model, agent.provider, agent.requested_provider = model, "openai", "openai"
    agent.base_url, agent.api_key = "https://synthetic.invalid/v1", "synthetic-key"
    agent.api_mode = "chat_completions"
    agent.reasoning_config = parse_reasoning_effort("high")
    agent._client_kwargs = {}
    agent.context_compressor = None
    agent._session_db = None
    agent._primary_runtime = None
    agent._anthropic_prompt_cache_policy = lambda **kw: (False, False)
    return agent


def _session(agent=None, **extra):
    return dict(agent=agent, session_key="key", history=[], history_version=0,
                history_lock=threading.Lock(), running=False, attached_images=[],
                cwd="/synthetic", cols=80, **extra)


@pytest.fixture
def harness(monkeypatch):
    cfg = {"agent": {"reasoning_effort": "high", "reasoning_overrides": {"new": "low"}}}
    persisted, events, writes = [], [], []
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: cfg)
    monkeypatch.setattr(server, "_load_cfg", lambda: cfg)
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda *a, **kw: None)
    # Transport construction is the only replaced part of AIAgent.switch_model.
    monkeypatch.setattr("agent.agent_runtime_helpers._build_switched_client", lambda *a: None)
    monkeypatch.setattr(server, "_merge_preflight_warning", lambda *a: None)
    monkeypatch.setattr(server, "_expensive_model_confirm", lambda *a: None)
    monkeypatch.setattr("hermes_cli.observability.shared_metrics_events.record_model_switch", lambda **kw: None)
    monkeypatch.setattr("hermes_cli.model_switch.persist_model_selection", lambda r: None)

    def resolve(**kw):
        return SimpleNamespace(success=True, new_model=kw["raw_input"], target_provider="openai",
                               base_url="https://synthetic.invalid/v1", api_key="synthetic-key",
                               api_mode="chat_completions", warning_message="", model_info=None,
                               runtime_capabilities={"synthetic_destination": True})

    monkeypatch.setattr("hermes_cli.model_switch.switch_model", resolve)
    for name in ("_restart_slash_worker", "_persist_live_session_system_prompt", "_append_model_switch_marker",
                 "_ensure_session_db_row", "_persist_branch_seed", "_install_borrowed_lease"):
        monkeypatch.setattr(server, name, lambda *a, **kw: None)
    monkeypatch.setattr(server, "_persist_live_session_runtime",
                        lambda s: persisted.append(copy.deepcopy(server._runtime_model_config(s["agent"]))))
    monkeypatch.setattr(server, "_emit", lambda kind, sid, payload: events.append((kind, copy.deepcopy(payload))))
    monkeypatch.setattr(server, "_write_config_key", lambda k, v: writes.append((k, v)))
    return SimpleNamespace(cfg=cfg, persisted=persisted, events=events, writes=writes)


def _assert_published(harness, effort):
    assert harness.persisted
    assert all(row["reasoning_config"] == effort for row in harness.persisted)
    infos = [payload for kind, payload in harness.events if kind == "session.info"]
    assert infos
    expected = "none" if effort == {"enabled": False} else effort["effort"] if effort else ""
    assert all(info["reasoning_effort"] == expected for info in infos)


@pytest.mark.parametrize("target", ["old", "new"])
@pytest.mark.parametrize("effort", ["high", "xhigh", "none"])
@pytest.mark.parametrize("queued", [False, True])
def test_session_effort_precedes_defaults_before_switch_publication(harness, target, effort, queued):
    agent = _agent()
    chosen = parse_reasoning_effort(effort)
    agent.reasoning_config = copy.deepcopy(chosen)
    session = _session(agent, create_reasoning_override=chosen)
    raw = f"{target} --provider openai --session"
    if queued:
        session["pending_model_switch"] = {"raw": raw}
        server._apply_pending_model_switch("sid", session)
        assert "pending_model_switch" not in session
    else:
        server._apply_model_switch("sid", session, raw)
    assert agent.reasoning_config == chosen
    assert agent._primary_runtime["reasoning_config"] == chosen
    assert agent.runtime_capabilities == {"synthetic_destination": True}
    assert session["create_reasoning_override"] == chosen
    assert harness.writes == []
    _assert_published(harness, chosen)


@pytest.mark.parametrize("override", ["absent", None])
@pytest.mark.parametrize("target, expected", [("new", "low"), ("old", "high")])
def test_unpinned_switch_keeps_destination_profile_policy(harness, override, target, expected):
    agent = _agent()
    agent.reasoning_config = parse_reasoning_effort("xhigh")  # live value alone is not a pin
    session = _session(agent)
    if override != "absent":
        session["create_reasoning_override"] = override
    server._apply_model_switch("sid", session, f"{target} --provider openai --session")
    _assert_published(harness, parse_reasoning_effort(expected))


@pytest.mark.parametrize("scope", ["session", "global", "once"])
@pytest.mark.parametrize("effort", ["xhigh", "none"])
def test_explicit_switch_effort_wins_before_any_publication(harness, scope, effort):
    agent = _agent()
    saved = parse_reasoning_effort("high")
    session = _session(agent, create_reasoning_override=copy.deepcopy(saved))
    server._apply_model_switch("sid", session, f"new --provider openai --reasoning {effort} --{scope}")
    selected = parse_reasoning_effort(effort)
    assert agent.reasoning_config == selected
    assert agent._primary_runtime["reasoning_config"] == selected
    _assert_published(harness, selected)
    if scope == "once":
        assert session["create_reasoning_override"] == saved
        server._restore_agent_model_runtime(agent, session.pop("one_turn_model_restore"))
        assert agent.model == "old"
        assert agent.reasoning_config == saved
    elif scope == "global":
        assert "create_reasoning_override" not in session
        assert harness.writes == [("agent.reasoning_effort", effort)]
    else:
        assert session["create_reasoning_override"] == selected
        assert harness.writes == []


def test_once_without_effort_preserves_session_pick_and_restores(harness):
    agent = _agent()
    chosen = parse_reasoning_effort("xhigh")
    agent.reasoning_config = copy.deepcopy(chosen)
    session = _session(agent, create_reasoning_override=chosen)
    server._apply_model_switch("sid", session, "new --provider openai --once")
    _assert_published(harness, chosen)
    server._restore_agent_model_runtime(agent, session.pop("one_turn_model_restore"))
    assert agent.model == "old"
    assert agent.reasoning_config == chosen
    assert session["create_reasoning_override"] == chosen


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("shadow", [False, True])
@pytest.mark.parametrize("effort", ["high", "xhigh", "none", None])
def test_send_carries_effort_to_fresh_and_reused_child(harness, monkeypatch, fresh, shadow, effort):
    parent = _session(_agent() if shadow else None, _compute_host_active=True)
    selected = parse_reasoning_effort(effort)
    if effort is not None:
        response = server._set_reasoning("set", {"session_id": "sid"}, "reasoning", effort, parent)
        assert "error" not in response
    frame = server._compute_host_turn_frame("send", "sid", parent, "synthetic prompt")
    assert frame["reasoning_config_override"] == selected
    harness.persisted.clear()
    harness.events.clear()
    child = _session(_agent())
    child_sessions = {} if fresh else {"sid": child}
    monkeypatch.setattr(server, "_sessions", child_sessions)
    builds, turns = [], []

    def make_agent(sid, key, **kwargs):
        builds.append(kwargs)
        agent = _agent()
        agent.reasoning_config = copy.deepcopy(kwargs["reasoning_config_override"] or resolve_reasoning_config(harness.cfg, "old"))
        return agent

    def init_session(sid, key, agent, history, **kwargs):
        child_sessions[sid] = _session(agent)

    def run_prompt(rid, sid, session, text, **kwargs):
        server._apply_pending_model_switch(sid, session)
        turns.append(copy.deepcopy(session["agent"].reasoning_config))
        server._persist_live_session_runtime(session)
        server._emit_session_info(sid, session)
        session["running"] = False

    monkeypatch.setattr(server, "_make_agent", make_agent)
    monkeypatch.setattr(server, "_init_session", init_session)
    monkeypatch.setattr(server, "_run_prompt_submit", run_prompt)
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    try:
        host._run_real_turn(frame)
    finally:
        host.close()
    expected = selected or parse_reasoning_effort("high")
    assert turns == [expected]
    assert len(builds) == int(fresh)
    if selected is not None:
        assert child_sessions["sid"]["create_reasoning_override"] == selected
    else:
        assert child_sessions["sid"].get("create_reasoning_override") is None
    _assert_published(harness, expected)


def test_reused_child_queued_same_model_keeps_frame_effort(harness, monkeypatch):
    child = _session(_agent())
    parent = _session(create_reasoning_override=parse_reasoning_effort("xhigh"),
                      pending_model_switch={"raw": "old --provider openai --session"})
    frame = server._compute_host_turn_frame("send", "sid", parent, "synthetic")
    monkeypatch.setattr(server, "_sessions", {"sid": child})
    observed = []

    def run_prompt(rid, sid, session, text, **kwargs):
        server._apply_pending_model_switch(sid, session)
        observed.append(copy.deepcopy(session["agent"].reasoning_config))
        session["running"] = False

    monkeypatch.setattr(server, "_run_prompt_submit", run_prompt)
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    try:
        host._run_real_turn(frame)
    finally:
        host.close()
    chosen = parse_reasoning_effort("xhigh")
    assert observed == [chosen]
    _assert_published(harness, chosen)


@pytest.mark.parametrize("primary", [False, True])
def test_once_restore_keeps_original_effort_in_recovery_snapshot(harness, primary):
    from agent.primary_runtime_snapshot import _build_primary_runtime_snapshot

    agent = _agent()
    chosen = parse_reasoning_effort("xhigh")
    agent.reasoning_config = copy.deepcopy(chosen)
    agent._use_prompt_caching = agent._use_native_cache_layout = False
    if primary:
        agent._primary_runtime = _build_primary_runtime_snapshot(agent, agent.api_mode)
    session = _session(agent, create_reasoning_override=chosen)
    server._apply_model_switch("sid", session, "new --provider openai --reasoning none --once")
    server._restore_agent_model_runtime(agent, session.pop("one_turn_model_restore"))
    assert agent.reasoning_config == chosen
    assert agent._primary_runtime["reasoning_config"] == chosen
    assert session["create_reasoning_override"] == chosen


@pytest.mark.parametrize("changed", [False, True])
def test_config_sync_preserves_pin_without_global_writes(harness, monkeypatch, changed):
    agent = _agent()
    chosen = parse_reasoning_effort("xhigh")
    agent.reasoning_config = copy.deepcopy(chosen)
    session = _session(agent, create_reasoning_override=chosen)
    monkeypatch.setattr(server, "_config_model_target", lambda: ("new" if changed else "old", "openai"))
    server._sync_agent_model_with_config("sid", session)
    assert agent.reasoning_config == chosen
    assert harness.writes == []
    if changed:
        _assert_published(harness, chosen)
    else:
        assert harness.persisted == []
        assert harness.events == []


@pytest.mark.parametrize("inherit", [False, True])
def test_child_inherit_is_profile_scoped_and_missing_field_is_noop(harness, monkeypatch, tmp_path, inherit):
    from hermes_constants import get_hermes_home, get_hermes_home_override

    profile = tmp_path / "secondary"
    profile.mkdir()
    (profile / "config.yaml").write_text("agent:\n  reasoning_effort: none\n", encoding="utf-8")
    chosen = parse_reasoning_effort("xhigh")
    agent = _agent()
    agent.reasoning_config = copy.deepcopy(chosen)
    child = _session(agent, profile_home=str(profile), create_reasoning_override=chosen)
    monkeypatch.setattr(server, "_sessions", {"sid": child})
    seen = []

    def load_reasoning(model):
        seen.append(str(get_hermes_home()))
        return {"enabled": False}

    monkeypatch.setattr(server, "_load_reasoning_config", load_reasoning)
    before = get_hermes_home_override()
    frame = {"sid": "sid", "text": "synthetic"}
    if inherit:
        frame["reasoning_config_override"] = None
    observed = []

    def run_prompt(rid, sid, session, text, **kwargs):
        observed.append(copy.deepcopy(session["agent"].reasoning_config))
        session["running"] = False

    monkeypatch.setattr(server, "_run_prompt_submit", run_prompt)
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    try:
        host._run_real_turn(frame)
    finally:
        host.close()
    assert observed == [{"enabled": False} if inherit else chosen]
    assert seen == [str(profile)] if inherit else seen == []
    assert get_hermes_home_override() == before
    assert child.get("create_reasoning_override") == (None if inherit else chosen)


def test_failed_swap_never_publishes_or_changes_pin(harness, monkeypatch):
    agent = _agent()
    chosen = parse_reasoning_effort("xhigh")
    agent.reasoning_config = copy.deepcopy(chosen)
    session = _session(agent, create_reasoning_override=chosen)

    def fail_client(*args):
        raise ValueError("synthetic client failure")

    monkeypatch.setattr("agent.agent_runtime_helpers._build_switched_client", fail_client)
    with pytest.raises(ValueError, match="staying on old"):
        server._apply_model_switch("sid", session, "new --provider openai --reasoning none --session")
    assert agent.model == "old"
    assert agent.reasoning_config == chosen
    assert session["create_reasoning_override"] == chosen
    assert harness.persisted == harness.events == harness.writes == []
