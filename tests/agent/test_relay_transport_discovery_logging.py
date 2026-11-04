"""Optional provider-adapter discovery remains fail-open with debug diagnostics."""

import logging
import sys

import pytest

from agent.relay_llm import _is_process_transport
from hermes_cli import runtime_provider_backends


@pytest.mark.parametrize("error", [ImportError("adapter unavailable"), RuntimeError("adapter failed")])
def test_discovery_failure_preserves_http_fallback_and_debug_traceback(monkeypatch, caplog, error):
    def fail(_provider):
        raise error

    monkeypatch.setattr(runtime_provider_backends, "_is_external_process_provider", fail)
    with caplog.at_level(logging.DEBUG, logger="agent.relay_llm"):
        assert _is_process_transport("synthetic") is False
    record, = [r for r in caplog.records if r.name == "agent.relay_llm"]
    assert record.levelno == logging.DEBUG
    assert record.exc_info[1] is error
    assert record.exc_info[2] is not None
    assert "Process transport discovery failed" in record.getMessage()


def test_missing_adapter_module_preserves_http_fallback(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider_backends", None)
    with caplog.at_level(logging.DEBUG, logger="agent.relay_llm"):
        assert _is_process_transport("synthetic") is False
    assert any(r.exc_info and isinstance(r.exc_info[1], ImportError) for r in caplog.records)


@pytest.mark.parametrize("external", [True, False])
def test_discovery_success_preserves_transport_choice(monkeypatch, caplog, external):
    monkeypatch.setattr(runtime_provider_backends, "_is_external_process_provider", lambda _: external)
    with caplog.at_level(logging.DEBUG, logger="agent.relay_llm"):
        assert _is_process_transport("synthetic") is external
    assert not [r for r in caplog.records if r.name == "agent.relay_llm"]


def test_discovery_does_not_swallow_process_interrupt(monkeypatch):
    def interrupt(_provider):
        raise KeyboardInterrupt

    monkeypatch.setattr(runtime_provider_backends, "_is_external_process_provider", interrupt)
    with pytest.raises(KeyboardInterrupt):
        _is_process_transport("synthetic")
