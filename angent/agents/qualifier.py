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

This module implements the full Qualifier:

* **Scoring + explanation + persistence** (Requirement 5.1–5.5): every candidate
  is scored, explained, and upserted into ``companies``.
* **Per-candidate Pioneer fallback** (Requirements 5.6, 5.7, 7.3, 18.5): the
  active :class:`~angent.scoring.scorer.Scorer` (typically the
  :class:`~angent.scoring.pioneer.PioneerScorer`) is tried first within its 10s
  per-candidate timeout; if it raises (timeout, transport error, or any failure)
  the Qualifier falls back to a :class:`~angent.scoring.scorer.HeuristicScorer`
  **for that candidate only**, records that the fallback occurred, and continues
  the remaining candidates without aborting the Tick.
* **Threshold-based forwarding** (Requirements 5.8, 5.9): candidates whose
  ``fit_score`` meets or exceeds the configured integer qualification threshold
  (``[0,100]``) are forwarded to the Writer; those below are withheld. *Every*
  candidate is still scored and persisted regardless of the threshold — only the
  forwarding set is filtered.

:meth:`Qualifier.qualify` returns a :class:`QualifyResult` so the orchestrator can
distinguish ``all`` (every scored candidate) from ``qualified``/``forwarded``
(those at or above the threshold), and inspect which candidates fell back to the
heuristic via ``fallbacks``.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from angent.models import Candidate, Qualified
from angent.scoring.scorer import HeuristicScorer, Scorer

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


def _validate_threshold(threshold: Any) -> int:
    """Validate the qualification threshold is an integer in the inclusive [0,100].

    The threshold gates which candidates are forwarded to the Writer
    (Requirement 5.8). It must be a genuine integer in ``[0,100]``; a bool, a
    non-integral float, or an out-of-range value is rejected with ``ValueError``
    so a misconfigured threshold fails fast rather than silently forwarding the
    wrong set.
    """
    if isinstance(threshold, bool):
        raise ValueError("qualification threshold must be an int, not a bool")
    if isinstance(threshold, float):
        if not threshold.is_integer():
            raise ValueError(
                f"qualification threshold must be an integer, got {threshold!r}"
            )
        threshold = int(threshold)
    if not isinstance(threshold, int):
        raise ValueError(
            f"qualification threshold must be an int in [0,100], got {type(threshold).__name__}"
        )
    if not 0 <= threshold <= 100:
        raise ValueError(
            f"qualification threshold must be in [0,100], got {threshold}"
        )
    return threshold


