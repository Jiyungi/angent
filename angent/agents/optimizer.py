"""The Optimizer: outcome collection, durable storage, and the learning feed.

The Optimizer is the pipeline stage that closes Angent's self-improvement loop
(Requirement 6). After the Sender delivers approved drafts, reply/open
**outcomes** trickle back; the Optimizer:

1. :meth:`collect` — gathers the reply/open outcomes that still need processing.
2. :meth:`store` — persists each outcome against its email + company in the
   ``outcomes`` ClickHouse table within 5 seconds, retrying up to 3 times and
   **retaining any outcome it could not store** (with an error indication that
   identifies the affected outcome) for the next collection cycle
   (Requirements 6.1, 6.2).
3. :meth:`feed` — hands the newly stored outcomes to the *active* scorer's
   :meth:`~angent.scoring.scorer.Scorer.learn` before the next Tick begins
   (Requirement 6.3, 6.4). If a Pioneer model update fails (raises
   :class:`~angent.scoring.pioneer.PioneerScorerError` or any error), the
   Optimizer keeps the previous model, **continues scoring with it**, and
   records an error indication identifying the failed update (Requirement 6.5).
   The heuristic default and Pioneer share one interface, so :meth:`feed` never
   branches on the concrete scorer type (Requirement 7.1).

Outcome source (what :meth:`collect` returns)
--------------------------------------------
For the demo there may be no live reply webhook, so the collection source is an
**injectable provider**: pass ``outcome_source`` (a zero-argument callable
returning ``list[Outcome]``) — e.g. an adapter over a reply/open webhook, a
queue, or the seeded demo outcomes. With no provider, :meth:`collect` returns
only the outcomes **retained from previous failed store cycles** (an empty list
on a clean start), which is the clear extension point for wiring a real source.
We deliberately do *not* default to reading the ``outcomes`` table itself: that
table is a plain ``MergeTree`` (no dedupe), so collecting what we just wrote and
re-storing it would create duplicate rows. A documented :meth:`read_outcomes`
helper is provided for callers that explicitly want to load already-stored
outcomes (e.g. to feed pre-loaded seeded outcomes to the scorer).

The reply-rate computation (Requirement 6.8) is intentionally **out of scope**
for this module's storage/feed responsibility and is implemented in a later
task; :meth:`compute_reply_rate` is a documented stub here.

Nothing in :meth:`feed` mutates the scorer on failure; persistence in
:meth:`store` is resilient (a ``None`` client or a write failure is surfaced via
the returned :class:`StoreResult`, never as a crash).
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence

from angent.models import Outcome
from angent.scoring.scorer import LearnResult, Scorer

logger = logging.getLogger("angent.agents.optimizer")

# Column order for the ``outcomes`` table — mirrors OUTCOMES_DDL in
# angent/persistence/clickhouse.py exactly so inserts line up with the schema.
OUTCOMES_COLUMNS: tuple[str, ...] = (
    "outcome_id",
    "run_id",
    "email_id",
    "company_id",
    "kind",
    "occurred_at",
    "seeded",
)

# Number of store attempts per outcome (Requirement 6.2): 1 initial + retries,
# bounded so a persistently failing store does not block the loop.
DEFAULT_STORE_MAX_ATTEMPTS = 3
# Soft wall-clock budget for storing a single outcome (Requirement 6.1): "within
# 5 seconds of collection". Attempts stop once this elapses.
DEFAULT_STORE_DEADLINE_S = 5.0
# Linear back-off (seconds) between store attempts; grows per attempt but is
# kept small so all attempts fit comfortably inside the 5s budget.
DEFAULT_STORE_BACKOFF_S = 0.25


@dataclass
class StoreError:
    """An error indication for a single outcome that could not be stored.

    Identifies the affected outcome by its ids (Requirement 6.2) so a caller can
    surface exactly which reply/open event failed to persist.
    """

    outcome_id: str
    email_id: str
    company_id: str
    error: str

    def __str__(self) -> str:  # human-readable for logs / demo output
        return (
            f"outcome {self.outcome_id} (email={self.email_id}, "
            f"company={self.company_id}) failed to store: {self.error}"
        )


@dataclass
class StoreResult:
    """The outcome of a :meth:`Optimizer.store` call.

    Attributes:
        stored: Outcomes successfully persisted to the ``outcomes`` table (each
            with an assigned ``outcome_id`` and tz-aware UTC ``occurred_at``).
        unstored: Outcomes that exhausted all retries; retained for the next
            collection cycle (Requirement 6.2).
        errors: One :class:`StoreError` per unstored outcome identifying it.
    """

    stored: list[Outcome] = field(default_factory=list)
    unstored: list[Outcome] = field(default_factory=list)
    errors: list[StoreError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when every collected outcome was stored (nothing retained)."""
        return not self.unstored

    def __bool__(self) -> bool:
        return self.ok


