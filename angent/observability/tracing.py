"""Langfuse tracing wrapper with a safe disabled fallback.

This module provides a thin :class:`Tracer` that records one trace per agent
step (step id, input, output, start/end timestamps) and records each
TrueFoundry LLM call as a linked span (prompt, response, token count).

Design contract (Requirement 13):
  * 13.1 — record a trace per agent step within ~2s of completion.
  * 13.2 — record each TrueFoundry call as a span linked to the current step.
  * 13.3 — if Langfuse is not configured at startup, run untraced and log once
           that tracing is disabled.
  * 13.4 — if a trace/span write fails, retry up to 3 times, then continue the
           step uninterrupted and log the failure. NEVER raise into the caller.

The tracer is intentionally tolerant of the concrete Langfuse SDK shape. It
only depends on a small duck-typed client interface so it can be exercised with
a fake client in tests without a live Langfuse backend:

    client.trace(**kwargs) -> trace_handle
    trace_handle.update(**kwargs)
    trace_handle.span(**kwargs) -> span_handle
    span_handle.end(**kwargs)          # optional
    client.flush()                     # optional

Any method may be missing or raise; the tracer degrades gracefully.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

logger = logging.getLogger("angent.observability.tracing")

# Maximum write attempts for any single trace/span operation (Req 13.4).
DEFAULT_MAX_RETRIES = 3


@dataclass
class StepHandle:
    """A lightweight handle to an in-flight step trace.

    Even when tracing is disabled or the backend is unavailable, callers always
    receive a handle so their ``with tracer.trace_step(...)`` blocks behave
    identically. ``trace`` is the backend trace object when available, else None.
    """

    step_id: str
    input: Any
    start_time: float
    output: Any = None
    end_time: Optional[float] = None
    trace: Any = None  # backend trace handle (None when disabled / failed)


class Tracer:
    """A safe, retrying wrapper over the Langfuse SDK.

    When Langfuse is not configured (missing public/secret keys) or its SDK is
    unavailable, the tracer enters *disabled* mode: every method becomes a
    no-op and a single "tracing disabled" log line is emitted at startup.
    """

    def __init__(
        self,
        *,
        public_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        host: Optional[str] = None,
        client: Any = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self.max_retries = max(1, int(max_retries))
        self._current: Optional[StepHandle] = None
        self.client: Any = None
        self.enabled: bool = False

        # 1) An explicitly injected client (e.g. tests) always wins and means
        #    "configured" — credentials are irrelevant in that case.
        if client is not None:
            self.client = client
            self.enabled = True
            logger.info("Langfuse tracing enabled (injected client).")
            return

        # 2) Otherwise resolve credentials from args, falling back to env vars.
        public_key = public_key or os.environ.get("LANGFUSE_PUBLIC_KEY")
        secret_key = secret_key or os.environ.get("LANGFUSE_SECRET_KEY")
        host = host or os.environ.get("LANGFUSE_HOST")

        if not (public_key and secret_key):
            # Req 13.3 — unconfigured at startup: run untraced, log once.
            logger.info("Langfuse not configured; tracing disabled. Loop will run untraced.")
            return

        # 3) Configured: try to build a real client. Any failure -> disabled.
        try:
            from langfuse import Langfuse  # type: ignore

            kwargs: dict[str, Any] = {"public_key": public_key, "secret_key": secret_key}
            if host:
                kwargs["host"] = host
            self.client = Langfuse(**kwargs)
            self.enabled = True
            logger.info("Langfuse tracing enabled.")
        except Exception as exc:  # ImportError or constructor failure
            logger.warning(
                "Langfuse configured but client init failed (%s); tracing disabled.", exc
            )
            self.client = None
            self.enabled = False

    # -- internal helpers ---------------------------------------------------

    def _with_retries(self, what: str, fn) -> Any:
        """Run ``fn`` up to ``max_retries`` times, swallowing all errors.

        Returns the function result on success, or ``None`` after the final
        failure (Req 13.4 — never raise into the caller).
        """
        last_exc: Optional[BaseException] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001 - we intentionally swallow
                last_exc = exc
                logger.debug("Tracing %s attempt %d/%d failed: %s", what, attempt, self.max_retries, exc)
        logger.warning(
            "Tracing %s failed after %d retries (%s); continuing step uninterrupted.",
            what,
            self.max_retries,
            last_exc,
        )
        return None

    # -- public API ---------------------------------------------------------

    @contextmanager
    def trace_step(self, step_id: str, input: Any = None) -> Iterator[StepHandle]:
        """Record a trace for a single agent step.

        Captures start/end timestamps and the step input/output. Safe no-op
        when disabled. Always yields a :class:`StepHandle` so callers can set
        ``handle.output`` regardless of backend state.
        """
        handle = StepHandle(step_id=step_id, input=input, start_time=time.time())

        if self.enabled and self.client is not None:
            handle.trace = self._with_retries(
                "trace-start",
                lambda: self.client.trace(name=step_id, input=input),
            )

        previous = self._current
        self._current = handle
        try:
            yield handle
        finally:
            handle.end_time = time.time()
            self._current = previous
            if handle.trace is not None:
                self._with_retries(
                    "trace-end",
                    lambda: handle.trace.update(
                        output=handle.output,
                        metadata={
                            "step_id": handle.step_id,
                            "start_time": handle.start_time,
                            "end_time": handle.end_time,
                            "duration_s": handle.end_time - handle.start_time,
                        },
                    ),
                )
                # Best-effort flush so the trace lands within ~2s (Req 13.1).
                flush = getattr(self.client, "flush", None)
                if callable(flush):
                    self._with_retries("flush", flush)

    def record_llm_span(
        self,
        prompt: Any,
        response: Any,
        token_count: Optional[int] = None,
        *,
        name: str = "truefoundry-call",
    ) -> None:
        """Record a TrueFoundry LLM call as a span linked to the current step.

        Safe no-op when disabled or when there is no active step trace.
        """
        if not self.enabled or self.client is None:
            return

        trace = self._current.trace if self._current is not None else None
        if trace is None:
            # No active step trace to link to; skip silently (still no raise).
            return

        def _create_span() -> Any:
            span = trace.span(
                name=name,
                input=prompt,
                output=response,
                metadata={"token_count": token_count},
            )
            end = getattr(span, "end", None)
            if callable(end):
                end()
            return span

        self._with_retries("llm-span", _create_span)

    def flush(self) -> None:
        """Flush any buffered events. Safe no-op when disabled."""
        if not self.enabled or self.client is None:
            return
        flush = getattr(self.client, "flush", None)
        if callable(flush):
            self._with_retries("flush", flush)


def build_tracer(**kwargs: Any) -> Tracer:
    """Convenience factory mirroring :class:`Tracer` construction."""
    return Tracer(**kwargs)