@dataclass
class QualifyResult:
    """The outcome of a Qualifier pass over a batch of candidates.

    Lets the orchestrator distinguish every scored candidate from the subset
    forwarded to the Writer, and inspect per-candidate Pioneer fallbacks.

    Attributes:
        all: Every candidate that was scored, explained, and persisted, in input
            order — regardless of whether it met the threshold (Requirement 5.4
            keeps every score; below-threshold rows are still persisted, just not
            forwarded).
        qualified: The subset of ``all`` whose ``fit_score`` is >= ``threshold``;
            these are the candidates forwarded to the Writer (Requirements 5.8,
            5.9). ``forwarded`` is a readable alias.
        fallbacks: ``source_unique_id`` values of candidates whose active-scorer
            call failed and fell back to the heuristic for that candidate only
            (Requirements 5.7, 7.3, 18.5).
        threshold: The validated integer threshold applied to this pass.
    """

    all: list[Qualified] = field(default_factory=list)
    qualified: list[Qualified] = field(default_factory=list)
    fallbacks: list[str] = field(default_factory=list)
    threshold: int = 0

    @property
    def forwarded(self) -> list[Qualified]:
        """Readable alias for :attr:`qualified` — the rows passed to the Writer."""
        return self.qualified

    @property
    def fallback_count(self) -> int:
        """How many candidates fell back to the heuristic scorer this pass."""
        return len(self.fallbacks)


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
        fallback_scorer: The scorer used for a single candidate when the active
            scorer raises (Pioneer timeout/error). Defaults to a fresh
            :class:`~angent.scoring.scorer.HeuristicScorer`, which is always
            available and never performs I/O (Requirements 5.7, 7.3).
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
        fallback_scorer: Optional[Scorer] = None,
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
        # Always-available per-candidate fallback (Requirements 5.7, 7.3).
        self._fallback_scorer: Scorer = fallback_scorer or HeuristicScorer()
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
    ) -> QualifyResult:
        """Score, explain, persist each candidate; forward those at/above threshold.

        For each candidate this:

        1. Obtains a fit score from the active ``scorer`` (clamped to ``[0,100]``).
           If the active scorer raises — a Pioneer timeout/error or any failure —
           it falls back to the heuristic scorer **for that candidate only**,
           records the fallback, and continues the remaining candidates without
           aborting the Tick (Requirements 5.6, 5.7, 7.3, 18.5).
        2. Generates a thesis-referencing explanation via TrueFoundry (or the
           placeholder on timeout/error) and persists score + explanation to
           ``companies`` with bounded retry — **every** candidate is persisted,
           regardless of the threshold (Requirements 5.2–5.5).
        3. Collects the candidate into ``all``; if its score meets or exceeds
           ``threshold`` it is also added to ``qualified`` (forwarded to the
           Writer), otherwise it is withheld (Requirements 5.8, 5.9).

        Args:
            candidates: Candidates discovered by the Scanner this Tick.
            thesis: The investor's thesis the candidates are scored against.
            scorer: The active scorer (e.g. ``PioneerScorer``). Failures fall back
                per-candidate to ``self._fallback_scorer``.
            threshold: Integer qualification threshold in ``[0,100]``; candidates
                with ``fit_score >= threshold`` are forwarded.

        Returns:
            A :class:`QualifyResult` with ``all`` (every scored candidate),
            ``qualified``/``forwarded`` (score >= threshold), and ``fallbacks``.

        Raises:
            ValueError: if ``threshold`` is not an integer in ``[0,100]``.
        """
        threshold = _validate_threshold(threshold)
        result = QualifyResult(threshold=threshold)

        for candidate in candidates or []:
            score, used_fallback = self._score_candidate(candidate, thesis, scorer)
            if used_fallback:
                result.fallbacks.append(
                    getattr(candidate, "source_unique_id", "") or ""
                )
            explanation = self._explain(candidate, thesis, score)
            self._persist(candidate, score, explanation)

            qualified = self._to_qualified(candidate, score, explanation)
            result.all.append(qualified)
            if score >= threshold:
                result.qualified.append(qualified)
            else:
                logger.info(
                    "Candidate %s scored %d < threshold %d; withheld from Writer.",
                    getattr(candidate, "source_unique_id", "?"),
                    score,
                    threshold,
                )

        logger.info(
            "Qualifier pass: %d scored, %d forwarded (threshold %d), %d fallback(s).",
            len(result.all),
            len(result.qualified),
            threshold,
            result.fallback_count,
        )
        return result

    # -- per-candidate scoring with fallback ---------------------------------

    def _score_candidate(
        self, candidate: Candidate, thesis: str, scorer: Scorer
    ) -> tuple[int, bool]:
        """Score one candidate via the active scorer, falling back on any failure.

        Tries ``scorer.score`` first (the active scorer; for Pioneer this carries
        its own 10s per-candidate timeout and raises on timeout/error). On *any*
        exception it falls back to the heuristic scorer for this candidate only,
        logs the fallback, and the Tick continues (Requirements 5.7, 7.3, 18.5).
        If the fallback scorer also fails, the candidate is scored 0 so the Tick
        still proceeds.

        Returns:
            ``(score, used_fallback)`` where ``score`` is clamped to ``[0,100]``
            and ``used_fallback`` is True when the heuristic fallback was used.
        """
        try:
            return _clamp_score(scorer.score(candidate, thesis)), False
        except Exception as exc:  # noqa: BLE001 - any active-scorer failure -> fallback
            logger.warning(
                "Active scorer failed for candidate %s: %s; falling back to "
                "HeuristicScorer for this candidate only.",
                getattr(candidate, "source_unique_id", "?"),
                exc,
            )

        try:
            return _clamp_score(self._fallback_scorer.score(candidate, thesis)), True
        except Exception as exc:  # noqa: BLE001 - fallback failed too; keep Tick alive
            logger.error(
                "Fallback scorer also failed for candidate %s: %s; scoring 0 so "
                "the Tick continues.",
                getattr(candidate, "source_unique_id", "?"),
                exc,
            )
            return 0, True

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
