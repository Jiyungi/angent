"""The Sender interface and its default SMTP backend.

The Sender is the pipeline stage that actually delivers an approved
:class:`~angent.models.Draft` and records the result on the blackboard. The
design defines a single, stable :class:`Sender` interface (Requirement 10.1)
that multiple backends implement so sending works regardless of the Airbyte
tier:

* :class:`SmtpSender` — the **default** backend (Requirement 10.2). It wraps the
  existing, Windows-friendly Gmail SMTP path in ``angent/email_sender.py``
  (``send_email(to_address, subject, body, from_name)`` over
  ``smtplib.SMTP_SSL("smtp.gmail.com", 465)`` with a Gmail App Password) and, on
  a successful send, marks the corresponding ``emails`` row ``sent`` with the
  returned timestamp (Requirement 10.3).
* ``GmailAgentSender`` (the Airbyte Gmail connector alternate), the 30-second
  send timeout, retry / failed-after-3 handling, the non-decrementing budget on
  failure, and gate-routing all live in **task 10.2** — this module deliberately
  implements only the protocol, the SMTP default, and mark-sent-on-success.

Mark-sent persistence (Requirement 10.3, 22)
--------------------------------------------
On a successful send the ``emails`` row for the draft is updated using the same
**read-latest + version-bumped rewrite** pattern the Writer and Governance Gate
use against the ``emails`` ``ReplacingMergeTree(version)`` table: read the
highest-``version`` row for the ``email_id``, set ``sent = 1`` and ``sent_at``
to the success timestamp (preserving every other column), bump ``version`` to
``max(version)+1`` and stamp a fresh tz-aware UTC ``updated_at``, then re-insert
so the just-written row is the latest-version winner any later read observes
(Requirement 22). The persistence step is best-effort and resilient: a ``None``
client, a missing row, or a write failure never turns a real successful delivery
into a crash — it is logged and the successful :class:`SendResult` still stands.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Protocol, runtime_checkable

# Reuse the canonical ``emails`` column order so reads/rewrites line up with the
# Writer's inserts and the EMAILS_DDL schema (single source of truth).
from angent.agents.writer import EMAILS_COLUMNS
from angent.models import Draft

logger = logging.getLogger("angent.sending.sender")

# Default backend identifier recorded for the SMTP path (matches the ``emails``
# ``sender_backend`` LowCardinality values 'smtp' | 'gmail_agent').
SMTP_BACKEND = "smtp"

# Display name used on the outgoing message's From header.
DEFAULT_FROM_NAME = "Angent"


@dataclass
class SendResult:
    """The outcome of a single :meth:`Sender.send` call (design "Sender Interface").

    Attributes:
        ok: ``True`` when the backend reported a successful delivery.
        sent_at: The tz-aware UTC instant the success result was returned
            (Requirement 10.3). ``None`` on failure.
        error: A short failure reason when ``ok`` is ``False``; ``None`` on
            success.
    """

    ok: bool
    sent_at: Optional[datetime] = None
    error: Optional[str] = None


@runtime_checkable
class Sender(Protocol):
    """The stable send contract every backend implements (Requirement 10.1).

    A backend accepts an approved :class:`~angent.models.Draft` and returns
    either a success result carrying a send timestamp or a failure result
    carrying a failure reason. Marked ``runtime_checkable`` so callers/tests can
    assert ``isinstance(backend, Sender)``.
    """

    def send(self, draft: Draft) -> SendResult:  # pragma: no cover - protocol
        """Send ``draft`` and return a :class:`SendResult`."""
        ...


class SmtpSender:
    """Default Sender backend: Gmail SMTP via :mod:`angent.email_sender` (Req 10.2).

    Wraps ``email_sender.send_email`` (the reliable App-Password SMTP path). On a
    successful send it marks the draft's ``emails`` row ``sent`` with the
    returned timestamp (Requirement 10.3); on failure it returns a
    :class:`SendResult` carrying the reason and leaves the row untouched (the
    draft stays eligible for retry — retry/failed-after-3 handling is task 10.2).

    Recipient resolution
    ---------------------
    The :class:`~angent.models.Draft` model carries no explicit recipient
    address, and for the demo every email is delivered to the **controlled
    inbox** for safety (Requirement 19.4). ``SmtpSender`` therefore resolves the
    ``to_address`` in priority order: an explicit constructor ``to_address``, the
    configured Gmail address (``config.gmail.address`` / ``GMAIL_ADDRESS``), and
    finally the module's SMTP sender identity. If none can be resolved the send
    fails fast with a clear reason rather than attempting an addressless send.

    Args:
        client: A :class:`~angent.persistence.clickhouse.ClickHouseClient` (or a
            compatible object exposing ``query`` and ``insert``) used to mark the
            ``emails`` row sent. When ``None``, sending still works and the result
            is returned, but the mark-sent persistence step is skipped (useful for
            offline sends/tests).
        config: An :class:`~angent.config.Config`; when ``None`` it is loaded from
            the environment. Supplies the default Gmail recipient address.
        to_address: Explicit recipient override; highest priority when set.
        from_name: Display name for the outgoing From header.
        send_email: Injectable send function with the signature
            ``send_email(to_address, subject, body, from_name) -> dict`` (defaults
            to :func:`angent.email_sender.send_email`). Lets tests substitute a
            fake client without sending real mail.
        now: Injectable clock returning the success timestamp; defaults to
            tz-aware UTC ``now`` so writes are deterministic and tz-stable.
    """

    backend_name = SMTP_BACKEND

    def __init__(
        self,
        client: Optional[Any] = None,
        *,
        config: Optional[Any] = None,
        to_address: Optional[str] = None,
        from_name: str = DEFAULT_FROM_NAME,
        send_email: Optional[Callable[..., dict]] = None,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if config is None:
            from angent.config import load_config

            config = load_config()
        self._config = config
        self._client = client
        self._to_address = to_address
        self._from_name = from_name or DEFAULT_FROM_NAME
        if send_email is None:
            from angent.email_sender import send_email as _send_email

            send_email = _send_email
        self._send_email = send_email
        self._now: Callable[[], datetime] = now or (lambda: datetime.now(timezone.utc))

    # -- public interface ----------------------------------------------------

    def send(self, draft: Draft) -> SendResult:
        """Deliver ``draft`` via Gmail SMTP and mark it sent on success.

        Resolves the recipient (see class docstring), calls the wrapped
        ``send_email(to, draft.subject, draft.body, from_name)``, and maps the
        returned dict to a :class:`SendResult`. On success the result carries a
        fresh tz-aware UTC ``sent_at`` and the draft's ``emails`` row is updated
        to ``sent = 1`` with that timestamp (Requirement 10.3, 22). On failure a
        :class:`SendResult` with the reason is returned and no row is rewritten.

        Returns:
            A :class:`SendResult` describing the delivery outcome.
        """
        to_address = self._resolve_recipient()
        if not to_address:
            reason = (
                "no recipient address resolved (set to_address or GMAIL_ADDRESS)"
            )
            logger.error(
                "SmtpSender: cannot send draft %s: %s",
                getattr(draft, "email_id", "?"),
                reason,
            )
            return SendResult(ok=False, error=reason)

        try:
            result = self._send_email(
                to_address,
                draft.subject,
                draft.body,
                self._from_name,
            )
        except Exception as exc:  # noqa: BLE001 - any transport error -> failure result
            reason = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "SmtpSender: send raised for draft %s: %s",
                getattr(draft, "email_id", "?"),
                reason,
            )
            return SendResult(ok=False, error=reason)

        if not isinstance(result, dict) or not result.get("ok"):
            reason = (
                (result.get("error") if isinstance(result, dict) else None)
                or "SMTP send did not report success"
            )
            logger.warning(
                "SmtpSender: send failed for draft %s: %s",
                getattr(draft, "email_id", "?"),
                reason,
            )
            return SendResult(ok=False, error=reason)

        # Success: timestamp is the moment the success result was returned.
        sent_at = self._as_utc(self._now())
        self._mark_sent(draft, sent_at)
        logger.info(
            "SmtpSender: draft %s delivered to %s at %s.",
            getattr(draft, "email_id", "?"),
            to_address,
            sent_at,
        )
        return SendResult(ok=True, sent_at=sent_at)

    # -- recipient resolution ------------------------------------------------

    def _resolve_recipient(self) -> str:
        """Resolve the recipient address (explicit > configured Gmail address).

        Returns an empty string when no address can be resolved so the caller
        fails fast with a clear reason instead of attempting an addressless send.
        """
        if self._to_address:
            return self._to_address
        gmail = getattr(self._config, "gmail", None)
        address = getattr(gmail, "address", None) if gmail is not None else None
        return address or ""

    # -- mark-sent persistence (read-latest + version-bumped rewrite) --------

    def _mark_sent(self, draft: Draft, sent_at: Optional[datetime]) -> bool:
        """Mark the draft's ``emails`` row ``sent`` with ``sent_at`` (Req 10.3, 22).

        Reads the latest ``emails`` row for ``draft.email_id``, sets ``sent = 1``
        and ``sent_at`` (preserving all other columns), bumps ``version`` and
        stamps ``updated_at = now``, then re-inserts so the sent row wins on the
        ``ReplacingMergeTree``. Best-effort and resilient: a ``None`` client, a
        missing row, or a write failure is logged and returns ``False`` without
        raising — a real successful delivery is never turned into a crash.
        """
        if self._client is None:
            logger.debug(
                "SmtpSender: no ClickHouse client; skipping mark-sent for draft %s.",
                getattr(draft, "email_id", "?"),
            )
            return False

        row = self._read_latest_email(draft.email_id)
        if row is None:
            logger.warning(
                "SmtpSender: emails row for draft %s not found; cannot mark sent "
                "(delivery still succeeded).",
                getattr(draft, "email_id", "?"),
            )
            return False

        try:
            current_version = int(row.get("version") or 0)
        except (TypeError, ValueError):
            current_version = 0

        updated_at = self._as_utc(self._now())
        new_row = {
            **row,
            "sent": 1,
            "failed": 0,
            "sent_at": self._as_utc(sent_at),
            "failure_reason": None,
            "updated_at": updated_at,
            "version": current_version + 1,
        }
        # Re-normalize the other nullable/timestamp columns so a read-back value
        # stays tz-stable on the rewrite.
        new_row["created_at"] = self._as_utc(new_row.get("created_at"))

        ordered = [new_row[col] for col in EMAILS_COLUMNS]
        try:
            result = self._client.insert("emails", [ordered], list(EMAILS_COLUMNS))
        except Exception as exc:  # noqa: BLE001 - persistence failure -> log, don't raise
            logger.error(
                "SmtpSender: mark-sent insert raised for draft %s: %s "
                "(delivery still succeeded).",
                draft.email_id,
                exc,
            )
            return False

        if not getattr(result, "ok", False):
            logger.error(
                "SmtpSender: mark-sent insert not-ok for draft %s: %s "
                "(delivery still succeeded).",
                draft.email_id,
                getattr(result, "error", "insert returned not-ok"),
            )
            return False
        return True

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
                "SmtpSender: emails read failed for draft %s: %s",
                draft_id,
                exc,
            )
            return None

        if not getattr(result, "ok", False) or not result.rows:
            return None
        return dict(zip(EMAILS_COLUMNS, result.rows[0]))

    @staticmethod
    def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
        """Normalize a datetime to tz-aware UTC for stable ClickHouse writes.

        Mirrors the Writer/Governance Gate ``_as_utc`` pattern: the
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
