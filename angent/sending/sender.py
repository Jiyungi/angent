"""The Sender interface, its backends, and gate-routed sending.

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
* :class:`GmailAgentSender` — the **Airbyte Gmail connector alternate**
  (Requirement 10.4). It speaks the same confirmed two-step Airbyte Agents-API
  OAuth flow the Scanner's GitHub source uses (``POST
  https://api.airbyte.com/v1/applications/token`` for a bearer token, then the
  Agents API connectors endpoint with ``Authorization: Bearer`` plus
  ``X-Organization-Id``). It is **only usable when the Gmail tier is unlocked**;
  because that tier is not generally unlocked, :meth:`GmailAgentSender.is_available`
  detects unavailability and :meth:`GmailAgentSender.send` returns
  ``SendResult(ok=False, error=...)`` gracefully rather than raising.

Backend selection (Requirement 10.2, 10.4)
-------------------------------------------
:func:`select_sender` returns the :class:`SmtpSender` **by default**. It returns
a :class:`GmailAgentSender` only when the caller opts in (``prefer_gmail_agent``)
*and* the Airbyte Gmail tier is detected as unlocked. Any time the tier is not
unlocked the selection falls back to SMTP, so the reliable default always wins.

Gate-routed sending (Requirement 9.7, 10.5–10.7)
------------------------------------------------
:func:`send_via_gate` (and the thin :class:`GatedSender` wrapper) route **every**
send through :meth:`GovernanceGate.authorize_send` first and reject any send the
gate did not PERMIT:

* On **BLOCK / DEFER** the backend is never called, the email budget is **not**
  decremented, and the gate decision is carried back on the result.
* On **PERMIT** the backend send is wrapped in a hard **30-second timeout** using
  a worker thread (Windows-safe — no ``signal``). A timeout is treated exactly
  like a failure (Requirement 10.5).
* A failed/timed-out send leaves the draft **eligible for retry** and does **not**
  decrement the budget; the ``emails`` row's ``attempt_count`` is incremented
  (Requirement 10.6).
* After **3 consecutive failures** the ``emails`` row is marked ``failed = 1`` and
  removed from retry eligibility (Requirement 10.7).

Mark-sent / mark-failed persistence (Requirement 10.3, 10.7, 22)
----------------------------------------------------------------
All ``emails`` row mutations share the **read-latest + version-bumped rewrite**
pattern the Writer and Governance Gate use against the ``emails``
``ReplacingMergeTree(version)`` table: read the highest-``version`` row for the
``email_id``, apply the change (preserving every other column), bump ``version``
to ``max(version)+1`` and stamp a fresh tz-aware UTC ``updated_at``, then
re-insert so the just-written row is the latest-version winner any later read
observes. Persistence is best-effort and resilient: a ``None`` client, a missing
row, or a write failure is logged and never turns a real delivery outcome into a
crash.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Protocol, Tuple, runtime_checkable

# Reuse the canonical ``emails`` column order so reads/rewrites line up with the
# Writer's inserts and the EMAILS_DDL schema (single source of truth).
from angent.agents.writer import EMAILS_COLUMNS
from angent.governance.gate import Decision, GovernanceGate, RateWindow, SendDecision
from angent.models import Draft

logger = logging.getLogger("angent.sending.sender")

# Backend identifiers recorded on the ``emails`` ``sender_backend`` column
# (LowCardinality values 'smtp' | 'gmail_agent').
SMTP_BACKEND = "smtp"
GMAIL_AGENT_BACKEND = "gmail_agent"

# Display name used on the outgoing message's From header.
DEFAULT_FROM_NAME = "Angent"

# Hard send timeout: a send that does not complete within this many seconds is
# treated as a failure (Requirement 10.5).
DEFAULT_SEND_TIMEOUT = 30.0

# Number of consecutive failures after which the ``emails`` row is marked
# ``failed`` and removed from retry eligibility (Requirement 10.7).
MAX_CONSECUTIVE_FAILURES = 3

# Airbyte Agents-API endpoints (identical to the Scanner's confirmed flow).
_AIRBYTE_TOKEN_URL = "https://api.airbyte.com/v1/applications/token"
_AIRBYTE_CONNECTORS_URL = "https://api.airbyte.ai/api/v1/integrations/connectors"


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


@dataclass
class AttemptOutcome:
    """Result of recording a failed/timed-out send attempt against ``emails``.

    Attributes:
        attempt_count: The draft's new cumulative ``attempt_count`` after this
            failure.
        marked_failed: ``True`` once ``attempt_count`` reached
            :data:`MAX_CONSECUTIVE_FAILURES`, meaning the row was marked
            ``failed = 1`` and removed from retry eligibility (Requirement 10.7).
        persisted: ``True`` when the ``emails`` row update was written; ``False``
            when there was no client, the row was missing, or the write failed.
    """

    attempt_count: int
    marked_failed: bool
    persisted: bool


class _EmailsBackend:
    """Shared ``emails`` row-update logic for every Sender backend.

    Holds the ClickHouse client and clock and implements the read-latest +
    version-bumped rewrite used to mark a row ``sent`` (Requirement 10.3) or to
    record a failed attempt / mark it ``failed`` after 3 consecutive failures
    (Requirements 10.6, 10.7). Subclasses (:class:`SmtpSender`,
    :class:`GmailAgentSender`) add the transport-specific :meth:`send`.
    """

    def __init__(
        self,
        client: Optional[Any] = None,
        *,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._client = client
        self._now: Callable[[], datetime] = now or (lambda: datetime.now(timezone.utc))

    # -- public row mutations ------------------------------------------------

    def mark_sent(self, draft: Draft, sent_at: Optional[datetime]) -> bool:
        """Mark the draft's ``emails`` row ``sent`` with ``sent_at`` (Req 10.3, 22).

        Sets ``sent = 1`` / ``failed = 0`` / ``sent_at`` / clears
        ``failure_reason`` while preserving all other columns. Best-effort: a
        ``None`` client, a missing row, or a write failure is logged and returns
        ``False`` without raising — a real successful delivery is never turned
        into a crash.
        """
        row = self._require_row(draft, action="mark sent")
        if row is None:
            return False
        return self._rewrite_email_row(
            row,
            {
                "sent": 1,
                "failed": 0,
                "sent_at": self._as_utc(sent_at),
                "failure_reason": None,
            },
        )

    def record_failed_attempt(
        self,
        draft: Draft,
        reason: Optional[str],
        max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES,
    ) -> AttemptOutcome:
        """Record a failed/timed-out send attempt against the ``emails`` row.

        Increments ``attempt_count`` and stores ``failure_reason`` while keeping
        ``sent = 0`` so the draft stays eligible for retry (Requirement 10.6).
        Once ``attempt_count`` reaches ``max_consecutive_failures`` the row is
        marked ``failed = 1`` and is no longer retry-eligible (Requirement 10.7).

        Best-effort: with no client or a missing/unwritable row the count is
        estimated from ``draft.attempt_count`` and ``persisted=False`` is returned
        so the caller still learns whether the failed-after-3 threshold was hit.
        """
        row = self._read_latest_email(draft.email_id) if self._client is not None else None
        if row is None:
            attempts = int(getattr(draft, "attempt_count", 0) or 0) + 1
            marked_failed = attempts >= max_consecutive_failures
            if self._client is None:
                logger.debug(
                    "Sender: no client; estimating attempt_count=%d for draft %s.",
                    attempts,
                    getattr(draft, "email_id", "?"),
                )
            else:
                logger.warning(
                    "Sender: emails row for draft %s not found; cannot persist "
                    "failed attempt.",
                    getattr(draft, "email_id", "?"),
                )
            return AttemptOutcome(attempts, marked_failed, persisted=False)

        try:
            current_attempts = int(row.get("attempt_count") or 0)
        except (TypeError, ValueError):
            current_attempts = 0
        attempts = current_attempts + 1
        marked_failed = attempts >= max_consecutive_failures

        persisted = self._rewrite_email_row(
            row,
            {
                "attempt_count": attempts,
                "failure_reason": reason,
                "sent": 0,
                "failed": 1 if marked_failed else 0,
            },
        )
        return AttemptOutcome(attempts, marked_failed, persisted)

    # -- read-latest + version-bumped rewrite -------------------------------

    def _require_row(self, draft: Draft, *, action: str) -> Optional[dict[str, Any]]:
        """Return the latest ``emails`` row for ``draft`` or ``None`` (logged)."""
        if self._client is None:
            logger.debug(
                "Sender: no ClickHouse client; skipping %s for draft %s.",
                action,
                getattr(draft, "email_id", "?"),
            )
            return None
        row = self._read_latest_email(draft.email_id)
        if row is None:
            logger.warning(
                "Sender: emails row for draft %s not found; cannot %s.",
                getattr(draft, "email_id", "?"),
                action,
            )
        return row

    def _rewrite_email_row(self, row: dict[str, Any], changes: dict[str, Any]) -> bool:
        """Re-insert ``row`` with ``changes`` applied, ``version`` bumped, fresh ``updated_at``.

        The new row carries ``max(version)+1`` and a fresh tz-aware UTC
        ``updated_at`` so it wins on the ``ReplacingMergeTree``; nullable
        timestamps are normalized to stay tz-stable on read-back. Returns ``True``
        on a persisted insert, ``False`` otherwise (logged, never raises).
        """
        if self._client is None:
            return False
        try:
            current_version = int(row.get("version") or 0)
        except (TypeError, ValueError):
            current_version = 0

        new_row = {
            **row,
            **changes,
            "updated_at": self._as_utc(self._now()),
            "version": current_version + 1,
        }
        new_row["sent_at"] = self._as_utc(new_row.get("sent_at"))
        new_row["created_at"] = self._as_utc(new_row.get("created_at"))

        ordered = [new_row[col] for col in EMAILS_COLUMNS]
        try:
            result = self._client.insert("emails", [ordered], list(EMAILS_COLUMNS))
        except Exception as exc:  # noqa: BLE001 - persistence failure -> log, don't raise
            logger.error(
                "Sender: emails rewrite insert raised for draft %s: %s",
                row.get("email_id"),
                exc,
            )
            return False

        if not getattr(result, "ok", False):
            logger.error(
                "Sender: emails rewrite insert not-ok for draft %s: %s",
                row.get("email_id"),
                getattr(result, "error", "insert returned not-ok"),
            )
            return False
        return True

    def _read_latest_email(self, draft_id: str) -> Optional[dict[str, Any]]:
        """Return the latest ``emails`` row for ``draft_id`` as a column dict.

        Selects the highest-``version`` row (``ORDER BY version DESC LIMIT 1``),
        the current state on the ``ReplacingMergeTree(version)``. Returns ``None``
        when the draft has no rows or the read fails, so callers treat a read
        failure the same as "not found" and never fabricate a row.
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
                "Sender: emails read failed for draft %s: %s",
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


