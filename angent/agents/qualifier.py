"""The Qualifier: fit scoring, thesis-grounded explanation, and persistence.

The Qualifier turns raw :class:`~angent.models.Candidate` rows discovered by the
Scanner into scored :class:`~angent.models.Qualified` rows the Writer can act on
(Requirement 5). For each candidate it:

1. Asks the active, pluggable :class:`~angent.scoring.scorer.Scorer` for a numeric
   fit score against the thesis and clamps it to ``[0, 100]`` (Requirements 5.1,
   7.1). The Qualifier never branches on the concrete scorer type.
2. Generates a 50–1000 character natural-language explanation that references the
   thesis, via the **TrueFoundry_Gateway** — the OpenAI SDK pointed at
   ``TRUEFOUNDRY_BASE_URL`` with the Bedrock-backed Claude model id — within a
   30-second budget (Requirements 5.2, 5.3, 18.3). If the gateway times out or
   errors at 30s, the score is kept and an *"explanation unavailable"* placeholder
   is stored in place of the prose, and the record is retained (Requirement 5.4).
3. Persists the ``fit_score`` + ``fit_explanation`` back to the candidate's
   ``companies`` row with a latest-version-wins **upsert** (read the existing row
   to preserve ``company_id``/``created_at``, bump ``version``/``updated_at``)
   using bounded retry (up to 3 attempts). The computed score/explanation are
   **never discarded** even when persistence ultimately fails — the returned
   :class:`Qualified` always carries them (Requirements 5.5, 12.6, 22).

This module implements the scoring + explanation + persistence half of the
Qualifier. The per-candidate Pioneer fallback and the threshold-based forwarding
of qualified candidates to the Writer are layered on separately; ``threshold`` is
accepted here for interface stability but every scored candidate is returned.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from angent.models import Candidate, Qualified
from angent.scoring.scorer import Scorer

logger = logging.getLogger("angent.agents.qualifier")


# --- Defaults ---------------------------------------------------------------

# The Bedrock-backed Claude model id routed through the TrueFoundry gateway. This
# matches the model the front end already drives (genui-chat-app/.../route.ts).
# Overridable via TRUEFOUNDRY_MODEL or OPENAI_MODEL so the gateway can be
# re-pointed without code changes.
DEFAULT_TRUEFOUNDRY_MODEL = "bi-beta-bedrock/global.anthropic.claude-sonnet-4-6"

# Hard explanation budget (Requirement 5.2): the whole score+explanation step for
# a candidate completes within 30 seconds; on timeout we store the placeholder.
DEFAULT_EXPLANATION_TIMEOUT = 30.0

# Explanation length bounds in characters (Requirement 5.3).
MIN_EXPLANATION_CHARS = 50
MAX_EXPLANATION_CHARS = 1000

# Bounded persistence retry attempts (Requirement 12.6 / 5.5).
DEFAULT_MAX_PERSIST_ATTEMPTS = 3

# Marker phrase stored when the explanation cannot be produced in time.
EXPLANATION_UNAVAILABLE_MARKER = "explanation unavailable"

# Column order for the ``companies`` table — mirrors COMPANIES_DDL in
# angent/persistence/clickhouse.py exactly so inserts line up with the schema.
COMPANIES_COLUMNS: tuple[str, ...] = (
    "company_id",
    "source",
    "source_unique_id",
    "name",
    "url",
    "signals",
    "first_activity",
    "fit_score",
    "fit_explanation",
    "created_at",
    "updated_at",
    "version",
)


def _clamp_score(value: Any) -> int:
    """Coerce a raw scorer output to an int clamped into the inclusive [0,100]."""
    try:
        numeric = int(round(float(value)))
    except (TypeError, ValueError):
        numeric = 0
    return max(0, min(100, numeric))


class Qualifier:
    """Scores candidates, explains the fit via TrueFoundry, and persists results.

    Args:
        client: A :class:`~angent.persistence.clickhouse.ClickHouseClient` used
            to upsert ``fit_score``/``fit_explanation`` into ``companies``. When
            ``None``, scoring and explanation still run and are returned, but the
            persistence step is skipped (useful for offline scoring/tests).
        config: An :class:`~angent.config.Config`; when ``None`` it is loaded from
            the environment. Supplies the TrueFoundry credentials/base URL.
        model_id: TrueFoundry model id for explanation generation. Defaults to the
            ``TRUEFOUNDRY_MODEL``/``OPENAI_MODEL`` env var or the Bedrock Claude id.
        explanation_timeout: Per-candidate explanation budget in seconds (30s).
        max_persist_attempts: Bounded retry count for the ``companies`` upsert.
        openai_client: Optional pre-built OpenAI-compatible client (eases testing
            and lets callers inject a configured gateway client).
        now: Injectable clock for deterministic ``created_at``/``updated_at``.
    """

    def __init__(
        self,
        client: Optional[Any] = None,
        *,
        config: Optional[Any] = None,
        model_id: Optional[str] = None,
        explanation_timeout: float = DEFAULT_EXPLANATION_TIMEOUT,
        max_persist_attempts: int = DEFAULT_MAX_PERSIST_ATTEMPTS,
        openai_client: Optional[Any] = None,
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
        self._explanation_timeout = explanation_timeout
        self._max_persist_attempts = max(1, int(max_persist_attempts))
        self._now: Callable[[], datetime] = now or (lambda: datetime.now(timezone.utc))
        # Lazily constructed OpenAI client pointed at the TrueFoundry gateway.
        self._openai_client = openai_client

    # -- public interface ----------------------------------------------------

    def qualify(
        self,
        candidates: list[Candidate],
        thesis: str,
        scorer: Scorer,
        threshold: int = 0,
    ) -> list[Qualified]:
        """Score, explain, and persist each candidate; return ``Qualified`` rows.

        Every candidate is scored via ``scorer`` (clamped to ``[0,100]``), given a
        thesis-referencing explanation via TrueFoundry (or the placeholder on
        timeout/error), persisted to ``companies`` with bounded retry, and
        returned as a :class:`~angent.models.Qualified`. The computed score and
        explanation are retained on the returned object even if persistence fails.

        ``threshold`` is accepted for interface stability; threshold-based
        forwarding to the Writer is handled downstream, so all scored candidates
        are returned here.
        """
        qualified: list[Qualified] = []
        for candidate in candidates or []:
            score = _clamp_score(scorer.score(candidate, thesis))
            explanation = self._explain(candidate, thesis, score)
            self._persist(candidate, score, explanation)
            qualified.append(self._to_qualified(candidate, score, explanation))
        return qualified

    # -- explanation (TrueFoundry gateway) -----------------------------------

    def _explain(self, candidate: Candidate, thesis: str, score: int) -> str:
        """Return a 50–1000 char thesis-referencing explanation, or a placeholder.

        Calls the TrueFoundry gateway (OpenAI SDK) with a 30-second timeout. Any
        timeout, transport error, missing-credentials condition, or an unusable
        (too-short) response degrades to the ``explanation unavailable``
        placeholder so the candidate's score is still kept (Requirement 5.4).
        """
        gateway = self._get_openai_client()
        if gateway is None:
            return self._placeholder(thesis, score)

        prompt = self._build_prompt(candidate, thesis, score)
        try:
            response = gateway.chat.completions.create(
                model=self._model_id,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are an angel investor's analyst. Explain, in 2-4 "
                            "sentences, how well a startup fits a stated investment "
                            "thesis. Always reference the thesis explicitly. Keep the "
                            "explanation between 50 and 1000 characters."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                timeout=self._explanation_timeout,
                max_tokens=400,
            )
            text = (response.choices[0].message.content or "").strip()
        except Exception as exc:  # noqa: BLE001 - any gateway failure -> placeholder
            logger.warning(
                "TrueFoundry explanation failed for candidate %s: %s; "
                "storing placeholder",
                getattr(candidate, "source_unique_id", "?"),
                exc,
            )
            return self._placeholder(thesis, score)

        return self._bound_explanation(text, thesis, score)

    def _bound_explanation(self, text: str, thesis: str, score: int) -> str:
        """Clamp explanation length to [50,1000] chars; placeholder if too short.

        Over-long prose is truncated on a word boundary at <=1000 chars; prose
        shorter than the 50-char minimum is treated as unusable and replaced with
        the placeholder so the stored explanation always satisfies the bound or is
        an explicit placeholder.
        """
        if not text or len(text) < MIN_EXPLANATION_CHARS:
            return self._placeholder(thesis, score)
        if len(text) > MAX_EXPLANATION_CHARS:
            clipped = text[:MAX_EXPLANATION_CHARS]
            # Prefer cutting at the last whitespace to avoid splitting a word.
            cut = clipped.rfind(" ")
            if cut >= MIN_EXPLANATION_CHARS:
                clipped = clipped[:cut]
            text = clipped.rstrip()
        return text

    def _placeholder(self, thesis: str, score: int) -> str:
        """Build the 'explanation unavailable' placeholder (still references thesis).

        The placeholder embeds the required marker phrase while staying within the
        50–1000 char window and naming the thesis, so downstream consumers can both
        detect the unavailable state and render a meaningful line.
        """
        snippet = (thesis or "").strip()
        if len(snippet) > 160:
            snippet = snippet[:160].rstrip() + "…"
        text = (
            f"Explanation {EXPLANATION_UNAVAILABLE_MARKER}: the qualifier scored this "
            f"candidate {score}/100 against the thesis"
        )
        if snippet:
            text += f' "{snippet}"'
        text += ", but the explanation service did not respond in time."
        # Guarantee the lower bound even for an empty thesis.
        if len(text) < MIN_EXPLANATION_CHARS:
            text = text.ljust(MIN_EXPLANATION_CHARS, ".")
        return text[:MAX_EXPLANATION_CHARS]

    @staticmethod
    def _build_prompt(candidate: Candidate, thesis: str, score: int) -> str:
        """Compose the user prompt describing the thesis, candidate, and score."""
        signals = candidate.signals or {}
        if isinstance(signals, dict) and signals:
            signal_str = ", ".join(f"{k}={v}" for k, v in signals.items())
        else:
            signal_str = "none"
        return (
            f"Investment thesis: {thesis}\n"
            f"Candidate company: {candidate.name}\n"
            f"Source: {candidate.source}\n"
            f"URL: {candidate.url}\n"
            f"Signals: {signal_str}\n"
            f"Computed fit score: {score}/100\n\n"
            "Explain in 50-1000 characters how this candidate fits (or does not "
            "fit) the thesis above. Reference the thesis explicitly."
        )

    def _get_openai_client(self) -> Optional[Any]:
        """Lazily build the OpenAI client pointed at the TrueFoundry gateway.

        Returns ``None`` when TrueFoundry is not configured or the OpenAI SDK is
        unavailable, so :meth:`_explain` can fall back to the placeholder.
        """
        if self._openai_client is not None:
            return self._openai_client
        if self._tf is None or not getattr(self._tf, "is_configured", False):
            logger.info("TrueFoundry not configured; explanations use placeholder.")
            return None
        try:
            from openai import OpenAI
        except ImportError:  # pragma: no cover - openai is a declared dependency
            logger.warning("openai SDK unavailable; explanations use placeholder.")
            return None
        self._openai_client = OpenAI(
            base_url=self._tf.base_url,
            api_key=self._tf.api_key,
        )
        return self._openai_client

    # -- persistence (companies upsert, bounded retry) -----------------------

    def _persist(self, candidate: Candidate, score: int, explanation: str) -> bool:
        """Upsert ``fit_score``/``fit_explanation`` into ``companies`` (bounded retry).

        Latest-version-wins upsert: read the candidate's existing row (by
        ``source`` + ``source_unique_id``) to preserve ``company_id`` and
        ``created_at``, then insert a new row with bumped ``version`` and
        ``updated_at``. Retries up to ``max_persist_attempts``. Returns ``True`` on
        success; on total failure it logs and returns ``False`` without raising —
        the computed score/explanation are still returned to the caller and never
        discarded (Requirement 5.5).
        """
        if self._client is None:
            logger.debug("No ClickHouse client; skipping companies persistence.")
            return False

        last_error: Optional[str] = None
        for attempt in range(1, self._max_persist_attempts + 1):
            try:
                row = self._build_company_row(candidate, score, explanation)
                result = self._client.insert(
                    "companies", [row], list(COMPANIES_COLUMNS)
                )
                if getattr(result, "ok", False):
                    return True
                last_error = getattr(result, "error", "insert returned not-ok")
            except Exception as exc:  # noqa: BLE001 - retain values, keep retrying
                last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "companies upsert attempt %d/%d failed for %s: %s",
                attempt,
                self._max_persist_attempts,
                getattr(candidate, "source_unique_id", "?"),
                last_error,
            )

        logger.error(
            "companies upsert failed after %d attempts for %s; "
            "score=%d retained in returned Qualified (not discarded). Last error: %s",
            self._max_persist_attempts,
            getattr(candidate, "source_unique_id", "?"),
            score,
            last_error,
        )
        return False

    def _build_company_row(
        self, candidate: Candidate, score: int, explanation: str
    ) -> list[Any]:
        """Build the ``companies`` row for an upsert, preserving identity fields.

        Reads the latest existing row for ``(source, source_unique_id)`` to reuse
        its ``company_id`` and ``created_at`` and to compute ``version + 1``. When
        no row exists (or the read fails), a fresh ``company_id`` and
        ``created_at`` are minted at ``version = 1`` so the candidate is inserted.
        """
        now = self._now()
        existing = self._read_existing(candidate)
        if existing is not None:
            company_id, created_at, version = existing
            new_version = version + 1
        else:
            company_id = uuid.uuid4().hex
            created_at = now
            new_version = 1

        signals = candidate.signals or {}
        try:
            signals_json = json.dumps(signals, default=str)
        except (TypeError, ValueError):
            signals_json = "{}"

        first_activity = candidate.first_activity or now

        return [
            company_id,
            candidate.source,
            candidate.source_unique_id,
            candidate.name,
            candidate.url,
            signals_json,
            self._as_utc(first_activity),
            int(score),
            explanation,
            self._as_utc(created_at),
            self._as_utc(now),
            new_version,
        ]

    def _read_existing(
        self, candidate: Candidate
    ) -> Optional[tuple[str, datetime, int]]:
        """Return ``(company_id, created_at, version)`` of the latest existing row.

        Returns ``None`` when the candidate is new or the read fails — the caller
        then treats this as a fresh insert. A read failure must not abort the
        write, so we degrade to ``None`` rather than raising.
        """
        try:
            result = self._client.query(
                "SELECT company_id, created_at, version FROM companies "
                "WHERE source = {source:String} "
                "AND source_unique_id = {suid:String} "
                "ORDER BY version DESC LIMIT 1",
                parameters={
                    "source": candidate.source,
                    "suid": candidate.source_unique_id,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("companies read failed (treating as new): %s", exc)
            return None

        if not getattr(result, "ok", False) or not result.rows:
            return None
        company_id, created_at, version = result.rows[0]
        try:
            version_int = int(version)
        except (TypeError, ValueError):
            version_int = 0
        return str(company_id), created_at, version_int

    @staticmethod
    def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
        """Normalize a datetime to timezone-aware UTC for stable ClickHouse writes.

        The ``clickhouse-connect`` driver converts *naive* datetimes from local
        time to UTC on insert but returns DateTime columns as naive-UTC on read;
        re-inserting such a read-back value would shift it again. Writing
        tz-aware UTC values stores the exact instant, so an upserted
        ``created_at`` read back and rewritten stays identical. Naive inputs are
        assumed to already be UTC (matching the Scanner's tz-aware UTC
        ``first_activity``).
        """
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    # -- mapping -------------------------------------------------------------

    @staticmethod
    def _to_qualified(
        candidate: Candidate, score: int, explanation: str
    ) -> Qualified:
        """Build a :class:`Qualified` carrying the candidate fields + score/prose."""
        return Qualified(
            source=candidate.source,
            source_unique_id=candidate.source_unique_id,
            name=candidate.name,
            url=candidate.url,
            signals=candidate.signals,
            first_activity=candidate.first_activity,
            fit_score=score,
            fit_explanation=explanation,
        )
