"""Mid-turn steer/redirect reach the compute-host child's agent under turn isolation (#82603).

The parent gateway holds ``agent: None`` for the whole isolated turn, so before this fix
``session.redirect`` hit the turn-build queue branch and ``session.steer`` returned 4010:
either way the correction waited for the turn to finish. The parent now relays the
correction to the child as a control frame, and the child applies it to its live agent.
"""

from __future__ import annotations

import io
import json
import threading
import types

import pytest

from tui_gateway import server
from tui_gateway.compute_host import ComputeHost
from tui_gateway.host_supervisor import MUTATOR_ROUTE_TABLE


class _Agent:
    _supports_active_turn_redirect = True

    def __init__(self, accept: bool = True):
        self.accept = accept
        self.steers: list[str] = []
        self.redirects: list[str] = []

    def steer(self, text):
        self.steers.append(text)
        return self.accept

    def redirect(self, text):
        self.redirects.append(text)
        return self.accept


def _json_lines(out: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def _child_session(agent, **extra) -> dict:
    return {"agent": agent, "running": True, "history_lock": threading.Lock(),
            "session_key": "child-key", "inflight_turn": {"user": "go", "assistant": ""}, **extra}


class _RelayToChild:
    """Supervisor stand-in that delivers control frames to an in-process ComputeHost child."""

    def __init__(self, host: ComputeHost, out: io.StringIO, child_sessions: dict):
        self.host, self.out, self.child_sessions, self.frames = host, out, child_sessions, []

    def control(self, sid, *, route_name, payload=None, wait=True, timeout=30.0, on_late_ack=None):
        assert route_name in MUTATOR_ROUTE_TABLE
        frame = {"type": "control", **(payload or {}), "sid": sid, "route_name": route_name,
                 "request_id": f"rq-{len(self.frames)}"}
        self.frames.append(frame)
        parent_session = server._sessions.pop(sid)
        server._sessions[sid] = self.child_sessions[sid]
        try:
            self.host.handle_frame(frame)
        finally:
            server._sessions[sid] = parent_session
        return _json_lines(self.out)[-1]


class _DeadHost:
    def control(self, *a, **k):
        raise RuntimeError("compute host is not running")

    def interrupt(self, sid, *, request_id=None):
        pass

    def has_pending_turn(self, sid):
        return True


@pytest.fixture
def isolated(monkeypatch):
    """A parent session mid isolated turn, wired to an in-process child that holds ``agent``."""
    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    children: dict = {}
    supervisor = _RelayToChild(host, out, children)
    monkeypatch.setattr(server, "_load_cfg", lambda: {"dashboard": {"turn_isolation": True}})
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config",
                        lambda: {"turn_isolation": True})
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda _cfg=None: supervisor)
    monkeypatch.setattr(server, "_session_compression_in_flight", lambda _s: False)
    monkeypatch.delenv("HERMES_COMPUTE_HOST_CHILD", raising=False)

    def make(sid: str, agent, **parent_extra):
        children[sid] = _child_session(agent)
        server._sessions[sid] = {
            "agent": None, "agent_ready": threading.Event(), "running": True,
            "history_lock": threading.Lock(), "session_key": "parent-key",
            "_compute_host_active": True, "_compute_host_turn_id": "turn-1",
            "inflight_turn": {"user": "go", "assistant": ""}, **parent_extra}
        return server._sessions[sid], children[sid]

    yield types.SimpleNamespace(make=make, supervisor=supervisor, children=children)
    for sid in list(children):
        server._sessions.pop(sid, None)
    host.close()


def _rpc(method, sid, text):
    return server.handle_request({"id": "1", "method": method, "params": {"session_id": sid, "text": text}})


def test_session_steer_reaches_the_child_agent(isolated):
    agent = _Agent()
    parent, _child = isolated.make("iso-steer", agent)

    resp = _rpc("session.steer", "iso-steer", "also check the logs")

    assert resp["result"] == {"status": "queued", "text": "also check the logs"}
    assert agent.steers == ["also check the logs"]
    assert not parent.get("queued_prompt")
    assert parent["inflight_turn"]["corrections"] == ["also check the logs"]


def test_session_redirect_reaches_the_child_agent_instead_of_queueing(isolated):
    agent = _Agent()
    parent, _child = isolated.make("iso-redirect", agent)

    resp = _rpc("session.redirect", "iso-redirect", "stop, do X instead")

    assert resp["result"]["status"] == "redirected"
    assert agent.redirects == ["stop, do X instead"]
    assert not parent.get("queued_prompt"), "redirect must not wait behind the live turn"


@pytest.mark.parametrize("mode, verb, status", [("steer", "steers", "steered"),
                                                ("interrupt", "redirects", "redirected")])
def test_busy_prompt_submit_corrects_the_child_turn(isolated, monkeypatch, mode, verb, status):
    agent = _Agent()
    parent, _child = isolated.make(f"iso-busy-{mode}", agent)
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: mode)
    interrupted: list = []
    monkeypatch.setattr(server, "_interrupt_busy_session", lambda *a: interrupted.append(a))

    resp = server._handle_busy_submit("1", f"iso-busy-{mode}", parent, "use the other API", None)

    assert resp["result"] == {"status": status}
    assert getattr(agent, verb) == ["use the other API"]
    assert not parent.get("queued_prompt") and interrupted == []


def test_child_without_a_live_turn_falls_back_to_the_queue(isolated):
    agent = _Agent()
    parent, child = isolated.make("iso-idle-child", agent)
    child["running"] = False  # the child already finished; turn.done not yet relayed

    resp = _rpc("session.redirect", "iso-idle-child", "late correction")

    assert resp["result"]["status"] == "queued"
    assert agent.redirects == []
    assert parent["queued_prompt"]["text"] == "late correction"


def test_dead_host_falls_back_to_the_queue(isolated, monkeypatch):
    parent, _child = isolated.make("iso-dead", _Agent())
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda _cfg=None: _DeadHost())

    resp = _rpc("session.redirect", "iso-dead", "are you there")

    assert resp["result"]["status"] == "queued"
    assert parent["queued_prompt"]["text"] == "are you there"


def test_child_rejection_is_reported_not_queued(isolated):
    agent = _Agent(accept=False)
    parent, _child = isolated.make("iso-reject", agent)

    resp = _rpc("session.steer", "iso-reject", "nope")

    assert resp["result"]["status"] == "rejected"
    assert "corrections" not in parent["inflight_turn"]


def test_in_process_session_never_contacts_the_host(isolated, monkeypatch):
    """No isolated turn in flight (agent built in-process): the existing path applies the steer."""
    agent = _Agent()
    server._sessions["inproc"] = {"agent": agent, "running": True, "history_lock": threading.Lock(),
                                  "session_key": "k", "inflight_turn": {"user": "go", "assistant": ""}}
    try:
        resp = _rpc("session.steer", "inproc", "inline")
    finally:
        server._sessions.pop("inproc", None)

    assert resp["result"]["status"] == "queued"
    assert agent.steers == ["inline"]
    assert isolated.supervisor.frames == []
