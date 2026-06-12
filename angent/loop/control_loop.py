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

``evaluate_termination`` (Requirement 3) is implemented above, and
``run_tick`` (Requirement 2) below: each Tick checks termination first, then
runs Scanner→Qualifier→Writer→Sender→Optimizer in order with per-stage failure
containment, updates loop state + reply-rate in ClickHouse, and — via the
:meth:`ControlLoop.run` driver — on stop persists the final state + stop reason
with bounded retry (retaining it in memory on total failure).
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional

from ..models import Goal, LoopState, StopReason, TickOutcome, TickPlan
from ..persistence.clickhouse import ClickHouseClient
from .planner import Planner
from .validation import _as_naive, validate_goal

logger = logging.getLogger("angent.loop.control_loop")

# Requirement 1.5: the initial state must be persisted within 2 seconds.
PERSIST_BUDGET_SECONDS = 2.0

# Default planner-tunable knobs for the initial state (match LoopState defaults).
DEFAULT_THESIS_BREADTH = 0.5
DEFAULT_EMAIL_ANGLE = ""
DEFAULT_SEND_VOLUME = 0

# Default per-send rate-limit ceiling for the gate's RateWindow. Kept high so the
# loop's own budget (email_budget) is the binding constraint by default; a
# deployment can lower it to throttle sends per Tick.
DEFAULT_RATE_LIMIT = 1000

# Default hard send timeout (seconds) routed into ``send_via_gate`` (Req 10.5).
DEFAULT_SEND_TIMEOUT = 30.0

# Bounded retries when persisting the FINAL state on stop (Requirement 3.7/3.8).
DEFAULT_FINAL_PERSIST_ATTEMPTS = 3

# Ordered pipeline stage names, used for ``failed_stage`` reporting (Req 2.5).
STAGE_SCANNER = "Scanner"
STAGE_QUALIFIER = "Qualifier"
STAGE_WRITER = "Writer"
STAGE_SENDER = "Sender"
STAGE_OPTIMIZER = "Optimizer"


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


