"""Stored effort survives real resume registration and isolated turn handoff.

Config, database, agent construction and JSONL transport are synthetic. Resume,
frame construction, child admission/sync and metadata adoption use real imports.
"""
from __future__ import annotations

import copy
import io
import json
import threading
from types import SimpleNamespace

import pytest

from hermes_constants import get_hermes_home, parse_reasoning_effort, resolve_reasoning_config
from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.compute_host import ComputeHost


@pytest.fixture
def resume_harness(monkeypatch, tmp_path):
    launch, secondary = tmp_path / "launch", tmp_path / "secondary"
    for home in (launch, secondary):
        home.mkdir()
        (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    configs = {
        str(launch): {"model": {"default": "saved", "provider": "openai"},
                      "agent": {"reasoning_effort": "high"}},
        str(secondary): {"model": {"default": "saved", "provider": "openai"},
                         "agent": {"reasoning_effort": "low", "reasoning_overrides": {"saved": "none"}}},
    }
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setattr(server, "_hermes_home", str(launch))
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_profile_home", lambda p: secondary if p == "secondary" else None)
    cfg = lambda: configs[str(get_hermes_home())]
    monkeypatch.setattr(server, "_load_cfg", cfg)
    monkeypatch.setattr("hermes_cli.config.load_config", cfg)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", cfg)
    db = SessionDB(tmp_path / "synthetic.db")
    monkeypatch.setattr(server, "_profile_session_db", lambda home: (db, False))
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr("hermes_state_registry.acquire", lambda *a, **kw: db)
    monkeypatch.setattr("hermes_state_registry.release_or_close", lambda *a, **kw: None)
    monkeypatch.setattr(server, "route_supported_efforts", lambda *a, **kw: None)
    monkeypatch.setattr("hermes_cli.env_loader.hydrate_profile_secret_sources", lambda *a: None)
    monkeypatch.setattr(server, "_default_session_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(server, "_profile_default_cwd", lambda home: str(tmp_path))
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {"turn_isolation": True})
    ensure_row = server._ensure_session_db_row
    for name in ("_enable_gateway_prompts", "_schedule_agent_build", "_schedule_resume_hydration",
                 "_schedule_session_cap_enforcement", "_maybe_schedule_auto_continue",
                 "_ensure_session_db_row", "_persist_branch_seed", "_install_borrowed_lease",
                 "_emit", "_register_session_cwd"):
        monkeypatch.setattr(server, name, lambda *a, **kw: None)
    builds, observed = [], []

    def make_agent(sid, key, **kw):
        builds.append(copy.deepcopy(kw.get("reasoning_config_override")))
        reasoning = kw.get("reasoning_config_override")
        if reasoning is None:
            reasoning = resolve_reasoning_config(cfg(), "saved")
        return SimpleNamespace(model="saved", provider="openai", requested_provider="openai",
                               api_mode="chat_completions", base_url="https://synthetic.invalid/v1",
                               reasoning_config=copy.deepcopy(reasoning), session_id=key,
                               _session_db=None, close=lambda: None)

    def init_session(sid, key, agent, history, **kw):
        server._sessions[sid] = dict(agent=agent, session_key=key, history=history,
                                     history_lock=threading.Lock(), history_version=0,
                                     running=False, attached_images=[], cols=80,
                                     cwd=str(tmp_path), created_at=1, source="desktop")

    def run_prompt(rid, sid, session, text, **kw):
        observed.append(copy.deepcopy(session["agent"].reasoning_config))
        session["running"] = False

    monkeypatch.setattr(server, "_make_agent", make_agent)
    monkeypatch.setattr(server, "_make_agent_in_context", make_agent)
    monkeypatch.setattr(server, "_init_session", init_session)
    monkeypatch.setattr(server, "_transfer_db_to_agent", lambda *a: True)
    monkeypatch.setattr(server, "_run_prompt_submit", run_prompt)
    try:
        yield SimpleNamespace(db=db, configs=configs, launch=launch, secondary=secondary,
                              builds=builds, observed=observed, ensure_row=ensure_row)
    finally:
        db.close()


def _resume(h, path, effort="xhigh", profile=None, follows=False, *, explicit=True):
    model_config: dict = {"provider": "openai"}
    if effort != "missing":
        model_config["reasoning_config"] = None if effort is None else (
            {} if effort == "empty" else parse_reasoning_effort(effort))
    if explicit and isinstance(model_config.get("reasoning_config"), dict):
        model_config["reasoning_config_override"] = copy.deepcopy(model_config["reasoning_config"])
    if follows:
        model_config["follow_profile_config"] = True
    h.db.create_session("stored", source="desktop", model="saved", model_config=model_config)
    return _resume_stored(h, path, profile)


def _resume_stored(h, path, profile=None, stored="stored"):
    response = server.handle_request({"id": "resume", "method": "session.resume", "params": {
        "session_id": stored, "source": "desktop", "profile": profile,
        "defer_history": path == "deferred", "eager_build": path == "eager"}})
    assert "error" not in response, response
    sid = response["result"]["session_id"]
    return sid, server._sessions[sid], response["result"]["info"]


def _send_twice(h, monkeypatch, sid, parent):
    output = io.StringIO()
    host = ComputeHost(stdout=output, heartbeat_secs=0)
    children, frames = {}, []
    try:
        for index in range(2):
            frame = server._compute_host_turn_frame(str(index), sid, parent, "synthetic prompt")
            frames.append(frame)
            output.seek(0)
            output.truncate()
            with monkeypatch.context() as m:
                m.setattr(server, "_sessions", children)
                host._run_real_turn(json.loads(json.dumps(frame)))
            reply = json.loads(output.getvalue().splitlines()[-1])
            assert reply["type"] == "turn.end", reply
            server._apply_compute_host_metadata_mirror(parent, reply)
        return frames
    finally:
        host.close()


@pytest.mark.parametrize("path", ["cold", "deferred", "eager"])
def test_effective_runtime_writer_does_not_manufacture_a_pin(resume_harness, monkeypatch, path):
    h = resume_harness
    agent = server._make_agent("original", "stored")
    agent._session_db = h.db
    agent._session_init_model_config = {"reasoning_config": parse_reasoning_effort("high")}
    server._init_session("original", "stored", agent, [])
    original = server._sessions["original"]
    assert h.ensure_row(original)
    server._persist_live_session_runtime(original)
    row = json.loads(h.db.get_session("stored")["model_config"])
    assert row["reasoning_config"] == parse_reasoning_effort("high")
    assert "reasoning_config_override" not in row
    server._sessions.clear()
    h.configs[str(h.launch)]["agent"]["reasoning_effort"] = "medium"
    sid, parent, _info = _resume_stored(h, path)
    _send_twice(h, monkeypatch, sid, parent)
    assert h.observed == [parse_reasoning_effort("medium")] * 2
    assert parent.get("create_reasoning_override") is None


@pytest.mark.parametrize("path", ["cold", "deferred", "eager"])
@pytest.mark.parametrize("effort", ["xhigh", "high", "none", "empty", "missing", None])
@pytest.mark.parametrize("profile", [None, "secondary"])
def test_resume_effort_reaches_first_and_second_isolated_send(resume_harness, monkeypatch, path, effort, profile):
    h = resume_harness
    sid, parent, _info = _resume(h, path, effort, profile)
    selected = {} if effort == "empty" else parse_reasoning_effort(effort)
    config = h.configs[str(h.secondary if profile else h.launch)]
    expected = resolve_reasoning_config(config, "saved") if selected is None else selected
    frames = _send_twice(h, monkeypatch, sid, parent)
    assert h.observed == [expected, expected]
    assert all(frame["reasoning_config_override"] == selected for frame in frames)
    assert parent.get("create_reasoning_override") == selected


@pytest.mark.parametrize("path", ["cold", "deferred", "eager"])
@pytest.mark.parametrize("effort,expected", [("xhigh", "xhigh"), ("none", "none"), ("empty", ""),
                                            ("missing", "high"), (None, "high")])
def test_immediate_resume_info_reports_effective_effort(resume_harness, path, effort, expected):
    _sid, _parent, info = _resume(resume_harness, path, effort)
    assert info.get("reasoning_effort") == expected


@pytest.mark.parametrize("path", ["cold", "deferred", "eager"])
@pytest.mark.parametrize("newer", ["high", "none", "inherit"])
def test_new_parent_intent_beats_resumed_effort(resume_harness, monkeypatch, path, newer):
    h = resume_harness
    sid, parent, _info = _resume(h, path)
    selected = None if newer == "inherit" else parse_reasoning_effort(newer)
    if newer == "inherit":
        server._set_session_reasoning_override(parent, None)
    else:
        response = server._set_reasoning("pick", {"session_id": sid}, "reasoning", newer, parent)
        assert "error" not in response, response
    frames = _send_twice(h, monkeypatch, sid, parent)
    assert all(frame["reasoning_config_override"] == selected for frame in frames)
    assert h.observed == [parse_reasoning_effort("high") if selected is None else selected] * 2


@pytest.mark.parametrize("path", ["cold", "deferred", "eager"])
def test_profile_following_resume_does_not_pin_stored_effort(resume_harness, monkeypatch, path):
    h = resume_harness
    sid, parent, _info = _resume(h, path, profile="secondary", follows=True)
    assert parent.get("create_reasoning_override") is None
    _send_twice(h, monkeypatch, sid, parent)
    assert h.observed == [{"enabled": False}] * 2
    assert parent.get("create_reasoning_override") is None


@pytest.mark.parametrize("path", ["cold", "deferred"])
@pytest.mark.parametrize("newer", ["unchanged", "none", "inherit"])
def test_nonisolated_deferred_build_respects_current_resume_intent(resume_harness, path, newer):
    h = resume_harness
    _sid, parent, _info = _resume(h, path)
    if newer != "unchanged":
        server._set_session_reasoning_override(parent, None if newer == "inherit" else parse_reasoning_effort(newer))
    kwargs = server._deferred_build_agent_kwargs(parent, h.db)
    selected = parse_reasoning_effort("xhigh" if newer == "unchanged" else newer)
    assert kwargs.get("reasoning_config_override") == selected
    with server._profile_build_scope(parent.get("profile_home")):
        agent = server._make_agent("sid", "stored", **kwargs)
    assert agent.reasoning_config == (parse_reasoning_effort("high") if selected is None else selected)


@pytest.mark.parametrize("path", ["cold", "deferred", "eager"])
@pytest.mark.parametrize("per_model", [False, True])
def test_unpinned_resume_metadata_uses_owning_profile_policy(resume_harness, path, per_model):
    h = resume_harness
    if not per_model:
        h.configs[str(h.secondary)]["agent"].pop("reasoning_overrides")
    _sid, parent, info = _resume(h, path, "missing", "secondary")
    assert info.get("reasoning_effort") == ("none" if per_model else "low")
    assert parent.get("create_reasoning_override") is None


@pytest.mark.parametrize("path", ["cold", "deferred", "eager"])
def test_inherited_snapshot_follows_edited_profile_on_two_sends(resume_harness, monkeypatch, path):
    h = resume_harness
    h.configs[str(h.launch)]["agent"]["reasoning_effort"] = "medium"
    sid, parent, _info = _resume(h, path, "high", explicit=False)
    _send_twice(h, monkeypatch, sid, parent)
    assert h.observed == [parse_reasoning_effort("medium")] * 2
    assert parent.get("create_reasoning_override") is None


@pytest.mark.parametrize("path", ["deferred", "eager"])
def test_inherited_snapshot_uses_real_destination_model_policy(resume_harness, monkeypatch, path):
    from tests.tui_gateway.test_send_reasoning_override import _agent

    h = resume_harness
    h.configs[str(h.launch)]["agent"]["reasoning_overrides"] = {"other": "low"}
    _sid, parent, _info = _resume(h, path, "high", explicit=False)
    agent = _agent("saved")
    parent["agent"] = agent
    if path == "deferred":
        kwargs = server._deferred_build_agent_kwargs(parent, h.db)
        agent.reasoning_config = kwargs.get("reasoning_config_override") or parse_reasoning_effort("high")
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda *a, **kw: None)
    monkeypatch.setattr("agent.agent_runtime_helpers._build_switched_client", lambda *a: None)
    for name in ("_restart_slash_worker", "_persist_live_session_system_prompt", "_append_model_switch_marker",
                 "_emit_session_info"):
        monkeypatch.setattr(server, name, lambda *a, **kw: None)
    result = SimpleNamespace(new_model="other", target_provider="openai", api_key="synthetic-key",
                             base_url="https://synthetic.invalid/v1", api_mode="chat_completions",
                             runtime_capabilities={})
    server._commit_agent_switch("sid", parent, agent, result, "saved", None)
    assert agent.model == "other"
    assert agent.reasoning_config == parse_reasoning_effort("low")
    assert parent.get("create_reasoning_override") is None


@pytest.mark.parametrize("overrides", [None, {"model_override": {"model": "saved"}}])
def test_resume_metadata_never_hydrates_external_secrets(resume_harness, monkeypatch, overrides):
    calls = []
    monkeypatch.setattr("hermes_cli.env_loader.hydrate_profile_secret_sources", lambda *a: calls.append(a))
    kwargs = {} if overrides is None else {"runtime_overrides": overrides}
    info = server._lazy_resume_info(str(resume_harness.secondary), profile="secondary", **kwargs)
    if overrides is not None:
        assert info["reasoning_effort"] == "none"
    assert calls == []
