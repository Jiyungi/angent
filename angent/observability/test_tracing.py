"""Tests for the Langfuse tracing wrapper (Requirement 13).

Covers:
  * Disabled fallback when Langfuse is unconfigured (Req 13.3) — no-op, logs.
  * Trace + linked LLM span recording on the happy path (Req 13.1, 13.2).
  * Write-failure resilience: retry 3x then continue uninterrupted (Req 13.4).
"""

from __future__ import annotations

import logging

import pytest

from angent.observability.tracing import DEFAULT_MAX_RETRIES, Tracer


# --- Fakes -----------------------------------------------------------------


class _FakeSpan:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.ended = False

    def end(self, **kwargs):
        self.ended = True


class _FakeTrace:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.updates = []
        self.spans = []

    def update(self, **kwargs):
        self.updates.append(kwargs)

    def span(self, **kwargs):
        s = _FakeSpan(**kwargs)
        self.spans.append(s)
        return s


class _FakeClient:
    """A well-behaved fake Langfuse client."""

    def __init__(self):
        self.traces = []
        self.flushed = 0

    def trace(self, **kwargs):
        t = _FakeTrace(**kwargs)
        self.traces.append(t)
        return t

    def flush(self):
        self.flushed += 1


class _FailingClient:
    """A client whose trace() always raises, to exercise retry/fallback."""

    def __init__(self):
        self.trace_calls = 0

    def trace(self, **kwargs):
        self.trace_calls += 1
        raise RuntimeError("simulated langfuse write failure")

    def flush(self):
        pass


# --- Disabled fallback (Req 13.3) -----------------------------------------


def test_disabled_when_unconfigured(monkeypatch, caplog):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)

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
        # Must not raise even though disabled.
        tracer.record_llm_span("prompt", "response", token_count=10)

    assert handle.trace is None
    assert handle.end_time is not None
    tracer.flush()  # no-op, no raise


# --- Happy path (Req 13.1, 13.2) ------------------------------------------


def test_records_trace_and_linked_span():
    client = _FakeClient()
    tracer = Tracer(client=client)

    assert tracer.enabled is True

    with tracer.trace_step("discover", input={"q": "ai"}) as handle:
        handle.output = {"results": 3}
        tracer.record_llm_span("the prompt", "the response", token_count=42)

    # One trace recorded with step id + input.
    assert len(client.traces) == 1
    trace = client.traces[0]
    assert trace.kwargs["name"] == "discover"
    assert trace.kwargs["input"] == {"q": "ai"}

    # Trace updated with output + timestamps within the step.
    assert trace.updates
    upd = trace.updates[-1]
    assert upd["output"] == {"results": 3}
    assert "start_time" in upd["metadata"] and "end_time" in upd["metadata"]

    # One linked LLM span with prompt/response/token count.
    assert len(trace.spans) == 1
    span = trace.spans[0]
    assert span.kwargs["input"] == "the prompt"
    assert span.kwargs["output"] == "the response"
    assert span.kwargs["metadata"]["token_count"] == 42
    assert span.ended is True

    # Flushed so the trace lands promptly.
    assert client.flushed >= 1


# --- Write-failure resilience (Req 13.4) ----------------------------------


def test_write_failure_retries_then_continues(caplog):
    client = _FailingClient()
    tracer = Tracer(client=client)

    step_ran = False
    with caplog.at_level(logging.WARNING, logger="angent.observability.tracing"):
        with tracer.trace_step("flaky", input={}) as handle:
            # The body must run uninterrupted despite the backend failing.
            step_ran = True
            handle.output = "done"
            tracer.record_llm_span("p", "r", token_count=1)

    assert step_ran is True
    # trace() retried exactly max_retries times.
    assert client.trace_calls == DEFAULT_MAX_RETRIES
    # No backend trace handle since creation failed.
    assert handle.trace is None
    # Failure was logged and the step still completed.
    assert handle.end_time is not None
    assert any("failed after" in r.message.lower() for r in caplog.records)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