class Optimizer:
    """Collects reply/open outcomes, stores them durably, and feeds learning.

    Args:
        client: A :class:`~angent.persistence.clickhouse.ClickHouseClient` (or a
            compatible object exposing ``insert`` and ``query``) used to persist
            outcomes to the ``outcomes`` table. When ``None``, :meth:`store`
            retains everything as unstored (there is nowhere to write).
        run_id: The current run's id, stamped onto outcomes that don't carry one
            and used to scope :meth:`read_outcomes`.
        outcome_source: Optional zero-argument callable returning the newly
            observed reply/open outcomes to process (see module docstring). When
            ``None``, :meth:`collect` yields only retained-unstored outcomes.
        max_attempts: Store attempts per outcome before retaining it (default 3).
        store_deadline_s: Soft per-outcome wall-clock budget in seconds
            (default 5.0, Requirement 6.1).
        store_backoff_s: Base linear back-off between store attempts.
        now: Injectable clock returning the ``occurred_at`` fallback / timing
            base; defaults to tz-aware UTC ``now``.
    """

    def __init__(
        self,
        client: Optional[Any] = None,
        *,
        run_id: str = "",
        outcome_source: Optional[Callable[[], Sequence[Outcome]]] = None,
        max_attempts: int = DEFAULT_STORE_MAX_ATTEMPTS,
        store_deadline_s: float = DEFAULT_STORE_DEADLINE_S,
        store_backoff_s: float = DEFAULT_STORE_BACKOFF_S,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._client = client
        self.run_id = run_id
        self._outcome_source = outcome_source
        self.max_attempts = max(1, int(max_attempts))
        self.store_deadline_s = float(store_deadline_s)
        self.store_backoff_s = float(store_backoff_s)
        self._now: Callable[[], datetime] = now or (lambda: datetime.now(timezone.utc))

        # Outcomes that exhausted retries on a prior cycle, re-offered by the
        # next collect() so they are not lost (Requirement 6.2).
        self._pending: list[Outcome] = []
        # outcome_ids already handed to a scorer's learn(), so feed() does not
        # re-learn the same outcome across Ticks.
        self._fed_ids: set[str] = set()
        # Accumulated error indications (store failures + failed model updates)
        # for observability / demo logging (Requirements 6.2, 6.5).
        self.errors: list[str] = []
        # The most recent failed Pioneer-update indication, if any (Req 6.5).
        self.last_feed_error: Optional[str] = None

    # -- collect -------------------------------------------------------------

    def collect(self) -> list[Outcome]:
        """Return the reply/open outcomes that still need to be processed.

        Combines, in order:

        1. Outcomes **retained** from previous failed store cycles (so a
           transient ClickHouse outage never drops an outcome — Requirement 6.2).
        2. Newly observed outcomes from the injected ``outcome_source`` (a reply/
           open webhook adapter, a queue, or seeded demo data), if one was
           provided.

        Outcomes carrying an ``outcome_id`` are de-duplicated so a retained
        outcome that the source also re-emits is not processed twice. The
        retained buffer is cleared here because the returned list now owns those
        outcomes; any that fail again are re-retained by the next :meth:`store`.
        """
        collected: list[Outcome] = []
        seen: set[str] = set()

        def _add(items: Sequence[Outcome]) -> None:
            for outcome in items:
                oid = getattr(outcome, "outcome_id", "") or ""
                if oid and oid in seen:
                    continue
                if oid:
                    seen.add(oid)
                collected.append(outcome)

        # 1. Retained-unstored first; the next store() decides their fate.
        _add(self._pending)
        self._pending = []

        # 2. Freshly observed outcomes from the injected source, if any.
        if self._outcome_source is not None:
            try:
                produced = list(self._outcome_source() or [])
            except Exception as exc:  # noqa: BLE001 - a bad source must not crash the loop
                logger.warning("Optimizer: outcome_source raised: %s", exc)
                produced = []
            _add(produced)

        logger.info("Optimizer: collected %d outcome(s) to process.", len(collected))
        return collected

    # -- store ---------------------------------------------------------------

    def store(self, outcomes: list[Outcome]) -> StoreResult:
        """Persist each outcome to the ``outcomes`` table within 5s, 3 retries.

        For every outcome we first normalize it — assign a ``outcome_id`` (uuid4)
        when missing, stamp the configured ``run_id`` when blank, and coerce
        ``occurred_at`` to tz-aware UTC (falling back to *now* when absent) — then
        insert a single row into ``outcomes`` (which carries the outcome's
        ``email_id`` and ``company_id``, storing it against those records,
        Requirement 6.1).

        Each insert is retried up to :attr:`max_attempts` times within the
        :attr:`store_deadline_s` budget. If all attempts fail (or there is no
        client), the outcome is **retained** for the next collection cycle and a
        :class:`StoreError` identifying it is recorded (Requirement 6.2). The
        returned :class:`StoreResult` carries the stored outcomes, the retained
        ``unstored`` outcomes, and the per-outcome errors.
        """
        result = StoreResult()
        for raw in outcomes or []:
            outcome = self._normalize(raw)
            if self._store_one(outcome):
                result.stored.append(outcome)
            else:
                error = self._last_store_error or "store failed"
                indication = StoreError(
                    outcome_id=outcome.outcome_id,
                    email_id=outcome.email_id,
                    company_id=outcome.company_id,
                    error=error,
                )
                result.unstored.append(outcome)
                result.errors.append(indication)
                self._pending.append(outcome)  # retained for the next cycle
                self.errors.append(str(indication))
                logger.error("Optimizer: %s", indication)

        logger.info(
            "Optimizer: stored %d outcome(s), retained %d unstored.",
            len(result.stored),
            len(result.unstored),
        )
        return result

    def _store_one(self, outcome: Outcome) -> bool:
        """Insert one outcome row with bounded retry inside the 5s budget.

        Returns ``True`` on a persisted insert. On total failure (no client, an
        insert that raises, or a not-ok result on every attempt) returns
        ``False`` and leaves :attr:`_last_store_error` describing the last error.
        """
        self._last_store_error = None
        if self._client is None:
            self._last_store_error = "no ClickHouse client configured"
            return False

        row = [
            outcome.outcome_id,
            outcome.run_id,
            outcome.email_id,
            outcome.company_id,
            outcome.kind,
            outcome.occurred_at,
            1 if outcome.seeded else 0,
        ]

        deadline = time.monotonic() + self.store_deadline_s
        for attempt in range(1, self.max_attempts + 1):
            try:
                insert_result = self._client.insert(
                    "outcomes", [row], list(OUTCOMES_COLUMNS)
                )
                if getattr(insert_result, "ok", True):
                    return True
                self._last_store_error = (
                    getattr(insert_result, "error", None) or "insert returned not-ok"
                )
            except Exception as exc:  # noqa: BLE001 - retry on any transport error
                self._last_store_error = f"{type(exc).__name__}: {exc}"

            logger.warning(
                "Optimizer: store attempt %d/%d for outcome %s failed: %s",
                attempt,
                self.max_attempts,
                outcome.outcome_id,
                self._last_store_error,
            )
            if attempt < self.max_attempts:
                # Respect the 5s budget: stop early if a back-off would blow it.
                backoff = self.store_backoff_s * attempt
                if time.monotonic() + backoff >= deadline:
                    self._last_store_error = (
                        f"{self._last_store_error} (5s store budget exhausted)"
                    )
                    break
                time.sleep(backoff)
        return False

    def _normalize(self, outcome: Outcome) -> Outcome:
        """Return ``outcome`` with id/run_id/occurred_at filled in (in place)."""
        if not getattr(outcome, "outcome_id", ""):
            outcome.outcome_id = str(uuid.uuid4())
        if not getattr(outcome, "run_id", ""):
            outcome.run_id = self.run_id
        outcome.occurred_at = self._as_utc(getattr(outcome, "occurred_at", None))
        return outcome

    # -- feed ----------------------------------------------------------------

    def feed(self, scorer: Scorer, outcomes: list[Outcome]) -> LearnResult:
        """Feed newly stored outcomes to the active scorer's ``learn`` (Req 6.3-6.5).

        Only outcomes not already learned from (tracked by ``outcome_id``) are
        submitted, so repeated Ticks don't re-learn the same signal. The call is
        delegated to ``scorer.learn`` uniformly — Pioneer and the heuristic share
        one interface, so this never branches on the scorer type (Requirement
        7.1, 6.4).

        If the model update fails — :class:`~angent.scoring.pioneer.PioneerScorerError`
        or any other exception — the previous model is kept (the scorer object is
        not replaced), the Optimizer **continues** (does not raise), and an error
        indication identifying the failed update is recorded in :attr:`errors`
        and :attr:`last_feed_error` (Requirement 6.5). In that case a
        :class:`~angent.scoring.scorer.LearnResult` with ``adjusted=False`` and a
        ``note`` prefixed ``ERROR:`` is returned so callers can detect the failure
        while keeping a uniform return shape.
        """
        self.last_feed_error = None
        fresh = [o for o in (outcomes or []) if self._is_unfed(o)]

        if not fresh:
            return LearnResult(
                num_outcomes=0,
                num_replies=0,
                adjusted=False,
                note="no new outcomes to feed",
            )

        try:
            learn_result = scorer.learn(fresh)
        except Exception as exc:  # noqa: BLE001 - keep previous model, continue (Req 6.5)
            indication = (
                f"scorer model update failed ({type(exc).__name__}: {exc}); "
                f"kept previous model and continued scoring with it "
                f"({len(fresh)} outcome(s) not learned)"
            )
            self.last_feed_error = indication
            self.errors.append(indication)
            logger.error("Optimizer: %s", indication)
            return LearnResult(
                num_outcomes=len(fresh),
                num_replies=sum(1 for o in fresh if getattr(o, "kind", None) == "reply"),
                adjusted=False,
                note=f"ERROR: {indication}",
            )

        # Success: mark these outcomes fed so a later Tick won't re-learn them.
        for outcome in fresh:
            oid = getattr(outcome, "outcome_id", "") or ""
            if oid:
                self._fed_ids.add(oid)
        logger.info(
            "Optimizer: fed %d outcome(s) to scorer (%s).",
            len(fresh),
            learn_result.note or "learned",
        )
        return learn_result

    def _is_unfed(self, outcome: Outcome) -> bool:
        """True when this outcome has not yet been handed to a scorer's learn()."""
        oid = getattr(outcome, "outcome_id", "") or ""
        if not oid:
            return True  # no id -> can't dedupe; treat as new
        return oid not in self._fed_ids

    # -- read helper (explicit, non-default source) --------------------------

    def read_outcomes(self, run_id: Optional[str] = None) -> list[Outcome]:
        """Load already-stored outcomes for ``run_id`` from the ``outcomes`` table.

        Provided for callers that explicitly want to feed pre-loaded/seeded
        outcomes to the scorer (Requirement 12.5). This is **not** used by
        :meth:`collect` to avoid re-storing rows into the dedupe-free
        ``outcomes`` ``MergeTree``. Returns an empty list when there is no client
        or the read fails.
        """
        if self._client is None:
            return []
        target_run = run_id if run_id is not None else self.run_id
        columns = ", ".join(OUTCOMES_COLUMNS)
        try:
            result = self._client.query(
                f"SELECT {columns} FROM outcomes "
                "WHERE run_id = {run_id:String} "
                "ORDER BY occurred_at",
                parameters={"run_id": target_run},
            )
        except Exception as exc:  # noqa: BLE001 - read failure -> empty
            logger.warning("Optimizer: read_outcomes failed: %s", exc)
            return []
        if not getattr(result, "ok", False) or not result.rows:
            return []
        return [self._row_to_outcome(row) for row in result.rows]

    @staticmethod
    def _row_to_outcome(row: Sequence[Any]) -> Outcome:
        """Reconstruct an :class:`Outcome` from an ``outcomes`` table row."""
        outcome_id, run_id, email_id, company_id, kind, occurred_at, seeded = row
        return Outcome(
            email_id=email_id,
            company_id=company_id,
            kind=kind,
            occurred_at=occurred_at,
            seeded=bool(seeded),
            run_id=run_id,
            outcome_id=outcome_id,
        )

    # -- reply-rate (implemented in a later task) ----------------------------

    def compute_reply_rate(self, run_id: str) -> float:
        """Reply-rate = replies / emails sent for the run (Requirement 6.8).

        Stubbed here on purpose: the reply-rate computation and its persistence
        are implemented in a dedicated follow-up task. Calling it now raises so
        the gap is explicit rather than silently returning a wrong number.
        """
        raise NotImplementedError(
            "compute_reply_rate is implemented in a later task (Requirement 6.8)"
        )

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _as_utc(dt: Optional[datetime]) -> datetime:
        """Normalize a datetime to tz-aware UTC (matching the core convention).

        The ``clickhouse-connect`` driver shifts *naive* datetimes from local
        time to UTC on insert, so we store tz-aware UTC values to record the
        exact instant. A missing value defaults to *now* in UTC.
        """
        if dt is None:
            return datetime.now(timezone.utc)
        if not isinstance(dt, datetime):
            return dt
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    # Initialized lazily by _store_one; declared for clarity.
    _last_store_error: Optional[str] = None