@dataclass(frozen=True)
class RunResult:
    """The outcome of a full :meth:`ControlLoop.run` driver loop.

    Attributes:
        final_state: The stopped :class:`LoopState` (``status="stopped"`` with
            the ``stop_reason`` set) as it was after the terminal Tick.
        stop_reason: The single prioritized :class:`StopReason` that ended the
            run, or ``None`` if the run stopped on a ``max_ticks`` safety cap
            without a natural termination condition.
        outcomes: The per-Tick :class:`TickOutcome` list, in order.
        persisted: ``True`` when the final state was durably persisted; ``False``
            when persistence exhausted its retries and the final state was
            retained in memory (Requirement 3.8).
    """

    final_state: LoopState
    stop_reason: Optional[StopReason]
    outcomes: List[TickOutcome] = field(default_factory=list)
    persisted: bool = False

    @property
    def tick_count(self) -> int:
        """Number of Ticks executed during the run (including the terminal one)."""
        return len(self.outcomes)


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
        thesis: str = "",
        planner: Optional[Planner] = None,
        scorer: Optional[Any] = None,
        scanner: Optional[Any] = None,
        qualifier: Optional[Any] = None,
        writer: Optional[Any] = None,
        gate: Optional[Any] = None,
        sender: Optional[Any] = None,
        optimizer: Optional[Any] = None,
        config: Optional[Any] = None,
        rate_limit: int = DEFAULT_RATE_LIMIT,
        send_timeout: float = DEFAULT_SEND_TIMEOUT,
        tick_interval: float = 0.0,
        final_persist_attempts: int = DEFAULT_FINAL_PERSIST_ATTEMPTS,
    ) -> None:
        self._client = client
        self._now: Callable[[], datetime] = now or datetime.now
        self.persist_budget_seconds = persist_budget_seconds
        # The investor thesis. LoopState/Goal don't store the thesis string, so
        # the ControlLoop threads it through to the Qualifier/Writer. It is set
        # here and (re)stamped on start(); run_tick also accepts a per-call
        # override.
        self._thesis = thesis
        self._config = config

        # Injectable pipeline components — lazily built from config/client on
        # first use when not supplied, so tests can pass fakes and a default
        # production wiring still works (see the lazy properties below).
        self._planner = planner
        self._scorer = scorer
        self._scanner = scanner
        self._qualifier = qualifier
        self._writer = writer
        self._gate = gate
        self._sender = sender
        self._optimizer = optimizer

        self.rate_limit = int(rate_limit)
        self.send_timeout = float(send_timeout)
        self.tick_interval = float(tick_interval)
        self.final_persist_attempts = max(1, int(final_persist_attempts))

        # Enforce "at most one Tick at a time": run_tick acquires this without
        # blocking and refuses a re-entrant/concurrent Tick (Requirement 2.1).
        self._tick_lock = threading.Lock()

        # Final states whose persistence exhausted all retries on stop are
        # retained here in memory so they are never silently lost (Req 3.8).
        self.unpersisted_final_states: List[LoopState] = []
        # Whether the most recent final-state persistence succeeded (set by
        # :meth:`_finalize`); surfaced on :class:`RunResult`.
        self._last_final_persist_ok: bool = False

    @property
    def client(self) -> ClickHouseClient:
        """The ClickHouse client, created lazily from config on first use."""
        if self._client is None:
            self._client = ClickHouseClient.from_config()
        return self._client

    # -- lazily-built pipeline components -------------------------------------
    #
    # Each component is injectable via the constructor (for tests/fakes) and
    # otherwise built once, on first use, from the shared client/config. This
    # keeps ``run_tick`` a thin orchestrator that never branches on whether a
    # component was injected.

    @property
    def planner(self) -> Planner:
        if self._planner is None:
            self._planner = Planner()
        return self._planner

    @property
    def scorer(self) -> Any:
        if self._scorer is None:
            from ..scoring.pioneer import select_scorer

            self._scorer = select_scorer(self._config)
        return self._scorer

    @property
    def scanner(self) -> Any:
        if self._scanner is None:
            from ..agents.scanner import Scanner

            self._scanner = Scanner(self.client, config=self._config)
        return self._scanner

    @property
    def qualifier(self) -> Any:
        if self._qualifier is None:
            from ..agents.qualifier import Qualifier

            self._qualifier = Qualifier(self.client, config=self._config)
        return self._qualifier

    @property
    def writer(self) -> Any:
        if self._writer is None:
            from ..agents.writer import Writer

            self._writer = Writer(self.client, config=self._config)
        return self._writer

    @property
    def gate(self) -> Any:
        if self._gate is None:
            from ..governance.gate import GovernanceGate

            self._gate = GovernanceGate(self.client, now=self._utc_now)
        return self._gate

    @property
    def sender(self) -> Any:
        if self._sender is None:
            from ..sending.sender import select_sender

            self._sender = select_sender(config=self._config, client=self.client)
        return self._sender

    def optimizer_for(self, run_id: str) -> Any:
        """Return the Optimizer, building a default bound to ``run_id`` if needed."""
        if self._optimizer is None:
            from ..agents.optimizer import Optimizer

            self._optimizer = Optimizer(self.client, run_id=run_id)
        return self._optimizer

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)

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
        # Remember the thesis so later Ticks can thread it into the Qualifier /
        # Writer (LoopState/Goal carry no thesis string).
        self._thesis = thesis
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
        """Return the single highest-priority stop reason, or None to continue.

        Pure function (no I/O): given the current ``state`` and a clock reading
        ``now``, decide whether the run should stop and, if so, *why*. The three
        terminal conditions are checked in strict priority order and the first
        match wins, so at most one :class:`StopReason` is ever returned
        (Requirements 3.1-3.4, 3.6):

          1. **goal-met** — the achieved metric has reached the target. We use
             ``state.reply_rate`` as the achieved measure of
             ``state.goal.target_metric`` (the loop's optimization signal): if
             ``state.reply_rate >= state.goal.target_metric`` return
             :attr:`StopReason.GOAL_MET` (Requirement 3.1).
          2. **deadline-reached** — ``now >= state.goal.deadline`` returns
             :attr:`StopReason.DEADLINE_REACHED` (Requirement 3.2).
          3. **email-budget-exhausted** — ``state.emails_sent >=
             state.goal.email_budget`` returns
             :attr:`StopReason.EMAIL_BUDGET_EXHAUSTED` (Requirement 3.3).

        When none match, return ``None`` to continue the loop (Requirement 3.4).
        Because the checks are ordered, if several conditions hold at once the
        highest-priority reason is reported (e.g. goal-met outranks both
        deadline-reached and email-budget-exhausted) (Requirement 3.6).

        Datetime handling: the deadline comparison coerces both ``now`` and
        ``state.goal.deadline`` to naive wall-clock time before comparing,
        matching the convention in :mod:`angent.loop.validation` (the core
        stores naive ``DateTime`` values). This keeps the comparison safe when a
        caller supplies a timezone-aware ``now`` while the stored deadline is
        naive (or vice versa).

        Args:
            state: The current :class:`LoopState` for the run.
            now: The current time (naive or timezone-aware).

        Returns:
            The highest-priority :class:`StopReason`, or ``None`` to continue.
        """
        # 1. goal-met (highest priority): achieved reply_rate vs target_metric.
        if state.reply_rate >= state.goal.target_metric:
            return StopReason.GOAL_MET

        # 2. deadline-reached: compare on a consistent (naive) basis.
        if _as_naive(now) >= _as_naive(state.goal.deadline):
            return StopReason.DEADLINE_REACHED

        # 3. email-budget-exhausted (lowest priority).
        if state.emails_sent >= state.goal.email_budget:
            return StopReason.EMAIL_BUDGET_EXHAUSTED

        # No terminal condition met — continue the loop.
        return None

    def run_tick(
        self, state: LoopState, thesis: Optional[str] = None
    ) -> TickOutcome:
        """Run a single Tick of the pipeline and return its :class:`TickOutcome`.

        Sequence per Tick (Requirements 2.1, 2.4, 2.5, 2.6, 3.5):

        1. **Termination is checked first.** :meth:`evaluate_termination` runs
           before any stage. If it returns a :class:`StopReason`, **no** stages
           run and a terminal :class:`TickOutcome` carrying that ``stop_reason``
           is returned so the run driver can stop and persist the final state
           (Requirements 2.2, 3.x).
        2. Otherwise the Planner produces this Tick's plan and the five stages
           run in order: **Scanner → Qualifier → Writer → Sender → Optimizer**.
           Each stage is wrapped in its own ``try/except``: a stage exception
           **stops the remaining stages**, records the ``failed_stage`` on the
           outcome, preserves the prior loop state, persists what is known, and
           returns so the outer driver proceeds to the next Tick (Requirement
           2.5). Any emails actually sent before the failure are still counted so
           the budget is never under-counted.
        3. After the stages (or a contained failure), the loop state + reply-rate
           are updated in ClickHouse (Requirements 2.4, 2.6, 6.8, 12.3).

        Only one Tick may run at a time: the call acquires a non-blocking lock
        and raises :class:`RuntimeError` if a Tick is already in progress
        (Requirement 2.1). The input ``state`` is never mutated — a version-
        bumped snapshot is what gets persisted, and the driver's
        :meth:`Planner.reflect` produces the next state.

        Args:
            state: The current :class:`LoopState` read at the start of the Tick.
            thesis: Optional per-call thesis override; defaults to the thesis
                stamped on :meth:`start` (threaded into Qualifier/Writer).

        Returns:
            A :class:`TickOutcome` populated with this Tick's counts, an optional
            ``failed_stage``, and (only on a terminal Tick) a ``stop_reason``.
        """
        if not self._tick_lock.acquire(blocking=False):
            # Requirement 2.1: strictly sequential — refuse a concurrent Tick.
            raise RuntimeError(
                "a Tick is already in progress; Ticks run one at a time"
            )
        try:
            return self._run_tick_locked(state, thesis)
        finally:
            self._tick_lock.release()

    def _run_tick_locked(
        self, state: LoopState, thesis: Optional[str]
    ) -> TickOutcome:
        """The body of :meth:`run_tick`, executed while holding the Tick lock."""
        outcome = TickOutcome(tick_index=state.tick_index)

        # 1. Termination is evaluated FIRST, before any stage (Req 2.2, 3.x).
        stop_reason = self.evaluate_termination(state, self._now())
        if stop_reason is not None:
            logger.info(
                "Tick %d: termination condition '%s' met before stages; "
                "running no stages.",
                state.tick_index,
                stop_reason.value,
            )
            outcome.stop_reason = stop_reason
            outcome.reply_rate = state.reply_rate
            return outcome

        active_thesis = thesis if thesis is not None else self._thesis
        plan: TickPlan = self.planner.plan(state)

        # Mutable per-stage progress; captured into the outcome on success or on
        # a contained stage failure so partial work (esp. sends) is never lost.
        candidates: list = []
        qualified: list = []
        drafts: list = []
        emails_sent_this_tick = 0
        replies_this_tick = 0
        reply_rate = state.reply_rate

        # 2. Run the five stages in order with per-stage failure containment.
        try:
            scan_result = self.scanner.scan(plan)
            candidates = list(getattr(scan_result, "candidates", []) or [])
            outcome.candidates_found = len(candidates)
        except Exception as exc:  # noqa: BLE001 - contain to this Tick (Req 2.5)
            return self._contain_failure(state, outcome, STAGE_SCANNER, exc, reply_rate)

        try:
            qualify_result = self.qualifier.qualify(
                candidates,
                active_thesis,
                self.scorer,
                threshold=plan.qualification_threshold,
            )
            qualified = list(getattr(qualify_result, "qualified", []) or [])
            outcome.qualified_count = len(qualified)
        except Exception as exc:  # noqa: BLE001
            return self._contain_failure(
                state, outcome, STAGE_QUALIFIER, exc, reply_rate
            )

        try:
            remaining_budget = max(0, state.goal.email_budget - state.emails_sent)
            draft_result = self.writer.draft(
                qualified, plan, remaining_budget, run_id=state.run_id
            )
            drafts = list(getattr(draft_result, "drafts", draft_result) or [])
            outcome.drafts_created = len(drafts)
        except Exception as exc:  # noqa: BLE001
            return self._contain_failure(state, outcome, STAGE_WRITER, exc, reply_rate)

        try:
            emails_sent_this_tick = self._send_drafts(state, drafts)
            outcome.emails_sent = emails_sent_this_tick
        except Exception as exc:  # noqa: BLE001
            # A send actually completed before the failure would have advanced
            # the counter inside _send_drafts; on an unexpected orchestration
            # error we contain and preserve what we can.
            return self._contain_failure(
                state,
                outcome,
                STAGE_SENDER,
                exc,
                reply_rate,
                emails_sent_this_tick=outcome.emails_sent,
            )

        try:
            optimizer = self.optimizer_for(state.run_id)
            collected = optimizer.collect()
            store_result = optimizer.store(collected)
            stored = list(getattr(store_result, "stored", []) or [])
            replies_this_tick = sum(
                1 for o in stored if getattr(o, "kind", None) == "reply"
            )
            # Feed newly stored outcomes to the active scorer before the next Tick.
            optimizer.feed(self.scorer, stored)
            reply_rate = optimizer.compute_reply_rate(state.run_id)
            outcome.replies = replies_this_tick
            outcome.reply_rate = reply_rate
        except Exception as exc:  # noqa: BLE001
            return self._contain_failure(
                state,
                outcome,
                STAGE_OPTIMIZER,
                exc,
                reply_rate,
                emails_sent_this_tick=emails_sent_this_tick,
            )

        # 3. Update loop state + reply-rate in ClickHouse after the Tick.
        new_emails_sent = state.emails_sent + emails_sent_this_tick
        self._persist_tick_state(state, new_emails_sent, reply_rate)

        logger.info(
            "Tick %d complete: %d candidate(s), %d qualified, %d draft(s), "
            "%d sent, %d repl(y/ies), reply_rate=%.4f.",
            state.tick_index,
            outcome.candidates_found,
            outcome.qualified_count,
            outcome.drafts_created,
            outcome.emails_sent,
            outcome.replies,
            outcome.reply_rate,
        )
        return outcome

    # -- stage orchestration helpers -----------------------------------------

    def _send_drafts(self, state: LoopState, drafts: list) -> int:
        """Route each draft through the Governance Gate; return emails sent.

        Every send passes through :func:`send_via_gate` so the gate is enforced
        on the self-driven path exactly as it would be under Guild (Requirements
        9.7, 11.1, 11.2). Unapproved drafts are BLOCKed (the default for freshly
        written drafts), budget-exhausting sends are BLOCKed, and rate-limited
        sends are DEFERred — none of which consume budget. Only a permitted,
        successful send increments the running count. The cumulative
        ``sent_count`` passed to the gate starts at ``state.emails_sent`` so the
        hard ``email_budget`` cap is honored across Ticks (Requirement 3.5).
        """
        from ..governance.gate import RateWindow
        from ..sending.sender import send_via_gate

        budget = state.goal.email_budget
        sent_count = state.emails_sent
        sent_this_tick = 0
        window_start = self._utc_now()

        for draft in drafts:
            window = RateWindow(
                sent_in_window=sent_this_tick,
                limit=self.rate_limit,
                window_start=window_start,
            )
            result = send_via_gate(
                self.gate,
                self.sender,
                draft,
                sent_count,
                budget,
                window,
                timeout=self.send_timeout,
            )
            if getattr(result, "sent", False):
                sent_this_tick += 1
                sent_count += 1
        return sent_this_tick

    def _contain_failure(
        self,
        state: LoopState,
        outcome: TickOutcome,
        stage: str,
        exc: Exception,
        reply_rate: float,
        *,
        emails_sent_this_tick: int = 0,
    ) -> TickOutcome:
        """Record a contained stage failure and persist the preserved state (Req 2.5).

        Stops the remaining stages, records ``failed_stage`` on the outcome,
        preserves the prior loop state (only advancing ``emails_sent`` by any
        sends that actually completed so the budget is never under-counted), and
        persists the loop-state update so the next Tick reads consistent state.
        Never re-raises — the outer driver proceeds to the next Tick.
        """
        logger.error(
            "Tick %d: stage %s failed (%s: %s); stopping remaining stages, "
            "preserving prior state, proceeding to next Tick.",
            state.tick_index,
            stage,
            type(exc).__name__,
            exc,
        )
        outcome.failed_stage = stage
        outcome.reply_rate = reply_rate
        new_emails_sent = state.emails_sent + max(0, emails_sent_this_tick)
        self._persist_tick_state(state, new_emails_sent, reply_rate)
        return outcome

    def _persist_tick_state(
        self, state: LoopState, emails_sent: int, reply_rate: float
    ) -> bool:
        """Write the post-Tick loop state + reply-rate to ClickHouse (Req 2.4, 12.3).

        Persists a version-bumped snapshot (the input ``state`` is not mutated)
        with the Tick's ``emails_sent`` and ``reply_rate`` via the persistence
        layer's bounded-retry ``write_loop_state``. Resilient: a write failure is
        logged (the payload is retained in the client's ``failed_payloads``) and
        never raised into the loop.
        """
        updated = dataclasses.replace(
            state, emails_sent=emails_sent, reply_rate=reply_rate
        )
        try:
            result = self.client.write_loop_state(updated)
        except Exception:  # noqa: BLE001 - persistence must not crash the Tick
            logger.warning(
                "Tick state persist for run_id=%s raised", state.run_id, exc_info=True
            )
            return False
        if not getattr(result, "ok", False):
            logger.warning(
                "Tick state persist for run_id=%s returned not-ok: %s",
                state.run_id,
                getattr(result, "error", "unknown"),
            )
            return False
        return True

    # -- run driver + final-state persistence --------------------------------

    def run(
        self,
        state: LoopState,
        thesis: Optional[str] = None,
        *,
        max_ticks: Optional[int] = None,
    ) -> "RunResult":
        """Drive the loop one Tick at a time until termination (Requirements 2, 3).

        Repeatedly calls :meth:`run_tick` (strictly sequential — never
        concurrent) and, between Ticks, folds the outcome into the next state via
        :meth:`Planner.reflect`. When a Tick returns a terminal outcome (a
        ``stop_reason``), the loop stops, the FINAL state + stop reason are
        persisted with bounded retry (retained in memory on total failure), and a
        :class:`RunResult` is returned (Requirements 3.7, 3.8).

        Args:
            state: The initial :class:`LoopState` (e.g. from :meth:`start`).
            thesis: Optional thesis override threaded into each Tick.
            max_ticks: Optional safety cap on the number of Ticks (``None`` runs
                until a natural termination condition fires).

        Returns:
            A :class:`RunResult` carrying the final state, the stop reason, the
            per-Tick outcomes, and whether the final state was persisted.
        """
        outcomes: List[TickOutcome] = []
        ticks = 0
        while True:
            if max_ticks is not None and ticks >= max_ticks:
                logger.warning(
                    "run: reached max_ticks=%d without natural termination; "
                    "stopping.",
                    max_ticks,
                )
                final = self._finalize(state, state.stop_reason)
                return RunResult(
                    final_state=final,
                    stop_reason=final.stop_reason,
                    outcomes=outcomes,
                    persisted=self._last_final_persist_ok,
                )

            outcome = self.run_tick(state, thesis)
            outcomes.append(outcome)
            ticks += 1

            if outcome.terminal:
                final = self._finalize(state, outcome.stop_reason)
                return RunResult(
                    final_state=final,
                    stop_reason=outcome.stop_reason,
                    outcomes=outcomes,
                    persisted=self._last_final_persist_ok,
                )

            # Not terminal: reflect this Tick's outcome into the next state.
            state = self.planner.reflect(state, outcome)

            if self.tick_interval > 0:
                time.sleep(self.tick_interval)

    def _finalize(
        self, state: LoopState, stop_reason: Optional[StopReason]
    ) -> LoopState:
        """Build the stopped final state and persist it with bounded retry."""
        final_state = dataclasses.replace(
            state, status="stopped", stop_reason=stop_reason
        )
        self._last_final_persist_ok = self._persist_final_state(final_state)
        return final_state

    def _persist_final_state(self, final_state: LoopState) -> bool:
        """Persist the FINAL state + stop reason with up to 3 retries (Req 3.7, 3.8).

        On stop, the loop must durably record the final state and its stop
        reason. This attempts ``write_loop_state`` up to
        :attr:`final_persist_attempts` times; on total failure the final state is
        **retained in memory** (:attr:`unpersisted_final_states`) so it is never
        silently lost, and ``False`` is returned. Returns ``True`` once the write
        succeeds.
        """
        last_error: Optional[str] = None
        for attempt in range(1, self.final_persist_attempts + 1):
            try:
                result = self.client.write_loop_state(final_state)
                if getattr(result, "ok", False):
                    logger.info(
                        "Final state for run_id=%s persisted (stop_reason=%s) on "
                        "attempt %d.",
                        final_state.run_id,
                        final_state.stop_reason.value
                        if final_state.stop_reason is not None
                        else None,
                        attempt,
                    )
                    return True
                last_error = getattr(result, "error", "write returned not-ok")
            except Exception as exc:  # noqa: BLE001 - retry, then retain in memory
                last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "Final-state persist attempt %d/%d for run_id=%s failed: %s",
                attempt,
                self.final_persist_attempts,
                final_state.run_id,
                last_error,
            )

        self.unpersisted_final_states.append(final_state)
        logger.error(
            "Final-state persist for run_id=%s failed after %d attempts; "
            "retaining final state in memory (stop_reason=%s). Last error: %s",
            final_state.run_id,
            self.final_persist_attempts,
            final_state.stop_reason.value
            if final_state.stop_reason is not None
            else None,
            last_error,
        )
        return False
