"""Tests for the Langfuse tracing wrapper (Requirement 13).

Covers:
  * Disabled fallback when Langfuse is unconfigured (Req 13.3) — no-op, logs.
  * Trace span + step output recording on the happy path (Req 13.1).
  * Failure resilience: a tracing error never interrupts the step (Req 13.4).

These use a fake client implementing the small slice of the Langfuse v3/v4 SDK
the Tracer relies on (``start_as_current_observation`` /
``update_current_span`` / ``flush``), so no live Langfuse backend is needed.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

import pytest

from angent.observability.tracing import Tracer


# --- Fakes -----------------------------------------------------------------


class _FakeClient:
    """A well-behaved fake Langfuse client (v3/v4 surface)."""

    def __init__(self):
        self.spans = []
        self.updates = []
        self.flushed = 0

    @contextmanager
    def start_as_current_observation(self, *, as_type, name, input=None, **kw):
        rec = {"as_type": as_type, "name": name, "input": input}
        self.spans.append(rec)
        yield rec

    def update_current_span(self, **kwargs):
        self.updates.append(kwargs)

    def flush(self):
        self.flushed += 1


class _FailingClient:
    """A client whose span creation always raises, to exercise resilience."""

    def __init__(self):
        self.calls = 0

    def start_as_current_observation(self, **kwargs):
        self.calls += 1
        raise RuntimeError("simulated langfuse failure")

    def update_current_span(self, **kwargs):
        raise RuntimeError("should not be called")

    def flush(self):
        pass


# --- Disabled fallback (Req 13.3) -----------------------------------------


def test_disabled_when_unconfigured(monkeypatch, caplog):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)

    with caplog.at_level(logging.INFO, logger="angent.observability.tracing"):
        tracer = Tracer()

    assert tracer.enabled is False
    assert tracer.client is None
    assert any("tracing disabled" in r.message.lower() for r in caplog.records)


def test_disabled_methods_are_noops(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    tracer = Tracer()

    with tracer.trace_step("step-1", input={"x": 1}) as handle:
        handle.output = {"y": 2}
        tracer.record_llm_span("prompt", "response", token_count=10)

    assert handle.span is None
    assert handle.end_time is not None
    with tracer.propagate(session_id="s", tags=["t"]):
        pass
    tracer.flush()  # no-op, no raise


def test_force_disabled():
    tracer = Tracer(client=_FakeClient(), enabled=False)
    assert tracer.enabled is False


# --- Happy path (Req 13.1) ------------------------------------------------


def test_records_span_and_output():
    client = _FakeClient()
    tracer = Tracer(client=client)

    assert tracer.enabled is True

    with tracer.trace_step("qualifier.qualify", input={"q": "ai"}) as handle:
        handle.output = {"results": 3}

    # One span recorded with the step name + input.
    assert len(client.spans) == 1
    assert client.spans[0]["name"] == "qualifier.qualify"
    assert client.spans[0]["input"] == {"q": "ai"}
    assert client.spans[0]["as_type"] == "span"

    # Output + duration recorded on exit.
    assert client.updates
    assert client.updates[-1]["output"] == {"results": 3}
    assert "duration_s" in client.updates[-1]["metadata"]

    tracer.flush()
    assert client.flushed >= 1


def test_record_llm_span_creates_generation():
    client = _FakeClient()
    tracer = Tracer(client=client)
    tracer.record_llm_span("the prompt", "the response", token_count=42, name="tf-call")
    gen = [s for s in client.spans if s["as_type"] == "generation"]
    assert gen and gen[0]["name"] == "tf-call"


# --- Failure resilience (Req 13.4) ----------------------------------------


def test_span_failure_does_not_interrupt_step(caplog):
    client = _FailingClient()
    tracer = Tracer(client=client)

    step_ran = False
    with caplog.at_level(logging.WARNING, logger="angent.observability.tracing"):
        with tracer.trace_step("flaky", input={}) as handle:
            step_ran = True
            handle.output = "done"

    assert step_ran is True
    assert handle.span is None  # span creation failed
    assert handle.end_time is not None  # step still completed
    assert any("failed to start span" in r.message.lower() for r in caplog.records)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
