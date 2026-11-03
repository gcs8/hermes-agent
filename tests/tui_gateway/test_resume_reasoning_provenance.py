"""Explicit intent is durable; effective defaults are not evidence of intent."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hermes_constants import parse_reasoning_effort
from tui_gateway import server
from tests.tui_gateway import test_resume_reasoning_handoff as resume_support

_resume_stored = resume_support._resume_stored
_send_twice = resume_support._send_twice
resume_harness = resume_support.resume_harness


@pytest.mark.parametrize("path", ["cold", "deferred", "eager"])
@pytest.mark.parametrize("effort", ["xhigh", "high", "none"])
@pytest.mark.parametrize("existing", [False, True])
def test_accepted_pick_persists_and_resumes_without_live_record(resume_harness, monkeypatch, path, effort, existing):
    h = resume_harness
    agent = server._make_agent("original", "stored")
    agent._session_db = h.db
    agent._session_init_model_config = {"reasoning_config": parse_reasoning_effort("high")}
    server._init_session("original", "stored", agent, [])
    original = server._sessions["original"]
    if existing:
        h.db.create_session("stored", source="desktop", model="saved", model_config={"provider": "openai"})
    response = server.handle_request({"id": "pick", "method": "config.set", "params": {
        "session_id": "original", "key": "reasoning", "value": effort}})
    assert "error" not in response, response
    # The production first-row path is needed when the picker precedes the first Send.
    if not existing:
        assert h.ensure_row(original)
        first_row = json.loads(h.db.get_session("stored")["model_config"])
        assert first_row["reasoning_config_override"] == parse_reasoning_effort(effort)
    server._persist_live_session_runtime(original)
    selected = parse_reasoning_effort(effort)
    row = json.loads(h.db.get_session("stored")["model_config"])
    assert row["reasoning_config_override"] == selected
    assert agent._session_init_model_config["reasoning_config_override"] == selected
    server._sessions.clear()  # No retained session, override, agent or frame can prove the resume pin.
    h.configs[str(h.launch)]["agent"]["reasoning_effort"] = "medium"
    sid, parent, info = _resume_stored(h, path)
    assert info["reasoning_effort"] == effort
    _send_twice(h, monkeypatch, sid, parent)
    assert h.observed == [selected] * 2
    assert parent["create_reasoning_override"] == selected


@pytest.mark.parametrize("effort", ["high", "none", None])
def test_first_build_seeds_compression_marker_before_runtime_persist(monkeypatch, tmp_path, effort):
    selected = parse_reasoning_effort(effort)
    fake = MagicMock()
    fake._session_init_model_config = {"reasoning_config": parse_reasoning_effort("low")}
    cfg = {"model": {"default": "saved", "provider": "openai"}, "agent": {"system_prompt": "synthetic"}}
    monkeypatch.setattr(server, "_load_cfg", lambda: cfg)
    monkeypatch.setattr(server, "_get_db", lambda: None)
    monkeypatch.setattr(server, "_load_reasoning_config", lambda *a: parse_reasoning_effort("low"))
    monkeypatch.setattr(server, "_load_enabled_toolsets", lambda *a: [])
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda **kw: {
        "provider": "openai", "base_url": "https://synthetic.invalid/v1", "api_key": "synthetic-key",
        "api_mode": "chat_completions"})
    monkeypatch.setattr("run_agent.AIAgent", lambda **kw: fake)
    agent = server._make_agent("first", "stored", reasoning_config_override=selected)
    assert agent is fake
    assert fake._session_init_model_config.get("reasoning_config_override") == selected
    if selected is None:
        assert "reasoning_config_override" not in fake._session_init_model_config


@pytest.mark.parametrize("release", [False, True])
@pytest.mark.parametrize("effort", ["high", "none"])
def test_persist_compress_resume_keeps_only_explicit_intent(resume_harness, monkeypatch, release, effort):
    from agent import conversation_compression as cc

    h = resume_harness
    selected = parse_reasoning_effort(effort)  # High is explicit even though it equals the default.
    agent = server._make_agent("original", "stored")
    agent._session_db = h.db
    agent._session_init_model_config = {"reasoning_config": copy.deepcopy(selected)}
    agent.platform = "desktop"
    agent.working_directory = None
    agent._memory_manager = None
    agent.context_compressor = SimpleNamespace()
    agent._flush_messages_to_session_db = lambda *a, **kw: None
    agent._persist_user_message_idx = None
    agent._session_messages = None
    agent._gateway_session_key = None
    agent._cached_system_prompt = "stable prompt"
    h.db.create_session("stored", source="desktop", model="saved")
    h.db.append_message("stored", "user", "synthetic")
    server._init_session("original", "stored", agent, [])
    original = server._sessions["original"]
    response = server.handle_request({"id": "pick", "method": "config.set", "params": {
        "session_id": "original", "key": "reasoning", "value": effort}})
    assert "error" not in response, response
    if release:
        server._set_session_reasoning_override(original, None)
        server._persist_live_session_runtime(original)
    cc._publish_rotated_compaction(
        agent, [{"role": "user", "content": "synthetic"}], [{"role": "user", "content": "[handoff]"}],
        new_system_prompt="stable prompt", lease=SimpleNamespace(holder=None, ttl=60.0, watermark=None),
        old_session_id="stored", compressed_user_turn_outcome="none")
    child_id = agent.session_id
    assert child_id != "stored"
    child = json.loads(h.db.get_session(child_id)["model_config"])
    assert child.get("reasoning_config_override") == (None if release else selected)
    if release:
        assert "reasoning_config_override" not in child
    server._sessions.clear()
    h.configs[str(h.launch)]["agent"]["reasoning_effort"] = "medium"
    sid, parent, _info = _resume_stored(h, "cold", stored=child_id)
    _send_twice(h, monkeypatch, sid, parent)
    assert h.observed == [parse_reasoning_effort("medium") if release else selected] * 2


@pytest.mark.parametrize("matches", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_bot_chat_matching_and_superseded_composer_pick(resume_harness, monkeypatch, matches, explicit):
    h = resume_harness
    selected = parse_reasoning_effort("xhigh")
    config = {"follow_profile_config": True, "composer_override_profile": {
        "model": "saved" if matches else "superseded", "provider": "openai"},
        "reasoning_config": selected, "provider": "openai"}
    if explicit:
        config["reasoning_config_override"] = selected
    h.db.create_session("stored", source="desktop", model="saved", model_config=config)
    h.db.set_session_title("stored", "Bot Chat")
    h.configs[str(h.launch)]["agent"]["reasoning_effort"] = "medium"
    sid, parent, _info = _resume_stored(h, "cold")
    _send_twice(h, monkeypatch, sid, parent)
    expected = selected if matches and explicit else parse_reasoning_effort("medium")
    assert h.observed == [expected] * 2
    assert parent.get("create_reasoning_override") == (selected if matches and explicit else None)

@pytest.mark.parametrize("fresh", [False, True])
def test_child_sync_updates_compression_seed_before_first_persist(resume_harness, fresh):
    import io
    from tui_gateway.compute_host import ComputeHost

    agent = server._make_agent("child", "stored")
    agent._session_init_model_config = {"reasoning_config": parse_reasoning_effort("high")}
    server._init_session("child", "stored", agent, [])
    child = server._sessions["child"]
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    try:
        if not fresh:
            child["_reasoning_parent_revision"] = "old"
            child["create_reasoning_override"] = parse_reasoning_effort("xhigh")
            agent._session_init_model_config["reasoning_config_override"] = parse_reasoning_effort("xhigh")
        host._sync_turn_reasoning(server, child, {"reasoning_parent_revision": "new",
                                                "reasoning_config_override": {"enabled": False}})
        assert agent._session_init_model_config["reasoning_config_override"] == {"enabled": False}
        # An unchanged revision must not rewrite newer child-local state or its compression seed.
        child["create_reasoning_override"] = parse_reasoning_effort("low")
        server._sync_compression_reasoning_marker(agent, child["create_reasoning_override"])
        host._sync_turn_reasoning(server, child, {"reasoning_parent_revision": "new",
                                                "reasoning_config_override": {"enabled": False}})
        assert agent._session_init_model_config["reasoning_config_override"] == parse_reasoning_effort("low")
        host._sync_turn_reasoning(server, child, {"reasoning_parent_revision": "release",
                                                "reasoning_config_override": None})
        assert "reasoning_config_override" not in agent._session_init_model_config
    finally:
        host.close()


@pytest.mark.parametrize("refused", [False, True])
def test_global_write_clears_durable_marker_only_on_success(resume_harness, monkeypatch, refused):
    from hermes_cli.config_backend import ConfigWriteError

    h = resume_harness
    agent = server._make_agent("original", "stored")
    agent._session_db = h.db
    agent._session_init_model_config = {}
    h.db.create_session("stored", source="desktop", model="saved")
    server._init_session("original", "stored", agent, [])
    original = server._sessions["original"]
    server._set_reasoning("pick", {}, "reasoning", "xhigh", original)

    def write(key, value):
        if refused:
            raise ConfigWriteError("synthetic refusal", code="locked")
        h.configs[str(h.launch)]["agent"]["reasoning_effort"] = value

    monkeypatch.setattr(server, "_write_config_key", write)
    response = server.handle_request({"id": "global", "method": "config.set", "params": {
        "session_id": "original", "key": "reasoning", "value": "high", "scope": "global"}})
    assert ("error" in response) is refused
    row = json.loads(h.db.get_session("stored")["model_config"])
    expected = parse_reasoning_effort("xhigh") if refused else None
    assert row.get("reasoning_config_override") == expected
    assert agent._session_init_model_config.get("reasoning_config_override") == expected
    assert original.get("create_reasoning_override") == expected


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("superseded", [False, True])
def test_real_bot_composer_switch_persist_resume_policy(resume_harness, monkeypatch, explicit, superseded):
    from tests.tui_gateway.test_send_reasoning_override import _agent

    h = resume_harness
    agent = _agent("saved")
    agent.session_id = "stored"
    agent._session_db = h.db
    agent._session_init_model_config = {}
    server._init_session("original", "stored", agent, [])
    original = server._sessions["original"]
    original["follow_profile_config"] = True
    assert h.ensure_row(original)
    h.db.set_session_title("stored", "Bot Chat")
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda *a, **kw: None)
    monkeypatch.setattr("agent.agent_runtime_helpers._build_switched_client", lambda *a: None)
    monkeypatch.setattr(server, "_merge_preflight_warning", lambda *a: None)
    monkeypatch.setattr(server, "_expensive_model_confirm", lambda *a: None)
    monkeypatch.setattr("hermes_cli.observability.shared_metrics_events.record_model_switch", lambda **kw: None)
    monkeypatch.setattr("hermes_cli.model_switch.switch_model", lambda **kw: SimpleNamespace(
        success=True, new_model="composer", target_provider="openai", api_key="synthetic-key",
        base_url="https://synthetic.invalid/v1", api_mode="chat_completions", warning_message="",
        model_info=None, runtime_capabilities={}))
    for name in ("_restart_slash_worker", "_persist_live_session_system_prompt", "_append_model_switch_marker"):
        monkeypatch.setattr(server, name, lambda *a, **kw: None)
    server._apply_model_switch("original", original, "composer --provider openai --session")
    if explicit:
        response = server.handle_request({"id": "pick", "method": "config.set", "params": {
            "session_id": "original", "key": "reasoning", "value": "xhigh"}})
        assert "error" not in response, response
    row = json.loads(h.db.get_session("stored")["model_config"])
    assert row["composer_override_profile"] == {"model": "saved", "provider": "openai"}
    assert row["reasoning_config"] == parse_reasoning_effort("xhigh" if explicit else "high")
    server._sessions.clear()
    h.configs[str(h.launch)]["agent"]["reasoning_effort"] = "medium"
    if superseded:
        h.configs[str(h.launch)]["model"]["default"] = "replacement"
    sid, parent, _info = _resume_stored(h, "cold")
    _send_twice(h, monkeypatch, sid, parent)
    expected = parse_reasoning_effort("xhigh" if explicit and not superseded else "medium")
    assert h.observed == [expected] * 2


@pytest.mark.parametrize("reasoning_field", ["reasoning_config", "reasoning_config_override"])
def test_session_resume_passes_stored_runtime_to_agent(resume_harness, monkeypatch, reasoning_field):
    captured = {}

    class FakeDB:
        def get_session(self, target):
            return {
                "id": target,
                "model": "gpt-5.4",
                "billing_provider": "openai-codex",
                "model_config": json.dumps({
                    reasoning_field: {"enabled": True, "effort": "high"}, "service_tier": "priority",
                    "base_url": "https://custom.example/v1", "api_mode": "chat_completions"}),
            }

        def reopen_session(self, target):
            pass

        def get_resume_conversations(self, session_id):
            return (
                self.get_messages_as_conversation(session_id, repair_alternation=True),
                self.get_messages_as_conversation(session_id, include_ancestors=True),
            )

        def get_ancestor_display_prefix(self, _sid):
            return []

        def get_messages_as_conversation(self, target, include_ancestors=False, repair_alternation=False, **_kwargs):
            return [{"role": "user", "content": "hello"}]

    def fake_make_agent(sid, key, session_id=None, session_db=None, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(model="gpt-5.4", provider="openai-codex")

    monkeypatch.setattr(server, "_get_db", lambda: FakeDB())
    monkeypatch.setattr(server, "_profile_session_db", lambda home: (FakeDB(), False))
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_set_session_context", lambda target, cwd=None: [])
    monkeypatch.setattr(server, "_clear_session_context", lambda tokens: None)
    monkeypatch.setattr(server, "_make_agent", fake_make_agent)
    monkeypatch.setattr(server, "_make_agent_in_context", lambda sid, key, **kw:
                        fake_make_agent(sid, key, session_id=key, **kw))
    monkeypatch.setattr(server, "_session_info", lambda agent, *a: {"model": agent.model, "provider": agent.provider})

    def fake_init_session(sid, key, agent, history, cols=80, **_kwargs):
        server._sessions[sid] = {"agent": agent, "session_key": key}

    monkeypatch.setattr(server, "_init_session", fake_init_session)

    # eager_build: this asserts the synchronous build contract (stored runtime
    # overrides reach _make_agent, info comes from _session_info). The deferred
    # default restores the same overrides via _start_agent_build off-thread.
    resp = server.handle_request(
        {"id": "1", "method": "session.resume", "params": {"session_id": "stored-session", "eager_build": True}}
    )
    assert "error" not in resp, resp

    assert captured["model_override"] == {
        "model": "gpt-5.4",
        "provider": "openai-codex",
        "base_url": "https://custom.example/v1",
        "api_mode": "chat_completions",
    }
    assert captured["provider_override"] == "openai-codex"
    expected = parse_reasoning_effort("high") if reasoning_field == "reasoning_config_override" else None
    assert captured.get("reasoning_config_override") == expected
    assert captured["service_tier_override"] == "priority"
    runtime_sid = resp["result"]["session_id"]
    assert server._sessions[runtime_sid]["model_override"] == captured["model_override"]
