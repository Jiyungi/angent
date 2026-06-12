"""The optional Pioneer (Fastino) adaptive-inference scorer and scorer selection.

Pioneer is Fastino's adaptive-inference platform: a hosted model that scores
candidates and continuously improves on the outcomes it is fed. Angent treats it
as an *optional* drop-in behind the exact same :class:`~angent.scoring.scorer.Scorer`
interface the built-in :class:`~angent.scoring.scorer.HeuristicScorer` satisfies
(Requirement 7.1), so the Qualifier and Optimizer never branch on the concrete
scorer type.

This module provides:

* :class:`PioneerScorer` — an HTTP client over the Fastino ``/run`` inference
  endpoint that implements ``score(candidate, thesis) -> int`` (clamped to
  ``[0,100]``) and ``learn(outcomes) -> LearnResult``. Each call carries a short
  per-call timeout (10s for scoring, per the design's per-candidate Pioneer
  timeout). **Scoring failures (timeout, transport error, or an unparseable
  response) are raised as :class:`PioneerScorerError`** so the Qualifier's
  per-candidate fallback can catch them and fall back to the heuristic for that
  candidate only, without aborting the Tick (Requirements 5.7, 7.3).
* :func:`select_scorer` — returns a :class:`PioneerScorer` when Pioneer
  credentials are present *and* the endpoint is reachable within a short health
  check, otherwise the :class:`HeuristicScorer`, logging that it is operating in
  heuristic mode when Pioneer is absent (Requirement 7.2).

Endpoint shape (Fastino Pioneer): ``POST {base_url}/run`` authenticated with an
``x-api-key`` header, JSON body ``{"model_id", "input": [{"text", "parameters"}]}``.
The base URL and model id default to the public Fastino values and may be
overridden via the ``PIONEER_BASE_URL`` / ``PIONEER_MODEL_ID`` environment
variables so the integration can be re-pointed without code changes.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

import requests

from angent.models import Candidate, Outcome
from angent.scoring.scorer import HeuristicScorer, LearnResult, Scorer

logger = logging.getLogger("angent.scoring.pioneer")


# --- Defaults ---------------------------------------------------------------

# Fastino's public inference endpoint and the header it authenticates with.
DEFAULT_PIONEER_BASE_URL = "https://api.fastino.com"
# Model id used for fit scoring; overridable via PIONEER_MODEL_ID.
DEFAULT_PIONEER_MODEL_ID = "pioneer"
# Per-candidate scoring timeout (seconds) — the design's Pioneer timeout.
DEFAULT_SCORE_TIMEOUT = 10.0
# Timeout for the model-update (learn) call.
DEFAULT_LEARN_TIMEOUT = 15.0
# Short timeout for the startup reachability/health check.
DEFAULT_REACHABILITY_TIMEOUT = 3.0


class PioneerScorerError(RuntimeError):
    """Raised when a Pioneer request fails or returns an unusable response.

    Propagated by :meth:`PioneerScorer.score` and :meth:`PioneerScorer.learn`
    so the Qualifier (per-candidate, Requirement 7.3) and the Optimizer
    (model update, Requirement 6.5) can apply their respective fallbacks.
    """


def _clamp_int(value: float) -> int:
    """Round and clamp a raw model score into the inclusive integer range [0,100]."""
    return max(0, min(100, int(round(value))))


class PioneerScorer:
    """Fastino Pioneer adaptive-inference scorer behind the ``Scorer`` interface.

    Drop-in compatible with :class:`HeuristicScorer`: identical method
    signatures and return structures (Requirement 7.1). All network access is
    bounded by a per-call timeout; any failure raises
    :class:`PioneerScorerError` so callers can fall back deterministically
    rather than hang or crash the Tick.

    Args:
        api_key: The Pioneer API key (``pio_sk_...``), sent as ``x-api-key``.
        base_url: Inference base URL. Defaults to ``PIONEER_BASE_URL`` env or the
            public Fastino endpoint.
        model_id: Model id to score against. Defaults to ``PIONEER_MODEL_ID`` env
            or ``"pioneer"``.
        score_timeout: Per-candidate scoring timeout in seconds (default 10s).
        learn_timeout: Timeout for the model-update call in seconds.
        session: Optional injected ``requests.Session`` (eases testing).
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: Optional[str] = None,
        model_id: Optional[str] = None,
        score_timeout: float = DEFAULT_SCORE_TIMEOUT,
        learn_timeout: float = DEFAULT_LEARN_TIMEOUT,
        session: Optional[requests.Session] = None,
    ) -> None:
        if not api_key:
            raise ValueError("PioneerScorer requires a non-empty api_key")
        self.api_key = api_key
        self.base_url = (
            base_url or os.environ.get("PIONEER_BASE_URL") or DEFAULT_PIONEER_BASE_URL
        ).rstrip("/")
        self.model_id = (
            model_id or os.environ.get("PIONEER_MODEL_ID") or DEFAULT_PIONEER_MODEL_ID
        )
        self.score_timeout = score_timeout
        self.learn_timeout = learn_timeout
        self._session = session or requests.Session()

    # -- public Scorer interface --------------------------------------------

    def score(self, candidate: Candidate, thesis: str) -> int:
        """Score a candidate's fit against the thesis via Pioneer, clamped to [0,100].

        Raises:
            PioneerScorerError: on timeout, transport error, non-2xx response, or
                a response from which no numeric score can be recovered. The
                Qualifier catches this and falls back to the heuristic for this
                candidate only (Requirement 7.3).
        """
        payload = {
            "model_id": self.model_id,
            "input": [
                {
                    "text": self._build_score_text(candidate, thesis),
                    "parameters": {
                        "task": "fit_score",
                        "scale": [0, 100],
                        "source": candidate.source,
                    },
                }
            ],
        }
        data = self._post("/run", payload, self.score_timeout, what="score")
        raw = self._extract_score(data)
        if raw is None:
            raise PioneerScorerError(
                "Pioneer response contained no recoverable numeric fit score"
            )
        return _clamp_int(raw)

    def learn(self, outcomes: list[Outcome]) -> LearnResult:
        """Push reply/open outcomes to Pioneer as adaptive-inference feedback.

        Pioneer continuously retrains on the outcomes it is fed; this sends the
        newly observed outcomes as the learning signal. Returns a
        :class:`LearnResult` summarizing what was submitted.

        Raises:
            PioneerScorerError: on timeout, transport error, or non-2xx response.
                The Optimizer catches this, keeps the previous model, continues
                scoring with it, and records the failed update (Requirement 6.5).
        """
        outcomes = outcomes or []
        num_outcomes = len(outcomes)
        num_replies = sum(1 for o in outcomes if getattr(o, "kind", None) == "reply")

        if num_outcomes == 0:
            return LearnResult(
                num_outcomes=0,
                num_replies=0,
                adjusted=False,
                note="no outcomes to submit to Pioneer",
            )

        payload = {
            "model_id": self.model_id,
            "feedback": [
                {
                    "company_id": o.company_id,
                    "email_id": o.email_id,
                    "kind": o.kind,
                    "label": 1 if o.kind == "reply" else 0,
                    "occurred_at": o.occurred_at.isoformat()
                    if getattr(o, "occurred_at", None) is not None
                    else None,
                }
                for o in outcomes
            ],
        }
        # The model-update path lives under the same inference host; "/learn" is
        # the adaptive-inference feedback channel. Failures propagate.
        self._post("/learn", payload, self.learn_timeout, what="learn")
        return LearnResult(
            num_outcomes=num_outcomes,
            num_replies=num_replies,
            adjusted=True,
            note=f"submitted {num_outcomes} outcome(s) to Pioneer for adaptive inference",
        )

    # -- reachability --------------------------------------------------------

    def is_reachable(self, timeout: float = DEFAULT_REACHABILITY_TIMEOUT) -> bool:
        """Best-effort health check: True if the Pioneer host answers in time.

        Any HTTP response (even 401/404) means the endpoint is reachable; only a
        transport-level failure (DNS, connection refused, timeout) counts as
        unreachable. Used by :func:`select_scorer` at startup.
        """
        try:
            resp = self._session.request(
                "HEAD",
                self.base_url + "/run",
                headers=self._headers(),
                timeout=timeout,
            )
            return resp.status_code < 500 or resp.status_code in (500, 501, 502, 503)
        except requests.RequestException as exc:
            logger.info("Pioneer reachability check failed: %s", exc)
            return False

    # -- internals -----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key, "Content-Type": "application/json"}

    def _post(
        self, path: str, payload: dict[str, Any], timeout: float, *, what: str
    ) -> Any:
        """POST JSON to the Pioneer endpoint, raising PioneerScorerError on any failure."""
        url = self.base_url + path
        try:
            resp = self._session.post(
                url, json=payload, headers=self._headers(), timeout=timeout
            )
        except requests.Timeout as exc:
            raise PioneerScorerError(
                f"Pioneer {what} request timed out after {timeout}s"
            ) from exc
        except requests.RequestException as exc:
            raise PioneerScorerError(f"Pioneer {what} request failed: {exc}") from exc

        if resp.status_code >= 400:
            raise PioneerScorerError(
                f"Pioneer {what} returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise PioneerScorerError(
                f"Pioneer {what} returned non-JSON response"
            ) from exc

    @staticmethod
    def _build_score_text(candidate: Candidate, thesis: str) -> str:
        """Compose the inference prompt describing the thesis and candidate."""
        signals = candidate.signals or {}
        signal_str = ", ".join(f"{k}={v}" for k, v in signals.items()) if signals else "none"
        return (
            f"Investment thesis: {thesis}\n"
            f"Candidate: {candidate.name}\n"
            f"Source: {candidate.source}\n"
            f"URL: {candidate.url}\n"
            f"Signals: {signal_str}\n"
            f"Rate this candidate's fit against the thesis from 0 to 100."
        )

    @staticmethod
    def _extract_score(data: Any) -> Optional[float]:
        """Recover a numeric fit score from a variety of plausible response shapes.

        Pioneer's exact response schema can vary, so we look for a numeric value
        under common keys (``score``, ``fit_score``, ``output``, ``result``,
        ``value``), then fall back to the first number found in any string field.
        Returns ``None`` when nothing numeric can be recovered.
        """
        score_keys = ("score", "fit_score", "value", "rating", "prediction")

        def search(obj: Any) -> Optional[float]:
            if isinstance(obj, (int, float)) and not isinstance(obj, bool):
                return float(obj)
            if isinstance(obj, str):
                match = re.search(r"-?\d+(?:\.\d+)?", obj)
                return float(match.group()) if match else None
            if isinstance(obj, dict):
                # Prefer explicit score-bearing keys.
                for key in score_keys:
                    if key in obj:
                        found = search(obj[key])
                        if found is not None:
                            return found
                # Then recurse into common container keys, then any value.
                for key in ("output", "result", "results", "outputs", "data", "input"):
                    if key in obj:
                        found = search(obj[key])
                        if found is not None:
                            return found
                for value in obj.values():
                    found = search(value)
                    if found is not None:
                        return found
                return None
            if isinstance(obj, (list, tuple)):
                for item in obj:
                    found = search(item)
                    if found is not None:
                        return found
                return None
            return None

        return search(data)


