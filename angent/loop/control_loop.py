"""The goal-driven Control Loop orchestrator (Requirements 1, 2, 3, 22).

This module owns the lifecycle of a single run. The piece implemented here is
:meth:`ControlLoop.start` — the **all-or-nothing goal initiation** (Requirement
1): validate the submission, then on success persist the Goal + start time +
initial :class:`~angent.models.LoopState` to ClickHouse within 2 seconds and
**before** the first Tick. If that persistence fails, the loop is not started,
any partial record is removed, and an *init-incomplete* error is returned so the
caller never observes a half-created run (Requirements 1.5, 1.6, 22).

Design references:
  * design.md → Components and Interfaces → "Control Loop and Planner".
  * design.md → "Goal Validation" (the pure :func:`validate_goal` gate).
  * design.md → "Retry and Persistence" (goal init is the one all-or-nothing
    exception to the otherwise retain-and-continue persistence contract).

``evaluate_termination`` (Requirement 3) and ``run_tick`` (Requirement 2) are
implemented in later tasks (13.1 and 13.5 respectively); they are declared here
as stubs so imports and wiring stay intact without claiming functionality.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from ..models import Goal, LoopState, StopReason, TickOutcome
from ..persistence.clickhouse import ClickHouseClient
from .validation import validate_goal

logger = logging.getLogger("angent.loop.control_loop")

# Requirement 1.5: the initial state must be persisted within 2 seconds.
PERSIST_BUDGET_SECONDS = 2.0

# Default planner-tunable knobs for the initial state (match LoopState defaults).
DEFAULT_THESIS_BREADTH = 0.5
DEFAULT_EMAIL_ANGLE = ""
DEFAULT_SEND_VOLUME = 0


@dataclass(frozen=True)
class RunHandle:
    """A handle to a successfully-initiated run.

    Returned by :meth:`ControlLoop.start` only when the Goal was both valid and
    its initial :class:`LoopState` was durably persisted (all-or-nothing). The
    caller uses ``run_id`` to address the run on the ClickHouse blackboard and
    ``state`` to drive the first Tick.
    """

    run_id: str
    state: LoopState


@dataclass(frozen=True)
class StartResult:
    """Outcome of :meth:`ControlLoop.start` — success xor a typed failure.

    Exactly one of the success/failure shapes is populated:

      * Success: ``ok=True`` and ``run_handle`` set; ``error_kind`` is ``None``.
      * Validation failure: ``ok=False``, ``error_kind="validation"`` and
        ``offending_field`` naming the rejected field (Requirements 1.3, 1.4).
        Nothing is persisted.
      * Init-incomplete: ``ok=False``, ``error_kind="init-incomplete"`` — the
        Goal was valid but the initial state could not be persisted; any partial
        record has been removed and the loop was not started (Requirement 1.6).
    """

    ok: bool
    run_handle: Optional[RunHandle] = None
    error_kind: Optional[str] = None  # "validation" | "init-incomplete"
    offending_field: Optional[str] = None
    message: str = ""

    def __bool__(self) -> bool:  # truthiness mirrors success
        return self.ok


class ControlLoop:
    """Owns a run's lifecycle: initiation now, Ticks + termination later.

    Args:
        client: The ClickHouse blackboard client. When omitted, one is created
            lazily from environment config via
            :meth:`ClickHouseClient.from_config`.
        now: Injectable clock (zero-arg callable returning ``datetime``) used for
            both goal validation and the recorded ``started_at``. Defaults to
            :func:`datetime.now`. Injecting it keeps initiation deterministic in
            tests.
        persist_budget_seconds: Soft budget for the initial persist
            (Requirement 1.5); exceeding it is logged as a warning but does not
            by itself fail initiation.
    """

    def __init__(
        self,
        client: Optional[ClickHouseClient] = None,
        *,
        now: Optional[Callable[[], datetime]] = None,
        persist_budget_seconds: float = PERSIST_BUDGET_SECONDS,
    ) -> None:
        self._client = client
        self._now: Callable[[], datetime] = now or datetime.now
        self.persist_budget_seconds = persist_budget_seconds

    @property
    def client(self) -> ClickHouseClient:
        """The ClickHouse client, created lazily from config on first use."""
        if self._client is None:
            self._client = ClickHouseClient.from_config()
        return self._client

    # -- goal initiation (Requirement 1) -------------------------------------

    def start(self, thesis: str, goal: Goal) -> StartResult:
        """Validate, then all-or-nothing persist the initial state (Requirement 1).

        Flow:
          1. Validate ``thesis`` + ``goal`` via :func:`validate_goal`. On failure
             return a ``validation`` :class:`StartResult` naming the offending
             field; **nothing is persisted** (Requirements 1.3, 1.4).
          2. On success, generate a ``run_id`` and build the initial
             :class:`LoopState` (tick_index=0, emails_sent=0, reply_rate=0,
             status="running", started_at=now, default knobs), then persist it
             within the 2-second budget and before any Tick (Requirements 1.2,
             1.5).
          3. If persistence fails, remove any partial write for the run and
             return an ``init-incomplete`` :class:`StartResult`; the loop is not
             started (Requirement 1.6).

        Returns:
            A :class:`StartResult`. On success ``ok`` is True with a
            :class:`RunHandle`; otherwise ``ok`` is False with a typed
            ``error_kind`` and a human-readable ``message``.
        """
        # 1. Validate (pure, no I/O). Convert the structured Goal to the mapping
        #    shape validate_goal expects, and share the same injectable clock.
        goal_input = {
            "target_metric": goal.target_metric,
            "deadline": goal.deadline,
            "email_budget": goal.email_budget,
        }
        validation = validate_goal(thesis, goal_input, now=self._now)
        if not validation.ok:
            logger.info(
                "Goal rejected during validation (field=%s): %s",
                validation.offending_field,
                validation.message,
            )
            return StartResult(
                ok=False,
                error_kind="validation",
                offending_field=validation.offending_field,
                message=validation.message,
            )

        # 2. Build the initial state and persist it (all-or-nothing).
        run_id = uuid.uuid4().hex
        started_at = self._now()
        state = LoopState(
            run_id=run_id,
            goal=goal,
            started_at=started_at,
            tick_index=0,
            emails_sent=0,
            reply_rate=0.0,
            thesis_breadth=DEFAULT_THESIS_BREADTH,
            email_angle=DEFAULT_EMAIL_ANGLE,
            send_volume=DEFAULT_SEND_VOLUME,
            metric_history=[],
            status="running",
            stop_reason=None,
        )

        persist_started = time.monotonic()
        result = self.client.write_loop_state(state)
        elapsed = time.monotonic() - persist_started
        if elapsed > self.persist_budget_seconds:
            # Requirement 1.5 is a soft budget here: log but do not fail solely
            # on slowness if the write ultimately succeeded.
            logger.warning(
                "Initial loop_state persist for run_id=%s took %.2fs (budget %.2fs)",
                run_id,
                elapsed,
                self.persist_budget_seconds,
            )

        # 3. On persistence failure: clean up any partial write, do not start.
        if not result.ok:
            logger.error(
                "Initial loop_state persist failed for run_id=%s: %s — "
                "cleaning up and returning init-incomplete",
                run_id,
                result.error,
            )
            self._cleanup_partial_write(run_id)
            return StartResult(
                ok=False,
                error_kind="init-incomplete",
                message=(
                    "initialization did not complete: failed to persist the "
                    f"initial loop state ({result.error})"
                ),
            )

        logger.info(
            "Run %s initiated: initial loop_state persisted in %.3fs", run_id, elapsed
        )
        return StartResult(ok=True, run_handle=RunHandle(run_id=run_id, state=state))

    def _cleanup_partial_write(self, run_id: str) -> None:
        """Remove any partial ``loop_state`` rows for ``run_id`` (best-effort).

        Goal initiation is the one all-or-nothing persistence path: if the
        initial write reports failure we must not leave a partial record behind
        (Requirement 1.6). We issue a lightweight ``ALTER TABLE ... DELETE`` for
        the run; any error here is logged but not surfaced, since the caller is
        already receiving an init-incomplete result.
        """
        try:
            cleanup = self.client.command(
                "ALTER TABLE loop_state DELETE WHERE run_id = {run_id:String}",
                parameters={"run_id": run_id},
            )
            if not cleanup.ok:
                logger.warning(
                    "Partial-write cleanup for run_id=%s did not confirm: %s",
                    run_id,
                    cleanup.error,
                )
        except Exception:  # noqa: BLE001 - cleanup must never raise to the caller
            logger.warning(
                "Partial-write cleanup for run_id=%s raised", run_id, exc_info=True
            )

    # -- per-Tick lifecycle (implemented in later tasks) ---------------------

    def evaluate_termination(
        self, state: LoopState, now: datetime
    ) -> Optional[StopReason]:
        """Return the highest-priority stop reason, or None to continue.

        Implemented in task 13.1 (Requirement 3). Declared here so wiring and
        imports remain stable.
        """
        raise NotImplementedError("evaluate_termination is implemented in task 13.1")

    def run_tick(self, state: LoopState) -> TickOutcome:
        """Run a single Tick of the pipeline and return its outcome.

        Implemented in task 13.5 (Requirement 2). Declared here so wiring and
        imports remain stable.
        """
        raise NotImplementedError("run_tick is implemented in task 13.5")
