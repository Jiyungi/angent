"""The Writer: budget-respecting, personalized outreach drafting.

The Writer turns the qualified candidates the Qualifier forwarded into
personalized outreach :class:`~angent.models.Draft` rows the Governance_Gate and
Sender act on (Requirement 8). For each qualified candidate, up to the Tick's
remaining email budget, it:

1. Drafts **exactly one** personalized email (subject + body) via the
   **TrueFoundry_Gateway** — the OpenAI SDK pointed at ``TRUEFOUNDRY_BASE_URL``
   with the Bedrock-backed Claude model id — within a 30-second budget,
   incorporating the candidate's ``signals`` and the plan's ``email_angle``
   (Requirements 8.1, 8.2, 18.3). This reuses the exact gateway client setup the
   Qualifier uses (``angent/agents/qualifier.py``).
2. Stores each completed draft in the ``emails`` table as **unsent and
   unapproved** (``approved=0``, ``sent=0``, ``failed=0``) with bounded retry (up
   to 3 attempts). On total persistence failure the draft is **not discarded** —
   it is retained in the returned result and an indication that it was not
   persisted is recorded (Requirements 8.3, 8.7, 22).

Budget is respected strictly (Requirements 8.4, 8.5):

* Cumulative drafts produced in a Tick never exceed ``remaining_budget``.
* A ``remaining_budget`` of zero (or less) produces **no** drafts.
* A gateway failure/timeout for a candidate **skips that candidate without
  consuming budget**, retains the drafts already completed, and records the
  drafting failure (Requirement 8.6). Only a *successful* gateway draft consumes
  one unit of budget.

``company_id`` resolution
-------------------------
A :class:`~angent.models.Qualified` extends :class:`~angent.models.Candidate`
and carries the source natural key (``source`` + ``source_unique_id``) but **not**
the ``companies.company_id`` surrogate. The Scanner/Qualifier persist companies
keyed by ``(source, source_unique_id)``, so the Writer resolves ``company_id`` by
querying the ``companies`` table for the latest row matching that natural key. If
the row cannot be found (new/unpersisted candidate) or the read fails, the Writer
falls back to the candidate's ``source_unique_id`` as the ``company_id`` so a
draft is never dropped purely for lack of a resolved surrogate id. This choice
keeps the Writer resilient and avoids a hard dependency on companies-table
availability while still linking to the real ``company_id`` whenever present.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from angent.models import Draft, Qualified, TickPlan

logger = logging.getLogger("angent.agents.writer")


# --- Defaults ---------------------------------------------------------------

# The Bedrock-backed Claude model id routed through the TrueFoundry gateway —
# identical to the Qualifier's default so all LLM calls use one model id.
# Overridable via TRUEFOUNDRY_MODEL / OPENAI_MODEL (Requirement 18.3).
DEFAULT_TRUEFOUNDRY_MODEL = "bi-beta-bedrock/global.anthropic.claude-sonnet-4-6"

# Hard drafting budget (Requirement 8.6): the gateway call for a candidate must
# complete within 30 seconds; on timeout/error the candidate is skipped.
DEFAULT_DRAFT_TIMEOUT = 30.0

# Bounded persistence retry attempts for the ``emails`` insert (Requirement 8.7).
DEFAULT_MAX_PERSIST_ATTEMPTS = 3

# Default sender backend recorded on a fresh draft (the reliable SMTP path).
DEFAULT_SENDER_BACKEND = "smtp"

# Column order for the ``emails`` table — mirrors EMAILS_DDL in
# angent/persistence/clickhouse.py exactly so inserts line up with the schema.
EMAILS_COLUMNS: tuple[str, ...] = (
    "email_id",
    "run_id",
    "company_id",
    "subject",
    "body",
    "angle",
    "approved",
    "sent",
    "failed",
    "attempt_count",
    "sender_backend",
    "sent_at",
    "failure_reason",
    "created_at",
    "updated_at",
    "version",
)


@dataclass
class DraftFailure:
    """A per-candidate drafting or persistence failure (Requirements 8.6, 8.7).

    ``stage`` is ``"gateway"`` when the TrueFoundry draft failed/timed out (the
    candidate was skipped without consuming budget) or ``"persistence"`` when the
    draft was produced but could not be stored in ``emails`` after all retries
    (the draft is retained in the result but flagged unpersisted).
    """

    source_unique_id: str
    name: str
    stage: str
    reason: str


@dataclass
class DraftResult:
    """The outcome of a Writer pass over a batch of qualified candidates.

    Attributes:
        drafts: Every :class:`~angent.models.Draft` produced this Tick, in input
            order. A draft is present whenever the gateway returned content for
            the candidate — even if its ``emails`` persistence ultimately failed
            (Requirement 8.7); such drafts are still listed here and also recorded
            in ``failures`` with ``stage="persistence"``.
        failures: Per-candidate failures (gateway skip or persistence failure).
        count: The number of drafts produced (``len(drafts)``); equals the budget
            consumed, which never exceeds ``remaining_budget`` (Requirement 8.4).

    The result is iterable over its drafts and supports ``len(...)`` so callers
    expecting the design's ``list[Draft]`` contract keep working while also
    getting the richer failure/count detail.
    """

    drafts: list[Draft] = field(default_factory=list)
    failures: list[DraftFailure] = field(default_factory=list)

    @property
    def count(self) -> int:
        """Number of drafts produced this Tick (== budget consumed)."""
        return len(self.drafts)

    @property
    def failure_count(self) -> int:
        """How many candidates failed to draft or persist this Tick."""
        return len(self.failures)

    def __iter__(self):
        return iter(self.drafts)

    def __len__(self) -> int:
        return len(self.drafts)


class Writer:
    """Drafts one personalized email per qualified candidate, up to budget.

    Args:
        client: A :class:`~angent.persistence.clickhouse.ClickHouseClient` used to
            resolve ``company_id`` and store drafts in ``emails``. When ``None``,
            drafting still runs and drafts are returned, but persistence is
            skipped and ``company_id`` falls back to ``source_unique_id`` (useful
            for offline drafting/tests).
        config: An :class:`~angent.config.Config`; when ``None`` it is loaded from
            the environment. Supplies the TrueFoundry credentials/base URL.
        model_id: TrueFoundry model id for drafting. Defaults to the
            ``TRUEFOUNDRY_MODEL``/``OPENAI_MODEL`` env var or the Bedrock Claude id.
        draft_timeout: Per-candidate gateway budget in seconds (30s).
        max_persist_attempts: Bounded retry count for the ``emails`` insert.
        openai_client: Optional pre-built OpenAI-compatible client (eases testing
            and lets callers inject a configured gateway client).
        sender_backend: Default ``sender_backend`` stored on each draft.
        now: Injectable clock for deterministic ``created_at``/``updated_at``.
    """

    def __init__(
        self,
        client: Optional[Any] = None,
        *,
        config: Optional[Any] = None,
        model_id: Optional[str] = None,
        draft_timeout: float = DEFAULT_DRAFT_TIMEOUT,
        max_persist_attempts: int = DEFAULT_MAX_PERSIST_ATTEMPTS,
        openai_client: Optional[Any] = None,
        sender_backend: str = DEFAULT_SENDER_BACKEND,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if config is None:
            from angent.config import load_config

            config = load_config()
        self._config = config
        self._tf = getattr(config, "truefoundry", None)
        self._client = client
        self._model_id = (
            model_id
            or os.environ.get("TRUEFOUNDRY_MODEL")
            or os.environ.get("OPENAI_MODEL")
            or DEFAULT_TRUEFOUNDRY_MODEL
        )
        self._draft_timeout = draft_timeout
        self._max_persist_attempts = max(1, int(max_persist_attempts))
        self._sender_backend = sender_backend or DEFAULT_SENDER_BACKEND
        self._now: Callable[[], datetime] = now or (lambda: datetime.now(timezone.utc))
        self._openai_client = openai_client

    # -- public interface ----------------------------------------------------

    def draft(
        self,
        qualified: list[Qualified],
        plan: TickPlan,
        remaining_budget: int,
        *,
        run_id: str = "",
    ) -> DraftResult:
        """Draft one email per qualified candidate, up to ``remaining_budget``.

        Iterates the qualified candidates and, while the cumulative number of
        produced drafts is below ``remaining_budget``, drafts a personalized email
        for each via TrueFoundry (incorporating the candidate's signals and
        ``plan.email_angle``) and stores it in ``emails`` as unsent/unapproved with
        bounded retry. A gateway failure/timeout skips the candidate **without
        consuming budget** (Requirement 8.6); a persistence failure after all
        retries retains the draft and records that it was not persisted
        (Requirement 8.7).

        Args:
            qualified: Candidates forwarded by the Qualifier this Tick.
            plan: The current :class:`~angent.models.TickPlan`; supplies the
                ``email_angle`` woven into each draft.
            remaining_budget: Emails still permitted this Tick. ``<= 0`` produces
                no drafts (Requirement 8.5); otherwise cumulative drafts never
                exceed it (Requirement 8.4).
            run_id: The loop run id to stamp on each draft (threaded from loop
                state). Optional so the Writer can run standalone.

        Returns:
            A :class:`DraftResult` with the produced ``drafts``, per-candidate
            ``failures``, and a ``count`` (== budget consumed).
        """
        result = DraftResult()

        try:
            budget = int(remaining_budget)
        except (TypeError, ValueError):
            budget = 0

        if budget <= 0:
            # Requirement 8.5: zero (or invalid/negative) budget -> no drafts.
            logger.info("Writer: remaining_budget=%s; producing no drafts.", budget)
            return result

        angle = getattr(plan, "email_angle", "") or ""

        for candidate in qualified or []:
            # Requirement 8.4: stop once cumulative drafts reach the budget.
            if len(result.drafts) >= budget:
                logger.info(
                    "Writer: remaining_budget %d reached; %d candidate(s) left undrafted.",
                    budget,
                    max(0, len(qualified) - len(result.drafts) - len(result.failures)),
                )
                break

            subject, body = self._draft_email(candidate, angle)
            if subject is None or body is None:
                # Requirement 8.6: gateway failed/timed out -> skip WITHOUT
                # consuming budget, retain completed drafts, record the failure.
                result.failures.append(
                    DraftFailure(
                        source_unique_id=getattr(candidate, "source_unique_id", "") or "",
                        name=getattr(candidate, "name", "") or "",
                        stage="gateway",
                        reason="TrueFoundry did not return a draft within 30s or returned an error",
                    )
                )
                continue

            draft = self._build_draft(candidate, subject, body, angle, run_id)
            persisted = self._persist(draft)
            if not persisted:
                # Requirement 8.7: draft produced but not stored after retries.
                draft.failure_reason = "draft not persisted to emails after retries"
                result.failures.append(
                    DraftFailure(
                        source_unique_id=getattr(candidate, "source_unique_id", "") or "",
                        name=getattr(candidate, "name", "") or "",
                        stage="persistence",
                        reason="emails insert failed after all retry attempts",
                    )
                )
            # The draft was produced by the gateway, so it consumes one unit of
            # budget and is retained regardless of persistence outcome.
            result.drafts.append(draft)

        logger.info(
            "Writer pass: %d draft(s) produced (budget %d), %d failure(s).",
            result.count,
            budget,
            result.failure_count,
        )
        return result

    # -- drafting (TrueFoundry gateway) --------------------------------------

    def _draft_email(
        self, candidate: Qualified, angle: str
    ) -> tuple[Optional[str], Optional[str]]:
        """Return ``(subject, body)`` for a candidate, or ``(None, None)`` on failure.

        Calls the TrueFoundry gateway (OpenAI SDK) with a 30-second timeout,
        asking for a JSON object with ``subject`` and ``body`` that weaves in the
        candidate's signals and the plan's email angle. Any timeout, transport
        error, missing-credentials condition, or an unusable/unparseable response
        returns ``(None, None)`` so the caller skips the candidate without
        consuming budget (Requirement 8.6).
        """
        gateway = self._get_openai_client()
        if gateway is None:
            logger.warning(
                "Writer: TrueFoundry not configured; cannot draft for %s.",
                getattr(candidate, "source_unique_id", "?"),
            )
            return None, None

        prompt = self._build_prompt(candidate, angle)
        try:
            response = gateway.chat.completions.create(
                model=self._model_id,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are an angel investor's outreach assistant. Write a "
                            "concise, personalized cold outreach email to a startup. "
                            "Reference the specific signals provided and adopt the "
                            "requested angle. Respond with ONLY a JSON object of the "
                            'form {"subject": "...", "body": "..."} and nothing else. '
                            "The subject must be a single short line; the body should "
                            "be 2-4 short paragraphs of plain text."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                timeout=self._draft_timeout,
                max_tokens=800,
            )
            text = (response.choices[0].message.content or "").strip()
        except Exception as exc:  # noqa: BLE001 - any gateway failure -> skip candidate
            logger.warning(
                "Writer: TrueFoundry draft failed for candidate %s: %s; skipping "
                "without consuming budget.",
                getattr(candidate, "source_unique_id", "?"),
                exc,
            )
            return None, None

        return self._parse_subject_body(text, candidate)

    def _parse_subject_body(
        self, text: str, candidate: Qualified
    ) -> tuple[Optional[str], Optional[str]]:
        """Parse the gateway response into ``(subject, body)``.

        Prefers a strict JSON object with ``subject``/``body`` keys. Tolerates a
        response wrapped in markdown code fences or with surrounding prose by
        extracting the first ``{...}`` block. Falls back to treating the whole
        response as the body (with a synthesized subject) when no JSON is found,
        provided the response is non-empty. Returns ``(None, None)`` only when the
        response is empty/unusable.
        """
        if not text:
            return None, None

        payload = self._extract_json_object(text)
        if payload is not None:
            subject = payload.get("subject")
            body = payload.get("body")
            subject = subject.strip() if isinstance(subject, str) else ""
            body = body.strip() if isinstance(body, str) else ""
            if body:
                if not subject:
                    subject = self._fallback_subject(candidate)
                return subject, body

        # No usable JSON: treat the whole text as the body so a real gateway
        # response is never wasted, synthesizing a subject from the candidate.
        return self._fallback_subject(candidate), text

    @staticmethod
    def _extract_json_object(text: str) -> Optional[dict]:
        """Best-effort extraction of the first JSON object from ``text``."""
        candidate_text = text.strip()
        # Strip a leading/trailing markdown code fence if present.
        if candidate_text.startswith("```"):
            fenced = candidate_text.strip("`")
            # Drop an optional leading "json" language tag.
            if fenced.lower().startswith("json"):
                fenced = fenced[4:]
            candidate_text = fenced.strip()
        try:
            obj = json.loads(candidate_text)
            return obj if isinstance(obj, dict) else None
        except (ValueError, TypeError):
            pass
        # Fall back to slicing the first balanced-looking {...} span.
        start = candidate_text.find("{")
        end = candidate_text.rfind("}")
        if 0 <= start < end:
            try:
                obj = json.loads(candidate_text[start : end + 1])
                return obj if isinstance(obj, dict) else None
            except (ValueError, TypeError):
                return None
        return None

    @staticmethod
    def _fallback_subject(candidate: Qualified) -> str:
        """Synthesize a subject line from the candidate name."""
        name = (getattr(candidate, "name", "") or "your team").strip()
        return f"Connecting about {name}"

    @staticmethod
    def _build_prompt(candidate: Qualified, angle: str) -> str:
        """Compose the user prompt describing the candidate, signals, and angle."""
        signals = getattr(candidate, "signals", None) or {}
        if isinstance(signals, dict) and signals:
            signal_str = ", ".join(f"{k}={v}" for k, v in signals.items())
        else:
            signal_str = "none"
        explanation = getattr(candidate, "fit_explanation", "") or ""
        angle_str = angle.strip() if angle else "a warm, founder-to-investor introduction"
        return (
            f"Company: {getattr(candidate, 'name', '')}\n"
            f"Source: {getattr(candidate, 'source', '')}\n"
            f"URL: {getattr(candidate, 'url', '')}\n"
            f"Signals: {signal_str}\n"
            f"Fit score: {getattr(candidate, 'fit_score', 0)}/100\n"
            f"Why they fit the thesis: {explanation}\n"
            f"Email angle to adopt: {angle_str}\n\n"
            "Write a personalized outreach email that explicitly references the "
            "signals above and adopts the requested email angle. Return only the "
            'JSON object {"subject": "...", "body": "..."}.'
        )

    def _get_openai_client(self) -> Optional[Any]:
        """Lazily build the OpenAI client pointed at the TrueFoundry gateway.

        Returns ``None`` when TrueFoundry is not configured or the OpenAI SDK is
        unavailable, so :meth:`_draft_email` skips the candidate. Mirrors the
        Qualifier's gateway client setup exactly.
        """
        if self._openai_client is not None:
            return self._openai_client
        if self._tf is None or not getattr(self._tf, "is_configured", False):
            logger.info("Writer: TrueFoundry not configured; no drafts can be produced.")
            return None
        try:
            from openai import OpenAI
        except ImportError:  # pragma: no cover - openai is a declared dependency
            logger.warning("Writer: openai SDK unavailable; no drafts can be produced.")
            return None
        self._openai_client = OpenAI(
            base_url=self._tf.base_url,
            api_key=self._tf.api_key,
        )
        return self._openai_client

    # -- draft construction --------------------------------------------------

    def _build_draft(
        self,
        candidate: Qualified,
        subject: str,
        body: str,
        angle: str,
        run_id: str,
    ) -> Draft:
        """Build a fresh, unsent, unapproved :class:`Draft` (Requirement 8.3)."""
        company_id = self._resolve_company_id(candidate)
        return Draft(
            email_id=uuid.uuid4().hex,
            company_id=company_id,
            subject=subject,
            body=body,
            angle=angle,
            run_id=run_id or "",
            approved=False,
            sent=False,
            failed=False,
            attempt_count=0,
            sender_backend=self._sender_backend,
            sent_at=None,
            failure_reason=None,
        )

    def _resolve_company_id(self, candidate: Qualified) -> str:
        """Resolve ``company_id`` from ``companies`` by the natural key.

        Queries the latest ``companies`` row for ``(source, source_unique_id)``.
        Falls back to the candidate's ``source_unique_id`` when no client is
        available, no row exists, or the read fails — so a draft is never dropped
        for lack of a resolved surrogate id (documented in the module docstring).
        """
        suid = getattr(candidate, "source_unique_id", "") or ""
        if self._client is None:
            return suid
        try:
            result = self._client.query(
                "SELECT company_id FROM companies "
                "WHERE source = {source:String} "
                "AND source_unique_id = {suid:String} "
                "ORDER BY version DESC LIMIT 1",
                parameters={
                    "source": getattr(candidate, "source", "") or "",
                    "suid": suid,
                },
            )
        except Exception as exc:  # noqa: BLE001 - read failure -> fall back to suid
            logger.debug(
                "Writer: companies lookup failed for %s: %s; using source_unique_id.",
                suid,
                exc,
            )
            return suid

        if getattr(result, "ok", False) and result.rows:
            company_id = result.rows[0][0]
            if company_id:
                return str(company_id)
        logger.debug(
            "Writer: no companies row for (%s, %s); using source_unique_id as company_id.",
            getattr(candidate, "source", "?"),
            suid,
        )
        return suid

    # -- persistence (emails insert, bounded retry) --------------------------

    def _persist(self, draft: Draft) -> bool:
        """Store ``draft`` in ``emails`` as unsent/unapproved with bounded retry.

        Inserts a fresh ``emails`` row (``approved=0``, ``sent=0``, ``failed=0``,
        ``version=1``) and retries up to ``max_persist_attempts`` (Requirement
        8.7). Returns ``True`` on success; on total failure it logs and returns
        ``False`` without raising so the draft is retained by the caller and the
        Tick continues.
        """
        if self._client is None:
            logger.debug("Writer: no ClickHouse client; skipping emails persistence.")
            return False

        row = self._build_email_row(draft)
        last_error: Optional[str] = None
        for attempt in range(1, self._max_persist_attempts + 1):
            try:
                result = self._client.insert("emails", [row], list(EMAILS_COLUMNS))
                if getattr(result, "ok", False):
                    return True
                last_error = getattr(result, "error", "insert returned not-ok")
            except Exception as exc:  # noqa: BLE001 - retain draft, keep retrying
                last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "Writer: emails insert attempt %d/%d failed for %s: %s",
                attempt,
                self._max_persist_attempts,
                draft.email_id,
                last_error,
            )

        logger.error(
            "Writer: emails insert failed after %d attempts for %s; draft retained "
            "in result (not persisted, per Req 8.7). Last error: %s",
            self._max_persist_attempts,
            draft.email_id,
            last_error,
        )
        return False

    def _build_email_row(self, draft: Draft) -> list[Any]:
        """Build the ``emails`` row aligned to :data:`EMAILS_COLUMNS`."""
        now = self._now()
        return [
            draft.email_id,
            draft.run_id,
            draft.company_id,
            draft.subject,
            draft.body,
            draft.angle,
            1 if draft.approved else 0,
            1 if draft.sent else 0,
            1 if draft.failed else 0,
            int(draft.attempt_count),
            draft.sender_backend,
            self._as_utc(draft.sent_at),
            draft.failure_reason,
            self._as_utc(now),
            self._as_utc(now),
            1,
        ]

    @staticmethod
    def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
        """Normalize a datetime to timezone-aware UTC for stable ClickHouse writes.

        The ``clickhouse-connect`` driver shifts *naive* datetimes from local time
        to UTC on insert; writing tz-aware UTC values stores the exact instant so
        a read-back/rewrite stays identical. Mirrors the Scanner/Qualifier
        ``_as_utc`` pattern. Naive inputs are assumed already UTC.
        """
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
