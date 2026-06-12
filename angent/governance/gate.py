"""The Governance Gate: non-bypassable human-approval enforcement.

The :class:`GovernanceGate` is the single, non-bypassable checkpoint every send
must pass (Requirement 9). This module implements the *approval lifecycle* half
of the gate:

* :meth:`GovernanceGate.approve` marks **one specific** draft approved so — and
  only so — that draft may be sent (Requirement 9.2).
* :meth:`GovernanceGate.on_draft_modified` reverts an approved draft back to
  unapproved when its content changes, forcing a fresh Investor approval before
  it can go out (Requirement 9.3). It can optionally update the draft's subject
  and/or body in the same write.

Both operations are expressed against the ``emails`` ``ReplacingMergeTree(version)``
table on the ClickHouse blackboard: each mutation reads the latest row for the
``email_id``, flips ``approved`` (and optionally subject/body), bumps ``version``
to ``max(version)+1``, stamps a tz-aware UTC ``updated_at``, and re-inserts the
full row. Because the table is ordered by ``email_id`` and replaces on
``version``, the just-written row is the latest-version winner that any
subsequent read (this or another agent) observes (Requirement 22). All other
columns (``run_id``, ``company_id``, ``subject``, ``body``, ``angle``, ``sent``,
``failed``, ``attempt_count``, ``sender_backend``, ``sent_at``,
``failure_reason``, ``created_at``) are preserved verbatim.

The send-time decision function (``authorize_send``) is added to this same class
by task 9.2; :meth:`approve` / :meth:`on_draft_modified` are designed to sit
beside it (the gate owns the ``emails`` row lifecycle, ``authorize_send`` reads
the ``approved`` flag this code maintains).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional

# Reuse the canonical ``emails`` column order so reads/rewrites stay in lock-step
# with the Writer's inserts and the EMAILS_DDL schema (single source of truth).
from angent.agents.writer import EMAILS_COLUMNS
from angent.models import Draft

logger = logging.getLogger("angent.governance.gate")

# A send deferred longer than this many seconds is surfaced to the Investor as
# still-pending due to the rate limit (Requirement 9.6 / design "Governance Gate").
DEFERRAL_PENDING_SECONDS = 3600


class Decision(str, Enum):
    """The three send-time verdicts ``authorize_send`` can return.

    Subclasses ``str`` so the verdict serializes/compares as its lowercase
    string (``"permit"`` / ``"block"`` / ``"defer"``) for logs, audit rows, and
    JSON surfaced to the Investor — mirroring the ``StopReason`` enum style.

    * ``PERMIT`` — the send may proceed (approved, within budget, within rate).
    * ``BLOCK``  — the send is rejected for a recorded ``reason`` and must not be
      retried as-is (unapproved draft or budget exhaustion).
    * ``DEFER``  — the send is rate-limited; it should be re-attempted in the next
      rate-limit window rather than blocked outright.
    """

    PERMIT = "permit"
    BLOCK = "block"
    DEFER = "defer"


@dataclass(frozen=True)
class RateWindow:
    """The current rate-limit window state passed to :meth:`authorize_send`.

    A minimal, pure value object: it carries only what the decision needs to
    judge the rate limit and how long a deferral has lasted. It performs no I/O —
    the caller (Sender/orchestrator) computes ``sent_in_window`` from the
    blackboard and hands it in.

    Attributes:
        sent_in_window: Number of sends already made inside the current window.
        limit: The configured maximum sends per window (Requirement 9.5). When a
            send would push usage to ``>= limit`` the send is deferred.
        window_start: The tz-aware instant the current rate-limit window began.
            This is also the moment from which a deferral is measured: when a send
            is deferred and ``now - window_start`` exceeds
            :data:`DEFERRAL_PENDING_SECONDS`, the deferral is reported as pending
            (Requirement 9.6). ``None`` means the window has no recorded start, so
            no elapsed time can be computed and the deferral is not yet pending.
    """

    sent_in_window: int
    limit: int
    window_start: Optional[datetime] = None


@dataclass(frozen=True)
class SendDecision:
    """The verdict returned by :meth:`authorize_send` (a pure decision).

    Attributes:
        decision: One of :class:`Decision` — ``PERMIT``, ``BLOCK``, or ``DEFER``.
        reason: Short machine-readable cause, recorded with blocks/deferrals and
            surfaced to the Investor. Empty for ``PERMIT``; one of
            ``'unapproved'`` / ``'email-budget'`` (for ``BLOCK``) or
            ``'rate-limit'`` (for ``DEFER``).
        pending: ``True`` only for a ``DEFER`` whose deferral has lasted longer
            than :data:`DEFERRAL_PENDING_SECONDS` (3600s); it signals the send
            "remains pending due to the rate limit" (Requirement 9.6). Always
            ``False`` for ``PERMIT`` and ``BLOCK``.
        message: Human-readable explanation surfaced to the Investor.
    """

    decision: Decision
    reason: str = ""
    pending: bool = False
    message: str = ""

    @property
    def permitted(self) -> bool:
        """``True`` only when the send may proceed (``decision is PERMIT``)."""
        return self.decision is Decision.PERMIT


@dataclass
class ApprovalResult:
    """Outcome of an :meth:`GovernanceGate.approve` call.

    Attributes:
        ok: ``True`` when the draft was found and marked approved; ``False`` when
            the draft does not exist or the rewrite could not be persisted.
        draft_id: The ``email_id`` the approval targeted.
        message: A short human-readable explanation surfaced to the Investor.
    """

    ok: bool
    draft_id: str
    message: str = ""

    def __bool__(self) -> bool:  # truthiness mirrors success
        return self.ok


class GovernanceGate:
    """Owns the approval lifecycle of ``emails`` rows on the blackboard.

    Args:
        client: A :class:`~angent.persistence.clickhouse.ClickHouseClient` (or a
            compatible object exposing ``query`` and ``insert``) used to read the
            latest ``emails`` row and re-insert the version-bumped rewrite.
        now: Injectable clock returning the ``updated_at`` instant; defaults to
            tz-aware UTC ``now`` so writes are deterministic and tz-stable.
    """

    def __init__(
        self,
        client: Any,
        *,
        now: Optional[Any] = None,
    ) -> None:
        self._client = client
        self._now = now or (lambda: datetime.now(timezone.utc))

    # -- public approval lifecycle ------------------------------------------

    def approve(self, draft_id: str, investor_id: str) -> ApprovalResult:
        """Mark exactly the draft ``draft_id`` approved (Requirement 9.2).

        Reads the latest ``emails`` row for ``email_id == draft_id``, sets
        ``approved = 1`` while preserving every other column, bumps ``version``
        to ``max(version)+1``, and re-inserts so the approved row wins on the
        ``ReplacingMergeTree``. Only this draft is affected — no other draft's
        approval state changes.

        Args:
            draft_id: The ``email_id`` of the draft to approve.
            investor_id: The approving Investor's id (recorded in the log/audit
                trail; the ``emails`` schema has no investor column so it is not
                persisted as a row field in this task).

        Returns:
            An :class:`ApprovalResult`. ``ok`` is ``False`` when the draft is not
            found or the rewrite fails to persist; ``True`` otherwise.
        """
        row = self._read_latest_email(draft_id)
        if row is None:
            logger.info(
                "GovernanceGate.approve: draft %s not found; nothing to approve.",
                draft_id,
            )
            return ApprovalResult(
                ok=False,
                draft_id=draft_id,
                message=f"Draft {draft_id} not found; cannot approve.",
            )

        ok = self._rewrite_with_bumped_version(row, approved=True)
        if not ok:
            return ApprovalResult(
                ok=False,
                draft_id=draft_id,
                message=f"Draft {draft_id} found but approval could not be persisted.",
            )

        logger.info(
            "GovernanceGate.approve: draft %s approved by investor %s.",
            draft_id,
            investor_id,
        )
        return ApprovalResult(
            ok=True,
            draft_id=draft_id,
            message=f"Draft {draft_id} approved.",
        )

    def on_draft_modified(
        self,
        draft_id: str,
        *,
        new_subject: Optional[str] = None,
        new_body: Optional[str] = None,
    ) -> None:
        """Revert an approved draft to unapproved after a content change (Req 9.3).

        Reads the latest ``emails`` row for ``draft_id``, sets ``approved = 0``
        (so the modified draft requires fresh Investor approval before it can be
        sent), optionally replaces the ``subject`` and/or ``body`` with the
        supplied new content, bumps ``version`` and stamps ``updated_at``, and
        re-inserts. Preserves all other columns.

        A missing draft is a no-op (logged). This method intentionally returns
        ``None`` to match the design's ``on_draft_modified(draft_id) -> None``
        contract; persistence failures are logged (the gate is fail-safe — a draft
        whose revert did not persist is treated conservatively at send time, where
        ``authorize_send`` blocks anything not currently approved).
        """
        row = self._read_latest_email(draft_id)
        if row is None:
            logger.info(
                "GovernanceGate.on_draft_modified: draft %s not found; no revert needed.",
                draft_id,
            )
            return

        if new_subject is not None:
            row["subject"] = new_subject
        if new_body is not None:
            row["body"] = new_body

        ok = self._rewrite_with_bumped_version(row, approved=False)
        if ok:
            logger.info(
                "GovernanceGate.on_draft_modified: draft %s reverted to unapproved "
                "(requires fresh approval before sending).",
                draft_id,
            )
        else:
            logger.error(
                "GovernanceGate.on_draft_modified: failed to persist unapproval revert "
                "for draft %s; it remains pending re-approval at send time.",
                draft_id,
            )

    # -- send-time authorization (pure decision, no I/O) --------------------

    def authorize_send(
        self,
        draft: Draft,
        sent_count: int,
        budget: int,
        window: RateWindow,
        *,
        now: Optional[datetime] = None,
    ) -> SendDecision:
        """Decide whether ``draft`` may be sent right now (Reqs 9.4, 9.5, 9.6).

        A **pure**, deterministic decision function: it reads only its arguments
        (no DB reads, no inserts, no clocks beyond the optionally-injected
        ``now``) and returns a :class:`SendDecision`. The Sender / Guild
        orchestrator routes every send through this and acts on the verdict;
        blocks and deferrals never advance the budget counter.

        Decision order (first match wins):

        1. **Unapproved** — if ``not draft.approved`` -> ``BLOCK('unapproved')``.
           Nothing goes out without a still-valid human approval (Req 9.1/9.2).
        2. **Budget** — if ``sent_count + 1 > budget`` -> ``BLOCK('email-budget')``.
           Sending would push the cumulative sent count past the hard cap, so the
           send is blocked and the reason recorded (Req 9.4, 3.5).
        3. **Rate limit** — if ``window.sent_in_window >= window.limit`` ->
           ``DEFER('rate-limit')`` until the next window (Req 9.5). If the
           deferral has already lasted longer than 3600s
           (``now - window.window_start``), the returned decision is marked
           ``pending`` so the Investor sees the send still pending (Req 9.6).
        4. Otherwise -> ``PERMIT``.

        Args:
            draft: The draft under consideration; only ``approved`` (and
                ``email_id`` for messaging) is inspected.
            sent_count: Cumulative emails already sent in this run.
            budget: The hard ``email_budget`` cap.
            window: The current :class:`RateWindow` (in-window count, limit, and
                window/deferral start instant).
            now: Optional injected tz-aware "current time" used only to measure a
                deferral's age; defaults to the gate's clock. Keeping it a
                parameter preserves purity/determinism for tests.

        Returns:
            A :class:`SendDecision` describing PERMIT / BLOCK / DEFER, the reason,
            and (for long deferrals) the pending indication.
        """
        draft_id = getattr(draft, "email_id", "")

        # (1) Human-approval gate: block anything not currently approved.
        if not draft.approved:
            return SendDecision(
                decision=Decision.BLOCK,
                reason="unapproved",
                message=(
                    f"Send blocked: draft {draft_id} is not approved; "
                    "Investor approval is required before sending."
                ),
            )

        # (2) Budget cap: block if this send would exceed the email_budget.
        if sent_count + 1 > budget:
            return SendDecision(
                decision=Decision.BLOCK,
                reason="email-budget",
                message=(
                    f"Send blocked: sending draft {draft_id} would exceed the "
                    f"email budget ({sent_count} sent, budget {budget})."
                ),
            )

        # (3) Rate limit: defer while the current window is saturated.
        if window.sent_in_window >= window.limit:
            pending = self._deferral_exceeds_pending(window, now)
            if pending:
                message = (
                    f"Send pending: draft {draft_id} has been deferred more than "
                    f"{DEFERRAL_PENDING_SECONDS}s due to the rate limit and "
                    "remains pending."
                )
            else:
                message = (
                    f"Send deferred: draft {draft_id} is rate-limited "
                    f"({window.sent_in_window}/{window.limit} sent in window); "
                    "will retry at the start of the next window."
                )
            return SendDecision(
                decision=Decision.DEFER,
                reason="rate-limit",
                pending=pending,
                message=message,
            )

        # (4) All gates clear: permit the send.
        return SendDecision(
            decision=Decision.PERMIT,
            message=f"Send permitted for draft {draft_id}.",
        )

    def _deferral_exceeds_pending(
        self, window: RateWindow, now: Optional[datetime]
    ) -> bool:
        """Return ``True`` when this deferral has lasted longer than 3600s.

        Measures ``now - window.window_start``. With no ``window_start`` there is
        no elapsed time to measure, so the deferral is not yet pending. Both
        instants are normalized to tz-aware UTC so naive/aware inputs compare
        safely. Pure aside from the optionally-injected ``now`` (falls back to the
        gate's clock).
        """
        start = self._as_utc(window.window_start)
        if start is None:
            return False
        current = self._as_utc(now) if now is not None else self._as_utc(self._now())
        if current is None:
            return False
        return (current - start) > timedelta(seconds=DEFERRAL_PENDING_SECONDS)

    # -- shared read-latest / rewrite-with-bumped-version helpers -----------

    def _read_latest_email(self, draft_id: str) -> Optional[dict[str, Any]]:
        """Return the latest ``emails`` row for ``draft_id`` as a column dict.

        Selects the highest-``version`` row (``ORDER BY version DESC LIMIT 1``),
        which on the ``ReplacingMergeTree(version)`` is the current state. Returns
        ``None`` when the draft has no rows or the read fails (so callers treat a
        read failure the same as "not found" and never fabricate a row).
        """
        columns = ", ".join(EMAILS_COLUMNS)
        try:
            result = self._client.query(
                f"SELECT {columns} FROM emails "
                "WHERE email_id = {draft_id:String} "
                "ORDER BY version DESC LIMIT 1",
                parameters={"draft_id": draft_id},
            )
        except Exception as exc:  # noqa: BLE001 - read failure -> treat as not found
            logger.warning(
                "GovernanceGate: emails read failed for draft %s: %s",
                draft_id,
                exc,
            )
            return None

        if not getattr(result, "ok", False) or not result.rows:
            return None

        return dict(zip(EMAILS_COLUMNS, result.rows[0]))

    def _rewrite_with_bumped_version(
        self, row: dict[str, Any], *, approved: bool
    ) -> bool:
        """Re-insert ``row`` with ``approved`` set, ``version`` bumped, ``updated_at`` now.

        The new row carries ``max(version)+1`` (taken from the read-back row's
        ``version``) and a fresh tz-aware UTC ``updated_at`` so it is the
        latest-version winner on the ``ReplacingMergeTree``. Every other column is
        preserved from ``row``. Returns ``True`` on a persisted insert, ``False``
        otherwise.
        """
        try:
            current_version = int(row.get("version") or 0)
        except (TypeError, ValueError):
            current_version = 0
        next_version = current_version + 1

        updated_at = self._as_utc(self._now())

        new_row = {
            **row,
            "approved": 1 if approved else 0,
            "updated_at": updated_at,
            "version": next_version,
        }
        # Re-normalize the nullable timestamp so a read-back value stays tz-stable.
        new_row["sent_at"] = self._as_utc(new_row.get("sent_at"))
        new_row["created_at"] = self._as_utc(new_row.get("created_at"))

        ordered = [new_row[col] for col in EMAILS_COLUMNS]

        try:
            result = self._client.insert("emails", [ordered], list(EMAILS_COLUMNS))
        except Exception as exc:  # noqa: BLE001 - persistence failure -> report False
            logger.error(
                "GovernanceGate: emails rewrite insert raised for draft %s: %s",
                row.get("email_id"),
                exc,
            )
            return False

        if not getattr(result, "ok", False):
            logger.error(
                "GovernanceGate: emails rewrite insert not-ok for draft %s: %s",
                row.get("email_id"),
                getattr(result, "error", "insert returned not-ok"),
            )
            return False
        return True

    @staticmethod
    def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
        """Normalize a datetime to tz-aware UTC for stable ClickHouse writes.

        Mirrors the Scanner/Qualifier/Writer ``_as_utc`` pattern: the
        ``clickhouse-connect`` driver shifts *naive* datetimes from local time to
        UTC on insert, so writing tz-aware UTC values stores the exact instant and
        keeps a read-back/rewrite identical. Naive inputs are assumed already UTC.
        """
        if dt is None:
            return None
        if not isinstance(dt, datetime):
            return dt
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
