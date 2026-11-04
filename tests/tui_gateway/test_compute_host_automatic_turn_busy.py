"""Host-local automatic turns must use the parent's normal busy-input path."""
from __future__ import annotations

import io
import json
import threading
import types

import pytest

from tests.tui_gateway.test_compute_host_turn_protocol import _agent, _session, turn_env
from tui_gateway import server
from tui_gateway.compute_host import ComputeHost
from tui_gateway.host_supervisor import HostSupervisor

pytestmark = pytest.mark.usefixtures("turn_env")


@pytest.fixture
def automatic_turn(monkeypatch, request):
    """Real notification admission and turn worker, synthetic provider and pipe only."""
    sid = "automatic-busy"
    if getattr(request, "param", False):
        from gateway import warning_notifications
        monkeypatch.setattr(warning_notifications, "diagnostic_turn_muted", lambda *args: True)
    entered, release = threading.Event(), threading.Event()
    agent = _agent(["finished"])
    run = agent.run_conversation
    agent.steers, agent.redirects = [], []
    agent.steer = lambda text: agent.steers.append(text) or True
    agent.redirect = lambda text: agent.redirects.append(text) or True
    agent._supports_active_turn_redirect = True

    def conversation(*args, **kwargs):
        entered.set()
        assert release.wait(10), "test did not release the synthetic provider"
        return run(*args, **kwargs)

    agent.run_conversation = conversation
    child = _session(agent)
    parent = _session(None)
    parent.update(_compute_host_active=True, agent_ready=threading.Event())
    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    child["transport"] = host._transport
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {"turn_isolation": True})
    monkeypatch.setattr(server, "_session_compression_in_flight", lambda session: False)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *args: None)
    monkeypatch.delenv("HERMES_COMPUTE_HOST_CHILD", raising=False)
    monkeypatch.setattr(server, "write_json", lambda message: True)
    supervisor = HostSupervisor(autostart=False, rpc_sink=server._relay_compute_host_rpc,
                                expected_build_sha="synthetic", expected_hermes_home="synthetic")
    controls, dispatches = [], []

    def control(target, *, route_name, payload=None, **kwargs):
        controls.append(route_name)
        server._sessions[sid] = child
        try:
            host.handle_frame({"type": "control", "sid": target, "route_name": route_name,
                               "request_id": "correction", **(payload or {})})
        finally:
            server._sessions[sid] = parent
        return json.loads(out.getvalue().splitlines()[-1])

    monkeypatch.setattr(supervisor, "control", control)
    monkeypatch.setattr(supervisor, "submit_turn", lambda frame, **kwargs: dispatches.append((frame, kwargs)))
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda cfg=None: supervisor)
    server._sessions[sid] = child
    assert server._notif_claim_turn(child)
    assert server._notif_submit("notification", sid, child, "background result", "test notification",
                                display_kind="process_complete")
    assert entered.wait(10)
    server._sessions[sid] = parent
    cursor = 0

    def relay():
        nonlocal cursor
        frames = [json.loads(line) for line in out.getvalue().splitlines()]
        for frame in frames[cursor:]:
            supervisor._handle_host_frame(frame)
        cursor = len(frames)
        return frames

    def finish():
        server._sessions[sid] = child
        release.set()
        child["_run_thread"].join(10)
        assert not child["_run_thread"].is_alive()
        server._sessions[sid] = parent
        return relay()

    try:
        yield types.SimpleNamespace(sid=sid, parent=parent, child=child, agent=agent,
                                    host=host, supervisor=supervisor, controls=controls, dispatches=dispatches,
                                    relay=relay, finish=finish)
    finally:
        server._sessions[sid] = child
        release.set()
        child["_run_thread"].join(10)
        server._sessions.pop(sid, None)
        host.close()


@pytest.mark.parametrize("mode, status, field", [("steer", "steered", "steers"),
                                                   ("interrupt", "redirected", "redirects")])
def test_automatic_turn_routes_prompt_submit_as_live_correction(automatic_turn, monkeypatch, mode, status, field):
    lane = automatic_turn
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: mode)
    lane.relay()
    assert lane.parent["running"] is True, "child automatic turn is invisible to parent admission"
    assert "_compute_host_turn_id" not in lane.parent
    response = server.handle_request({"id": "user", "method": "prompt.submit",
                                      "params": {"session_id": lane.sid, "text": "use this result"}})
    assert response["result"]["status"] == status
    assert getattr(lane.agent, field) == ["use this result"]
    assert not lane.dispatches and not lane.parent.get("queued_prompt")
    lane.finish()
    assert lane.parent["running"] is False