def select_scorer(
    config: Any = None,
    *,
    reachability_timeout: float = DEFAULT_REACHABILITY_TIMEOUT,
    check_reachability: bool = True,
) -> Scorer:
    """Return the active scorer: Pioneer when usable, else the heuristic default.

    Pioneer is selected only when its credentials are present *and* the endpoint
    is reachable within ``reachability_timeout``; otherwise Angent runs on the
    always-available :class:`HeuristicScorer` and logs that it is operating in
    heuristic mode (Requirement 7.2).

    Args:
        config: An :class:`~angent.config.Config` (or any object exposing a
            ``pioneer.api_key`` attribute). When ``None``, configuration is
            loaded from the environment via :func:`angent.config.load_config`.
        reachability_timeout: Seconds to wait on the startup health check.
        check_reachability: When False, skip the network health check and select
            Pioneer on credential presence alone (useful for tests/offline runs).

    Returns:
        A :class:`Scorer` — either a :class:`PioneerScorer` or a
        :class:`HeuristicScorer`.
    """
    if config is None:
        from angent.config import load_config

        config = load_config()

    api_key = _resolve_api_key(config)

    if not api_key:
        logger.info(
            "Pioneer credentials absent — operating in HeuristicScorer mode."
        )
        return HeuristicScorer()

    scorer = PioneerScorer(api_key)

    if check_reachability and not scorer.is_reachable(timeout=reachability_timeout):
        logger.warning(
            "Pioneer credentials present but endpoint unreachable at %s — "
            "operating in HeuristicScorer mode.",
            scorer.base_url,
        )
        return HeuristicScorer()

    logger.info("Pioneer reachable — using PioneerScorer (model_id=%s).", scorer.model_id)
    return scorer


def _resolve_api_key(config: Any) -> Optional[str]:
    """Extract the Pioneer API key from a Config, a PioneerConfig, or a mapping."""
    # angent.config.Config -> .pioneer.api_key
    pioneer = getattr(config, "pioneer", None)
    if pioneer is not None and getattr(pioneer, "api_key", None):
        return pioneer.api_key
    # A PioneerConfig passed directly.
    if getattr(config, "api_key", None):
        return config.api_key
    # A plain mapping / env-like object.
    if isinstance(config, dict):
        return config.get("PIONEER_API_KEY") or config.get("pioneer_api_key")
    return None
