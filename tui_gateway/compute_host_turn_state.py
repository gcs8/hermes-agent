"""Internal compute-host turn lifecycle, independent of rendered message events."""
from __future__ import annotations

from typing import Any

METHOD = "compute_host.turn_state"


def publish_turn_state(session: dict, sid: str, turn_id: str, running: bool) -> None:
    """Called under history_lock at admission/settlement, including muted turns."""
    publish = getattr(session.get("transport"), "turn_state", None)
    if callable(publish):
        publish(sid, {"turn_id": turn_id, "running": running,
                      "session_key": session.get("session_key") or sid,
                      "history_version": session.get("history_version", 0),
                      "message_count": len(session.get("history", [])),
                      "inflight": session.get("inflight_turn")})


def mirror_turn_state(server: Any, params: dict) -> None:
    sid = str(params.get("session_id") or "")
    session = server._sessions.get(sid)
    if session is None:
        return
    turn_id = params.get("turn_id")
    if not isinstance(turn_id, str) or not turn_id:
        return
    with session["history_lock"]:
        if params.get("running"):
            if session.get("_closing") or not server._session_uses_compute_host(session):
                return
            session["_compute_host_local_turn_id"] = turn_id
            session["running"] = True
            if not session.get("_compute_host_turn_id"):
                session["inflight_turn"] = params.get("inflight")
            return
        if session.get("_compute_host_local_turn_id") != turn_id:
            return

        # A parent-dispatched turn owns its own completion callback. The old child's
        # settlement must not release that newer claim, even before it starts running.
        if session.get("_compute_host_turn_id"):
            session.pop("_compute_host_local_turn_id", None)
            return
    server._on_compute_host_turn_done("__host_local__", sid, session, {
        **params, "sid": sid, "type": "turn.error" if params.get("error") else "turn.end",
        "message": params.get("error"), "session_info_emitted": True, "local_turn_id": turn_id})