class SmtpSender(_EmailsBackend):
    """Default Sender backend: Gmail SMTP via :mod:`angent.email_sender` (Req 10.2).

    Wraps ``email_sender.send_email`` (the reliable App-Password SMTP path). On a
    successful send it marks the draft's ``emails`` row ``sent`` with the
    returned timestamp (Requirement 10.3); on failure it returns a
    :class:`SendResult` carrying the reason and leaves the row untouched (the
    draft stays eligible for retry — retry/failed-after-3 handling lives in
    :func:`send_via_gate`).

    Recipient resolution
    ---------------------
    The :class:`~angent.models.Draft` model carries no explicit recipient
    address, and for the demo every email is delivered to the **controlled
    inbox** for safety (Requirement 19.4). ``SmtpSender`` therefore resolves the
    ``to_address`` in priority order: an explicit constructor ``to_address`` then
    the configured Gmail address (``config.gmail.address`` / ``GMAIL_ADDRESS``).
    If none can be resolved the send fails fast with a clear reason rather than
    attempting an addressless send.

    Args:
        client: A :class:`~angent.persistence.clickhouse.ClickHouseClient` (or a
            compatible object exposing ``query`` and ``insert``) used to mark the
            ``emails`` row sent. When ``None``, sending still works and the result
            is returned, but the mark-sent persistence step is skipped.
        config: An :class:`~angent.config.Config`; when ``None`` it is loaded from
            the environment. Supplies the default Gmail recipient address.
        to_address: Explicit recipient override; highest priority when set.
        from_name: Display name for the outgoing From header.
        send_email: Injectable send function with the signature
            ``send_email(to_address, subject, body, from_name) -> dict`` (defaults
            to :func:`angent.email_sender.send_email`). Lets tests substitute a
            fake without sending real mail.
        now: Injectable clock returning the success timestamp; defaults to
            tz-aware UTC ``now``.
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
        super().__init__(client, now=now)
        if config is None:
            from angent.config import load_config

            config = load_config()
        self._config = config
        self._to_address = to_address
        self._from_name = from_name or DEFAULT_FROM_NAME
        if send_email is None:
            from angent.email_sender import send_email as _send_email

            send_email = _send_email
        self._send_email = send_email

    # -- public interface ----------------------------------------------------

    def send(self, draft: Draft) -> SendResult:
        """Deliver ``draft`` via Gmail SMTP and mark it sent on success.

        Resolves the recipient (see class docstring), calls the wrapped
        ``send_email(to, draft.subject, draft.body, from_name)``, and maps the
        returned dict to a :class:`SendResult`. On success the result carries a
        fresh tz-aware UTC ``sent_at`` and the draft's ``emails`` row is updated to
        ``sent = 1`` with that timestamp (Requirement 10.3, 22). On failure a
        :class:`SendResult` with the reason is returned and no row is rewritten.
        """
        to_address = self._resolve_recipient()
        if not to_address:
            reason = "no recipient address resolved (set to_address or GMAIL_ADDRESS)"
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
        self.mark_sent(draft, sent_at)
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


class GmailAgentSender(_EmailsBackend):
    """Alternate Sender backend: the Airbyte Gmail connector (Requirement 10.4).

    Speaks the same confirmed two-step Airbyte Agents-API OAuth flow the
    Scanner's GitHub source uses: ``POST
    https://api.airbyte.com/v1/applications/token`` (client_id / client_secret /
    grant_type=client_credentials) for a bearer token, then the Agents API
    connectors endpoint with ``Authorization: Bearer`` plus ``X-Organization-Id``.

    Tier gating
    -----------
    The Gmail send connector is only usable when the **Airbyte Gmail tier is
    unlocked**. That tier is not generally unlocked, so this backend is built to
    detect unavailability and degrade gracefully rather than crash:

    * :meth:`is_available` returns ``True`` only when Airbyte is configured *and*
      either an explicit ``tier_unlocked=True`` was passed or a probe of the
      connectors catalog finds a Gmail connector. The probe result is cached.
    * :meth:`send` returns ``SendResult(ok=False, error=...)`` (never raises) when
      the tier is unavailable, the token exchange fails, or the connector send
      reports failure, so the gated-send path treats it as an ordinary,
      retry-eligible failure.

    On a successful connector send the ``emails`` row is marked ``sent`` exactly
    like :class:`SmtpSender` (shared :meth:`_EmailsBackend.mark_sent`).

    Args:
        client: ClickHouse client used to mark the ``emails`` row sent (optional).
        config: An :class:`~angent.config.Config`; loaded from env when ``None``.
        session: Injectable ``requests``-style session for the Airbyte HTTP calls.
        tier_unlocked: Explicit override of tier availability (skips the probe).
            ``None`` (default) means "detect via the connectors catalog".
        timeout: Per-request HTTP timeout for the Airbyte calls.
        now: Injectable clock for the success timestamp / ``updated_at``.
    """

    backend_name = GMAIL_AGENT_BACKEND

    # Connector-catalog field/name hints used to detect a Gmail send connector.
    _CONNECTOR_CONTAINER_KEYS = (
        "connectors",
        "available",
        "records",
        "results",
        "items",
        "data",
    )
    _CONNECTOR_NAME_KEYS = ("name", "connector", "connector_name", "type", "id", "slug")

    def __init__(
        self,
        client: Optional[Any] = None,
        *,
        config: Optional[Any] = None,
        session: Optional[Any] = None,
        tier_unlocked: Optional[bool] = None,
        timeout: float = DEFAULT_SEND_TIMEOUT,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        super().__init__(client, now=now)
        if config is None:
            from angent.config import load_config

            config = load_config()
        self._config = config
        self._airbyte = getattr(config, "airbyte", None)
        self._session = session
        self._timeout = timeout
        # ``None`` means "not yet probed"; a bool is an explicit/cached verdict.
        self._tier_unlocked: Optional[bool] = tier_unlocked

    # -- availability detection ---------------------------------------------

    def is_available(self) -> bool:
        """Return ``True`` only when the Airbyte Gmail tier is unlocked.

        Requires Airbyte credentials to be configured. With an explicit
        ``tier_unlocked`` override that value is used directly; otherwise a
        one-time probe of the connectors catalog looks for a Gmail connector and
        the result is cached. Any error during the probe degrades to ``False``.
        """
        if self._airbyte is None or not getattr(self._airbyte, "is_configured", False):
            return False
        if self._tier_unlocked is not None:
            return self._tier_unlocked
        self._tier_unlocked = self._detect_gmail_tier()
        return self._tier_unlocked

    def _detect_gmail_tier(self) -> bool:
        """Probe the Agents-API connectors catalog for a Gmail send connector."""
        token = self._get_token()
        if not token:
            return False
        payload = self._get_connectors(token)
        if payload is None:
            return False
        return self._catalog_has_gmail(payload)

    @classmethod
    def _catalog_has_gmail(cls, payload: object) -> bool:
        """Defensively scan an uncertain connectors payload for 'gmail'."""
        def _names(container: object):
            if isinstance(container, list):
                for item in container:
                    if isinstance(item, dict):
                        for key in cls._CONNECTOR_NAME_KEYS:
                            value = item.get(key)
                            if isinstance(value, str):
                                yield value
                    elif isinstance(item, str):
                        yield item

        if isinstance(payload, dict):
            for key in cls._CONNECTOR_CONTAINER_KEYS:
                for name in _names(payload.get(key)):
                    if "gmail" in name.lower():
                        return True
        elif isinstance(payload, list):
            for name in _names(payload):
                if "gmail" in name.lower():
                    return True
        return False

    # -- public interface ----------------------------------------------------

    def send(self, draft: Draft) -> SendResult:
        """Send ``draft`` via the Airbyte Gmail connector (only if tier unlocked).

        Returns ``SendResult(ok=False, error=...)`` — never raises — when the tier
        is unavailable, the token exchange fails, or the connector send reports
        failure, so the gated-send path treats it as a retry-eligible failure. On
        success the ``emails`` row is marked ``sent`` with a fresh tz-aware UTC
        timestamp (Requirement 10.3).
        """
        if not self.is_available():
            reason = (
                "Airbyte Gmail tier not unlocked; GmailAgentSender unavailable "
                "(use the SMTP default)"
            )
            logger.info(
                "GmailAgentSender: %s (draft %s).",
                reason,
                getattr(draft, "email_id", "?"),
            )
            return SendResult(ok=False, error=reason)

        token = self._get_token()
        if not token:
            return SendResult(
                ok=False, error="Airbyte token exchange failed for Gmail connector"
            )

        ok, error = self._send_via_connector(token, draft)
        if not ok:
            logger.warning(
                "GmailAgentSender: connector send failed for draft %s: %s",
                getattr(draft, "email_id", "?"),
                error,
            )
            return SendResult(ok=False, error=error or "Gmail connector send failed")

        sent_at = self._as_utc(self._now())
        self.mark_sent(draft, sent_at)
        logger.info(
            "GmailAgentSender: draft %s delivered via Airbyte Gmail at %s.",
            getattr(draft, "email_id", "?"),
            sent_at,
        )
        return SendResult(ok=True, sent_at=sent_at)

    # -- Airbyte Agents-API HTTP (mirrors the Scanner's confirmed flow) ------

    def _ensure_session(self) -> Any:
        """Lazily create a ``requests.Session`` if one was not injected."""
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def _get_token(self) -> Optional[str]:
        """Exchange client_id/client_secret for a bearer ``access_token``.

        Mirrors the Scanner's confirmed token exchange. Any timeout, transport,
        or parse error — or a missing token — returns ``None`` so the caller
        degrades gracefully.
        """
        if self._airbyte is None or not getattr(self._airbyte, "is_configured", False):
            return None
        session = self._ensure_session()
        try:
            response = session.post(
                _AIRBYTE_TOKEN_URL,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json={
                    "client_id": self._airbyte.client_id,
                    "client_secret": self._airbyte.client_secret,
                    "grant_type": "client_credentials",
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:  # noqa: BLE001 - any failure -> no token, degrade gracefully
            return None
        if not isinstance(payload, dict):
            return None
        token = payload.get("access_token")
        return token if isinstance(token, str) and token else None

    def _get_connectors(self, token: str) -> Optional[object]:
        """GET the Agents-API connectors catalog (for tier detection)."""
        session = self._ensure_session()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        org_id = getattr(self._airbyte, "organization_id", None)
        if org_id:
            headers["X-Organization-Id"] = org_id
        try:
            response = session.get(
                _AIRBYTE_CONNECTORS_URL,
                params={"workspace_name": "default"},
                headers=headers,
                timeout=self._timeout,
            )
            response.raise_for_status()
            return response.json()
        except Exception:  # noqa: BLE001 - any failure -> no catalog
            return None

    def _send_via_connector(self, token: str, draft: Draft) -> Tuple[bool, Optional[str]]:
        """Attempt the Gmail connector send; return ``(ok, error)``.

        Because the exact Gmail-send contract is gated behind the unlocked tier
        and not available in this environment, this issues a best-effort POST to
        the connectors endpoint and treats any non-2xx / transport / parse error
        as a clean, catchable failure (never raises). When the tier is genuinely
        unlocked this is the single place to refine the request/response shape.
        """
        session = self._ensure_session()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        org_id = getattr(self._airbyte, "organization_id", None)
        if org_id:
            headers["X-Organization-Id"] = org_id

        gmail = getattr(self._config, "gmail", None)
        to_address = getattr(gmail, "address", None) if gmail is not None else None
        body = {
            "connector": "gmail",
            "action": "send",
            "message": {
                "to": to_address,
                "subject": draft.subject,
                "body": draft.body,
            },
        }
        try:
            response = session.post(
                _AIRBYTE_CONNECTORS_URL,
                headers=headers,
                json=body,
                timeout=self._timeout,
            )
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - any failure -> catchable error result
            return False, f"{type(exc).__name__}: {exc}"
        return True, None


# --- backend selection ------------------------------------------------------


def select_sender(
    config: Optional[Any] = None,
    client: Optional[Any] = None,
    *,
    prefer_gmail_agent: bool = False,
    session: Optional[Any] = None,
    tier_unlocked: Optional[bool] = None,
    **smtp_kwargs: Any,
) -> Sender:
    """Select the send backend, defaulting to :class:`SmtpSender` (Req 10.2, 10.4).

    The reliable SMTP path is always the default. A :class:`GmailAgentSender` is
    returned **only** when the caller opts in via ``prefer_gmail_agent`` *and* the
    Airbyte Gmail tier is detected as unlocked (or forced via ``tier_unlocked``).
    Any time the tier is not unlocked the selection falls back to SMTP.

    Args:
        config: The :class:`~angent.config.Config`; loaded from env when ``None``.
        client: ClickHouse client passed to the chosen backend for row updates.
        prefer_gmail_agent: Opt in to the Gmail connector when its tier is unlocked.
        session: Optional HTTP session forwarded to :class:`GmailAgentSender`.
        tier_unlocked: Optional explicit Gmail-tier override (skips the probe).
        **smtp_kwargs: Extra keyword args forwarded to :class:`SmtpSender`
            (e.g. ``to_address``, ``from_name``, ``send_email``).

    Returns:
        A :class:`Sender` — :class:`GmailAgentSender` when explicitly preferred and
        unlocked, otherwise the default :class:`SmtpSender`.
    """
    if config is None:
        from angent.config import load_config

        config = load_config()

    if prefer_gmail_agent:
        gmail_agent = GmailAgentSender(
            client,
            config=config,
            session=session,
            tier_unlocked=tier_unlocked,
        )
        if gmail_agent.is_available():
            logger.info("select_sender: Gmail agent tier unlocked; using GmailAgentSender.")
            return gmail_agent
        logger.info(
            "select_sender: Gmail agent tier not unlocked; defaulting to SmtpSender."
        )

    return SmtpSender(client, config=config, **smtp_kwargs)


# --- gate-routed sending ----------------------------------------------------


@dataclass
class GatedSendResult:
    """Outcome of a gate-routed send (:func:`send_via_gate`).

    Attributes:
        permitted: ``True`` only when the gate returned PERMIT and the backend was
            invoked. ``False`` for a BLOCK/DEFER (the backend was never called).
        sent: ``True`` only when a permitted send actually succeeded.
        decision: The :class:`~angent.governance.gate.SendDecision` the gate
            returned (carries PERMIT/BLOCK/DEFER, reason, and pending flag).
        result: The backend :class:`SendResult` when a send was attempted; ``None``
            for a non-permitted send or a timeout.
        budget_consumed: ``True`` only on a successful send. A block, deferral,
            failure, or timeout never decrements the budget (Requirement 10.6).
        attempt_count: The draft's ``attempt_count`` after a failed/timed-out send.
        marked_failed: ``True`` once 3 consecutive failures marked the row
            ``failed`` (Requirement 10.7).
        retry_eligible: ``True`` when the draft may be retried — a deferral, or a
            failure/timeout that has not yet reached the 3-failure threshold.
        timed_out: ``True`` when the permitted send exceeded the timeout
            (Requirement 10.5).
    """

    permitted: bool
    sent: bool
    decision: SendDecision
    result: Optional[SendResult] = None
    budget_consumed: bool = False
    attempt_count: int = 0
    marked_failed: bool = False
    retry_eligible: bool = False
    timed_out: bool = False


def _send_with_timeout(
    backend: Sender, draft: Draft, timeout: Optional[float]
) -> Tuple[Optional[SendResult], bool]:
    """Run ``backend.send(draft)`` under a hard timeout (Windows-safe).

    Uses a single worker thread and ``Future.result(timeout=...)`` rather than
    ``signal`` so it works on Windows and off the main thread. Returns
    ``(result, timed_out)``: on timeout ``(None, True)``; if ``send`` raises, the
    exception is captured as a failure :class:`SendResult` ``(result, False)``.
    """
    if timeout is None or timeout <= 0:
        try:
            return backend.send(draft), False
        except Exception as exc:  # noqa: BLE001 - treat as failure result
            return SendResult(ok=False, error=f"{type(exc).__name__}: {exc}"), False

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(backend.send, draft)
        try:
            return future.result(timeout=timeout), False
        except FuturesTimeout:
            return None, True
        except Exception as exc:  # noqa: BLE001 - treat as failure result
            return SendResult(ok=False, error=f"{type(exc).__name__}: {exc}"), False
    finally:
        # Don't block on a hung send thread; let it wind down in the background.
        executor.shutdown(wait=False)


def send_via_gate(
    gate: GovernanceGate,
    backend: Sender,
    draft: Draft,
    sent_count: int,
    budget: int,
    window: RateWindow,
    *,
    investor_id: Optional[str] = None,
    timeout: Optional[float] = DEFAULT_SEND_TIMEOUT,
    max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES,
) -> GatedSendResult:
    """Route a single send through the Governance Gate (Requirements 9.7, 10.5–10.7).

    Every send first passes :meth:`GovernanceGate.authorize_send`; the backend is
    invoked **only** on PERMIT and the result is rejected for any other verdict:

    * **BLOCK / DEFER** — the backend is never called and the budget is not
      decremented. A DEFER leaves the draft retry-eligible; a BLOCK does not.
    * **PERMIT** — the send runs under a 30-second timeout (Requirement 10.5). On
      success the budget is consumed (the caller advances ``sent_count``). On a
      failure or timeout the budget is left untouched, ``attempt_count`` is
      incremented, and the draft stays retry-eligible — until 3 consecutive
      failures mark the row ``failed`` and remove it from retry eligibility
      (Requirements 10.6, 10.7).

    Args:
        gate: The :class:`~angent.governance.gate.GovernanceGate`.
        backend: The selected :class:`Sender` backend.
        draft: The draft to send.
        sent_count: Cumulative emails already sent this run (for the budget check).
        budget: The hard ``email_budget`` cap.
        window: The current :class:`~angent.governance.gate.RateWindow`.
        investor_id: Optional id recorded for the audit trail.
        timeout: Hard send timeout in seconds (``None``/``<=0`` disables it).
        max_consecutive_failures: Failures before the row is marked ``failed``.

    Returns:
        A :class:`GatedSendResult` describing the verdict and outcome.
    """
    decision = gate.authorize_send(draft, sent_count, budget, window)

    if not decision.permitted:
        # Rejected by the gate: do NOT send, do NOT decrement the budget.
        retry_eligible = decision.decision is Decision.DEFER
        logger.info(
            "send_via_gate: draft %s %s by gate (%s); not sent, budget unchanged.",
            getattr(draft, "email_id", "?"),
            decision.decision.value,
            decision.reason or "n/a",
        )
        return GatedSendResult(
            permitted=False,
            sent=False,
            decision=decision,
            result=None,
            budget_consumed=False,
            retry_eligible=retry_eligible,
        )

    # PERMIT: attempt the send under a hard timeout.
    result, timed_out = _send_with_timeout(backend, draft, timeout)

    if result is not None and result.ok:
        logger.info(
            "send_via_gate: draft %s sent (budget consumed).",
            getattr(draft, "email_id", "?"),
        )
        return GatedSendResult(
            permitted=True,
            sent=True,
            decision=decision,
            result=result,
            budget_consumed=True,
            retry_eligible=False,
        )

    # Failure or timeout: leave eligible for retry, do NOT decrement budget,
    # increment attempt_count, and mark failed after 3 consecutive failures.
    if timed_out:
        reason = f"send timed out after {timeout:g}s"
    else:
        reason = (result.error if result is not None else None) or "send failed"

    recorder = getattr(backend, "record_failed_attempt", None)
    if callable(recorder):
        outcome = recorder(draft, reason, max_consecutive_failures)
    else:
        attempts = int(getattr(draft, "attempt_count", 0) or 0) + 1
        outcome = AttemptOutcome(
            attempts, attempts >= max_consecutive_failures, persisted=False
        )

    logger.warning(
        "send_via_gate: draft %s send failed (%s); attempt %d, %s.",
        getattr(draft, "email_id", "?"),
        reason,
        outcome.attempt_count,
        "marked failed (no more retries)"
        if outcome.marked_failed
        else "eligible for retry",
    )
    return GatedSendResult(
        permitted=True,
        sent=False,
        decision=decision,
        result=result if result is not None else SendResult(ok=False, error=reason),
        budget_consumed=False,
        attempt_count=outcome.attempt_count,
        marked_failed=outcome.marked_failed,
        retry_eligible=not outcome.marked_failed,
        timed_out=timed_out,
    )


class GatedSender:
    """Thin wrapper binding a :class:`GovernanceGate` and a :class:`Sender` backend.

    Convenience over :func:`send_via_gate` for callers that hold a fixed gate +
    backend pair: ``GatedSender(gate, backend).send(draft, sent_count, budget,
    window)`` routes every send through the gate with the same semantics.
    """

    def __init__(
        self,
        gate: GovernanceGate,
        backend: Sender,
        *,
        timeout: Optional[float] = DEFAULT_SEND_TIMEOUT,
        max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES,
    ) -> None:
        self._gate = gate
        self._backend = backend
        self._timeout = timeout
        self._max_consecutive_failures = max_consecutive_failures

    def send(
        self,
        draft: Draft,
        sent_count: int,
        budget: int,
        window: RateWindow,
        *,
        investor_id: Optional[str] = None,
    ) -> GatedSendResult:
        """Route ``draft`` through the bound gate + backend (see :func:`send_via_gate`)."""
        return send_via_gate(
            self._gate,
            self._backend,
            draft,
            sent_count,
            budget,
            window,
            investor_id=investor_id,
            timeout=self._timeout,
            max_consecutive_failures=self._max_consecutive_failures,
        )
