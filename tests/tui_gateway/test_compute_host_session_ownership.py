"""Compute-host ownership across session creation and deferred resume."""

import threading
import time
import types

import pytest

from tui_gateway import server


@pytest.fixture(autouse=True)
def _neuter_agent_prewarm_timer(request, monkeypatch):
    """Stub the deferred agent pre-warm timer for every test in this module.

    ``session.create`` and non-eager ``session.resume`` fire a 50 ms
    background ``threading.Timer`` (``_schedule_agent_build``) that calls
    whatever ``server._make_agent`` is patched in AT FIRE TIME. Left live,
    a timer armed by one test outlives it and lands in the NEXT test's
    ``_make_agent`` mock, racily corrupting its captured state (the
    ``'tip' == 'cont_tip'`` flakes in the session_resume tests). Tests that
    exercise the deferred build itself opt back in with
    ``@pytest.mark.real_agent_prewarm``.
    """
    if request.node.get_closest_marker("real_agent_prewarm"):
        yield
        return
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)
    yield


@pytest.fixture(autouse=True)
def _reap_leaked_notification_pollers():
    """Stop and join notification pollers leaked by each test.

    session.init/create paths start a per-session poller daemon thread. A
    poller left running by one test steals-and-requeues events off the
    PROCESS-GLOBAL process_registry.completion_queue while a later test is
    asserting on it — the root cause of the flaky
    test_run_prompt_submit_requeues_all_unstarted_notifications_with_real_threading
    (two CI hits on unrelated PRs, Aug 2026). Set every registered poller's
    stop event (the loop wakes at least every 0.5s), then join with ONE
    small shared budget — never per-thread — so teardown stays O(seconds)
    for the whole file even when many tests leaked pollers.
    """
    yield
    pollers = [
        (stop, thread)
        for stop, thread in list(server._notification_pollers)
        if thread.is_alive()
    ]
    for stop, _thread in pollers:
        stop.set()
    deadline = time.time() + 3.0
    for _stop, thread in pollers:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        thread.join(timeout=remaining)
    server._notification_pollers[:] = [
        (stop, thread)
        for stop, thread in server._notification_pollers
        if thread.is_alive()
    ]


def test_session_create_then_prompt_submit_routes_to_compute_host(monkeypatch):
    """Regression for #107924: a real UI's session.create → prompt.submit path must keep
    routing to the compute host under dashboard.turn_isolation. The existing
    test_prompt_submit_dispatches_to_compute_host_when_turn_isolation_enabled seeds
    agent=None directly without going through session.create, so it never exercises
    the eager _schedule_agent_build (50ms timer) that defeats the lazy-session gate
    in _session_uses_compute_host for any real client."""

    class FakeSupervisor:
        def __init__(self):
            self.frames = []
            self.callback = None

        def submit_turn(self, frame, *, on_complete=None):
            self.frames.append(frame)
            self.callback = on_complete
            return frame["request_id"]

    fake_supervisor = FakeSupervisor()
    monkeypatch.setattr(server, "_load_cfg", lambda: {"dashboard": {"turn_isolation": True}})
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda _cfg=None: fake_supervisor)

    # Mirror what the real 50ms threading.Timer does — populate agent + signal agent_ready —
    # without running the heavy AIAgent construction that would require a working provider.
    def _fake_schedule(sid, delay=0.05):
        sess = server._sessions.get(sid)
        if sess is None:
            return
        sess["agent"] = types.SimpleNamespace()
        sess["agent_ready"].set()

    monkeypatch.setattr(server, "_schedule_agent_build", _fake_schedule)

    try:
        create_resp = server.handle_request(
            {"id": "create", "method": "session.create", "params": {"messages": []}}
        )
        assert "error" not in create_resp, create_resp
        sid = create_resp["result"]["session_id"]
        # Real session.create arms a 50ms timer; any real UI (or iso-certify) takes longer
        # than 50ms between create and submit, so by prompt time agent is already set.
        assert server._sessions[sid]["agent"] is not None, "timer did not fire (test setup wrong)"
        resp = server.handle_request(
            {"id": "submit", "method": "prompt.submit",
             "params": {"session_id": sid, "text": "hello"}}
        )
        assert "error" not in resp, resp
        assert resp["result"].get("turn_isolation") is True, (
            "prompt.submit fell back to the in-process path; the lazy-session gate was "
            "defeated by the eager _start_agent_build scheduled at session.create time"
        )
        assert fake_supervisor.frames, "compute host supervisor never received the turn frame"
    finally:
        for sid in list(server._sessions):
            server._sessions.pop(sid, None)


def test_session_resume_deferred_history_acknowledges_and_reuses(monkeypatch):
    history_started = threading.Event()
    release_history = threading.Event()
    build_started = threading.Event()
    history_calls = []
    auto_continue_calls = []
    ancestor = {"role": "assistant", "content": "ancestor"}
    loaded = {"role": "user", "content": "loaded"}

    class FakeDB:
        def get_session(self, target):
            return {"id": target, "message_count": 1200}

        def resolve_resume_session_id(self, target):
            return target

        def reopen_session(self, target):
            assert target == "large-session"

        def get_resume_conversations(self, target):
            history_calls.append(("resume", target))
            history_started.set()
            assert release_history.wait(timeout=2.0)
            return [loaded], [ancestor, loaded]

        def get_ancestor_display_prefix(self, target):
            history_calls.append(("prefix", target))
            return [ancestor]

    monkeypatch.setattr(server, "_get_db", lambda: FakeDB())
    monkeypatch.setattr(server, "_load_cfg", lambda: {"dashboard": {"turn_isolation": True}})
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(
        server,
        "_start_agent_build",
        lambda _sid, _session: build_started.set(),
    )
    monkeypatch.setattr(
        server,
        "_maybe_schedule_auto_continue",
        lambda sid, session, stored_id: auto_continue_calls.append(
            (sid, session, stored_id)
        ),
    )

    try:
        first = server._methods["session.resume"](
            "r1",
            {
                "session_id": "large-session",
                "source": "desktop",
                "defer_history": True,
            },
        )

        assert first["result"]["hydrating"] is True
        assert first["result"]["messages"] == []
        assert first["result"]["message_count"] == 1200
        assert history_started.wait(timeout=1.0)

        second = server._methods["session.resume"](
            "r2",
            {"session_id": "large-session", "defer_history": True},
        )
        assert second["result"]["session_id"] == first["result"]["session_id"]
        assert second["result"]["hydrating"] is True
        assert second["result"]["messages"] == []

        release_history.set()
        sid = first["result"]["session_id"]
        assert server._sessions[sid]["_compute_host_active"] is True
        assert server._sessions[sid]["resume_history_ready"].wait(timeout=1.0)
        assert build_started.wait(timeout=1.0)
        assert history_calls == [
            ("resume", "large-session"),
            ("prefix", "large-session"),
        ]
        assert server._sessions[sid]["history"] == [loaded]
        assert server._sessions[sid]["display_history_prefix"] == [ancestor]
        assert server._sessions[sid]["resume_message_count"] == 2
        assert auto_continue_calls == [(sid, server._sessions[sid], "large-session")]
    finally:
        release_history.set()
        for sid, session in list(server._sessions.items()):
            if session.get("session_key") == "large-session":
                lease = session.get("active_session_lease")
                if lease is not None:
                    lease.release()
                server._sessions.pop(sid, None)