def test_automatic_turn_queued_submit_drains_once_on_settlement(automatic_turn, monkeypatch):
    lane = automatic_turn
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "steer")
    lane.relay()
    assert lane.parent["running"] is True, "automatic turn must own parent busy admission"
    response = server.handle_request({"id": "queued", "method": "prompt.submit", "params": {
        "session_id": lane.sid, "text": "next user turn", "queued": True}})
    assert response["result"]["status"] == "queued"
    assert lane.agent.steers == [] and lane.agent.redirects == []
    assert lane.parent["queued_prompt"]["text"] == "next user turn"
    frames = lane.finish()
    assert len(lane.dispatches) == 1
    assert lane.dispatches[0][0]["text"] == "next user turn"
    for frame in frames:
        lane.supervisor._handle_host_frame(frame)
    assert len(lane.dispatches) == 1, "replayed settlement must not drain twice"
    assert lane.parent["running"] is True, "old local settlement must not clear the newer dispatch"


@pytest.mark.parametrize("automatic_turn", [True], indirect=True)
def test_muted_automatic_turn_still_publishes_internal_lifecycle(automatic_turn):
    lane = automatic_turn
    frames = lane.relay()
    assert lane.parent["running"] is True
    assert not any(f.get("message", {}).get("params", {}).get("type") == "message.start" for f in frames)
    lane.finish()
    assert lane.parent["running"] is False


def test_host_crash_releases_automatic_busy_mirror(automatic_turn):
    lane = automatic_turn
    lane.relay()
    assert lane.parent["running"] is True
    lane.supervisor._fail_pending_turns(reason="crash", message="synthetic host exit")
    assert lane.parent["running"] is False
    assert not lane.parent.get("_compute_host_local_turn_id")
    assert lane.supervisor._local_turns == {}


def test_older_routed_completion_cannot_idle_an_automatic_turn(automatic_turn):
    lane = automatic_turn
    lane.relay()
    server._on_compute_host_turn_done("older", lane.sid, lane.parent, {
        "type": "turn.end", "sid": lane.sid, "session_info_emitted": True})
    assert lane.parent["running"] is True
    assert lane.parent["inflight_turn"]["user"] == "background result"
    lane.finish()
    assert lane.parent["running"] is False


def test_older_local_settlement_cannot_idle_a_newer_local_turn(automatic_turn):
    lane = automatic_turn
    frames = lane.relay()
    start = next(f["message"] for f in frames if f.get("message", {}).get("method") == "compute_host.turn_state")
    newer = {**start, "params": {**start["params"], "turn_id": "newer-automatic"}}
    lane.supervisor._handle_host_frame({"type": "rpc", "message": newer})
    lane.finish()
    assert lane.parent["running"] is True
    assert lane.parent["_compute_host_local_turn_id"] == "newer-automatic"
    assert lane.supervisor._local_turns == {lane.sid: "newer-automatic"}


def test_media_fallback_stays_queued_without_interrupting_automatic_turn(automatic_turn, monkeypatch):
    lane = automatic_turn
    lane.relay()
    lane.parent["attached_images"] = ["synthetic-image.png"]
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "interrupt")
    interrupted = []
    monkeypatch.setattr(server, "_interrupt_busy_session", lambda *args: interrupted.append(args))
    response = server.handle_request({"id": "media", "method": "prompt.submit", "params": {
        "session_id": lane.sid, "text": "inspect this"}})
    assert response["result"]["status"] == "queued"
    assert lane.controls == [] and interrupted == []
    lane.finish()
    assert len(lane.dispatches) == 1
    assert lane.dispatches[0][0]["attached_images"] == ["synthetic-image.png"]


def test_stop_reaches_automatic_child_and_cancels_parent_queue(automatic_turn, monkeypatch):
    lane = automatic_turn
    lane.relay()
    assert lane.supervisor.has_pending_turn(lane.sid)
    monkeypatch.setattr(lane.supervisor, "is_running", lambda: True)

    def interrupt(sid, *, request_id=None):
        server._sessions[sid] = lane.child
        try:
            lane.host.handle_frame({"type": "interrupt", "sid": sid, "request_id": "stop"})
        finally:
            server._sessions[sid] = lane.parent

    monkeypatch.setattr(lane.supervisor, "interrupt", interrupt)
    server._handle_busy_submit("queue", lane.sid, lane.parent, "do not run after Stop", None, queued=True)
    response = server.handle_request({"id": "stop", "method": "session.interrupt",
                                      "params": {"session_id": lane.sid}})
    assert "error" not in response
    assert lane.child["_turn_cancel_requested"] is True
    lane.finish()
    assert not lane.dispatches and not lane.parent.get("queued_prompt")
