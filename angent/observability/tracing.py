"""Langfuse tracing for the Angent control loop, with a safe disabled fallback.

Built per the Langfuse skill (github.com/langfuse/skills →
references/instrumentation.md) and the current Langfuse Python SDK docs
(https://langfuse.com/docs/observability/overview). Best practices applied:

- **Framework integration over manual instrumentation.** LLM calls are traced
  automatically by the OpenAI drop-in (see :mod:`angent.observability.llm`),
  which records model name, token usage, cost, latency and API errors as
  *generation* observations — no hand-rolled LLM spans needed.
- **Descriptive, nested spans.** :meth:`Tracer.trace_step` opens a named span
  per agent step (``scanner.scan``, ``qualifier.qualify``, …) via the SDK's
  ``start_as_current_observation``; OpenAI generations nest underneath the
  active step automatically.
- **Explicit, masked input/output.** Step input/output are set explicitly via
  ``update_current_span`` so traces stay readable and don't leak unrelated args.
- **Trace attributes.** :meth:`Tracer.propagate` attaches ``session_id`` (the
  run id) and ``tags`` so a run's Ticks group together in the Langfuse UI.
- **Flush before exit.** :meth:`Tracer.flush` is called at the end of the run so
  a short-lived process actually ships its traces.

Disabled fallback (Requirement 13.3): if Langfuse is not configured at startup
(missing ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY``) or the SDK is not
installed, the tracer runs as a no-op and logs once that tracing is disabled.
Any tracing error degrades gracefully and never interrupts the step
(Requirement 13.4).

Credentials (env, loaded from ``.env`` before this module is used):
``LANGFUSE_PUBLIC_KEY``, ``LANGFUSE_SECRET_KEY``, ``LANGFUSE_BASE_URL`` (the SDK
also accepts ``LANGFUSE_HOST``).
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Optional

logger = logging.getLogger("angent.observability.tracing")

# Retained for backward compatibility with callers/tests that referenced it.
DEFAULT_MAX_RETRIES = 3


@dataclass
class StepHandle:
    """A handle to an in-flight step, returned by :meth:`Tracer.trace_step`.

    Callers always receive one (even when tracing is disabled) so their
    ``with tracer.trace_step(...) as h:`` blocks behave identically and can set
    ``h.output``. ``span`` is the backend observation when tracing is live,
    else ``None``.
    """

    step_id: str
    input: Any
    start_time: float
    output: Any = None
    end_time: Optional[float] = None
    span: Any = None
    # Back-compat alias: older code referenced ``handle.trace``.
    trace: Any = None


def _langfuse_configured() -> bool:
    """True when Langfuse public + secret keys are present in the environment."""
    return bool(
        os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")
    )


class Tracer:
    """Thin wrapper over the Langfuse SDK with a safe disabled fallback.

    Args:
        client: An explicit Langfuse client (mainly for tests). When provided,
            tracing is enabled regardless of env credentials. When omitted, the
            tracer initializes the global client via ``langfuse.get_client()``
            only if credentials are configured.
        enabled: Force-disable tracing by passing ``False`` (used by callers
            that want to opt out). ``None`` means "auto-detect from env".
    """

    def __init__(
        self,
        *,
        client: Any = None,
        enabled: Optional[bool] = None,
    ) -> None:
        self.client: Any = None
        self.enabled: bool = False
        self._current: Optional[StepHandle] = None

        if enabled is False:
            logger.info("Langfuse tracing disabled by caller; loop runs untraced.")
            return

        # An injected client (tests) wins and means "enabled".
        if client is not None:
            self.client = client
            self.enabled = True
            logger.info("Langfuse tracing enabled (injected client).")
            return

        if not _langfuse_configured():
            # Requirement 13.3 — unconfigured at startup: run untraced, log once.
            logger.info(
                "Langfuse not configured (LANGFUSE_PUBLIC_KEY/SECRET_KEY missing); "
                "tracing disabled. Loop will run untraced."
            )
            return

        try:
            from langfuse import get_client  # imported after env is loaded

            self.client = get_client()
            # Best-effort connectivity check; never fatal.
            try:
                if not self.client.auth_check():
                    logger.warning(
                        "Langfuse auth_check failed; continuing (traces may be dropped)."
                    )
            except Exception:  # noqa: BLE001 - auth_check is best-effort
                pass
            self.enabled = True
            logger.info("Langfuse tracing enabled.")
        except Exception as exc:  # noqa: BLE001 - import/init failure -> disabled
            logger.warning(
                "Langfuse configured but client init failed (%s); tracing disabled.",
                exc,
            )
            self.client = None
            self.enabled = False

    # -- trace attributes ----------------------------------------------------

    @contextmanager
    def propagate(
        self,
        *,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> Iterator[None]:
        """Attach trace attributes (session/user/tags) to enclosed observations.

        Wrap a run with this so all of its Ticks share a ``session_id`` (the run
        id) and ``tags`` in the Langfuse UI. Safe no-op when disabled or when the
        SDK lacks ``propagate_attributes``.
        """
        if not self.enabled or self.client is None:
            yield
            return
        try:
            from langfuse import propagate_attributes

            kwargs: dict[str, Any] = {}
            if session_id is not None:
                kwargs["session_id"] = session_id
            if user_id is not None:
                kwargs["user_id"] = user_id
            if tags is not None:
                kwargs["tags"] = tags
            with propagate_attributes(**kwargs):
                yield
        except Exception as exc:  # noqa: BLE001 - never break the caller
            logger.debug("propagate_attributes unavailable/failed: %s", exc)
            yield

    # -- step spans ----------------------------------------------------------

    @contextmanager
    def trace_step(self, step_id: str, input: Any = None) -> Iterator[StepHandle]:
        """Open a named span for one agent step (Requirement 13.1).

        Records the step id + input on entry and the output + duration on exit
        via ``update_current_span``. OpenAI generations created inside the block
        nest under this span automatically. Always yields a :class:`StepHandle`;
        tracing errors degrade to a no-op without interrupting the step.
        """
        handle = StepHandle(step_id=step_id, input=input, start_time=time.time())
        previous = self._current
        self._current = handle

        if not self.enabled or self.client is None:
            try:
                yield handle
            finally:
                handle.end_time = time.time()
                self._current = previous
            return

        cm = None
        try:
            cm = self.client.start_as_current_observation(
                as_type="span", name=step_id, input=input
            )
            span = cm.__enter__()
            handle.span = span
            handle.trace = span  # back-compat alias
        except Exception as exc:  # noqa: BLE001 - tracing must not break the step
            logger.warning("Tracing: failed to start span '%s' (%s); continuing.", step_id, exc)
            cm = None

        try:
            yield handle
        finally:
            handle.end_time = time.time()
            if cm is not None:
                try:
                    self.client.update_current_span(
                        output=handle.output,
                        metadata={"duration_s": handle.end_time - handle.start_time},
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Tracing: update_current_span failed: %s", exc)
                try:
                    cm.__exit__(None, None, None)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Tracing: span exit failed: %s", exc)
            self._current = previous

    def record_llm_span(
        self,
        prompt: Any,
        response: Any,
        token_count: Optional[int] = None,
        *,
        name: str = "llm-call",
    ) -> None:
        """Record a manual LLM generation under the current step.

        With the OpenAI drop-in (:mod:`angent.observability.llm`) LLM calls are
        already traced automatically, so this is only needed for non-OpenAI
        calls. Safe no-op when disabled or when there is no active step.
        """
        if not self.enabled or self.client is None:
            return
        try:
            with self.client.start_as_current_observation(
                as_type="generation",
                name=name,
                input=prompt,
                output=response,
                metadata={"token_count": token_count} if token_count is not None else None,
            ):
                pass
        except Exception as exc:  # noqa: BLE001 - never break the caller
            logger.debug("Tracing: record_llm_span failed: %s", exc)

    def flush(self) -> None:
        """Flush buffered events so a short-lived process ships its traces."""
        if not self.enabled or self.client is None:
            return
        try:
            self.client.flush()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Tracing: flush failed: %s", exc)


def build_tracer(**kwargs: Any) -> Tracer:
    """Convenience factory mirroring :class:`Tracer` construction."""
    return Tracer(**kwargs)
