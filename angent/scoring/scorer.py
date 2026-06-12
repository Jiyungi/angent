"""The pluggable scoring interface and the always-available heuristic default.

Angent qualifies each :class:`~angent.models.Candidate` against the investor's
Thesis with a numeric fit score in ``[0, 100]`` (Requirement 5.1). Scoring is
pluggable behind a *single* interface so that the optional Pioneer model and the
built-in heuristic share identical method signatures and return structures
(Requirement 7.1): the Qualifier and Optimizer never branch on the concrete
scorer type.

This module defines:

* :class:`Scorer` — the ``typing.Protocol`` both implementations satisfy
  (``score(candidate, thesis) -> int`` clamped to ``[0,100]`` and
  ``learn(outcomes) -> LearnResult``). The signatures here are the contract
  ``PioneerScorer`` must match exactly for drop-in compatibility.
* :class:`LearnResult` — the structured return of ``learn`` (post-learn weight
  summary + how many outcomes were consumed).
* :class:`HeuristicScorer` — the default, always-available scorer. It blends
  three signals — keyword match (thesis terms vs. candidate name/signals),
  recency of ``first_activity`` (newer scores higher), and signal weight
  (stars / points / comments) — into a clamped integer score, and nudges its
  blend weights from reply/open outcomes in ``learn`` (Requirements 6.3, 6.6).

Nothing here performs I/O. Scores are deterministic given a candidate, a thesis,
and the injectable clock used for recency, which keeps the heuristic testable.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Mapping, Optional, Protocol, runtime_checkable

from angent.models import Candidate, Outcome


# --- Learn result -----------------------------------------------------------


@dataclass
class LearnResult:
    """Structured outcome of feeding reply/open outcomes to a scorer.

    Both :class:`HeuristicScorer` and ``PioneerScorer`` return this shape so the
    Optimizer can treat learning uniformly (Requirement 7.1). It summarizes what
    was consumed and the resulting blend weights so a caller (or the demo log)
    can show that learning actually moved the model.

    Attributes:
        num_outcomes: How many outcomes were provided as learning signal.
        num_replies: How many of those outcomes were replies (the positive
            signal that drives weight adjustment).
        weights: The blend weights *after* learning (component name -> weight).
            For scorers without explicit weights this may be empty.
        adjusted: True when ``learn`` actually changed the weights.
        note: Short human-readable summary of what learning did.
    """

    num_outcomes: int
    num_replies: int = 0
    weights: dict[str, float] = field(default_factory=dict)
    adjusted: bool = False
    note: str = ""


# --- Scorer protocol --------------------------------------------------------


@runtime_checkable
class Scorer(Protocol):
    """The single scoring interface backing Pioneer and the heuristic default.

    Implementations MUST keep these exact signatures and return structures so
    they are interchangeable wherever a ``Scorer`` is expected (Requirement
    7.1). The Qualifier calls :meth:`score`; the Optimizer calls :meth:`learn`.
    """

    def score(self, candidate: Candidate, thesis: str) -> int:
        """Return the candidate's fit against the thesis as an int in ``[0,100]``."""
        ...

    def learn(self, outcomes: list[Outcome]) -> LearnResult:
        """Update the scorer from reply/open outcomes and report what changed."""
        ...


# --- Heuristic blend tuning -------------------------------------------------

# Default blend weights (must sum to 1.0). Keyword fit dominates because thesis
# alignment is the primary qualification signal; recency and raw signal volume
# refine it. ``learn`` nudges these from reply outcomes.
_DEFAULT_WEIGHTS: dict[str, float] = {
    "keyword": 0.5,
    "recency": 0.2,
    "signal": 0.3,
}

# Recency horizon: candidates are only scanned within the last 90 days
# (Requirement 4), so recency decays linearly to 0 across that window.
_RECENCY_HORIZON_DAYS = 90.0

# Signal volume is log-scaled; this cap maps a "very strong" raw signal total to
# a recency/keyword-comparable 100. log10(1 + 10000) ≈ 4.0.
_SIGNAL_SATURATION = 10_000.0

# How hard each ``learn`` call nudges the weights toward what replied (0..1).
_LEARN_RATE = 0.1

# Common English words ignored when extracting thesis terms so the keyword match
# reflects meaningful tokens rather than filler.
_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "are", "you", "your",
        "our", "from", "into", "out", "but", "not", "all", "any", "can",
        "will", "has", "have", "had", "was", "were", "they", "them", "their",
        "who", "what", "which", "when", "where", "how", "why", "a", "an",
        "to", "of", "in", "on", "at", "by", "is", "it", "as", "or", "be",
        "we", "us", "i", "startups", "startup", "companies", "company",
        "building", "build", "looking", "invest", "investing", "early",
    }
)

# Signal keys that count as "engagement volume" across the supported sources
# (GitHub stars/commits, Hacker News points/comments, etc.).
_SIGNAL_KEYS = ("stars", "points", "comments", "commits", "followers", "downloads")

_WORD_RE = re.compile(r"[a-z0-9]+")


def _clamp_int(value: float) -> int:
    """Round and clamp a blended score into the inclusive integer range [0,100]."""
    return max(0, min(100, int(round(value))))


def _extract_terms(text: str) -> set[str]:
    """Lowercase, tokenize, drop stopwords/short tokens; return the term set."""
    return {
        tok
        for tok in _WORD_RE.findall(text.lower())
        if len(tok) >= 3 and tok not in _STOPWORDS
    }


class HeuristicScorer:
    """Default scorer: a keyword / recency / signal-weight blend (Requirement 7.2).

    The score is a weighted blend of three components, each computed on a 0..100
    scale and then combined and clamped to an integer in ``[0,100]``:

    1. **keyword** — fraction of meaningful thesis terms that appear in the
       candidate's name, URL, and signal values.
    2. **recency** — how recently ``first_activity`` occurred, decaying linearly
       to 0 across the 90-day scan horizon (newer scores higher).
    3. **signal** — log-scaled engagement volume (stars / points / comments /
       commits / followers / downloads).

    :meth:`learn` adjusts the blend weights from reply outcomes so qualification
    improves as real replies arrive (Requirements 6.3, 6.6). The scorer caches a
    per-candidate component breakdown during :meth:`score` so ``learn`` can
    reinforce the components that the replied-to candidates scored highest on;
    when outcomes can't be matched to a cached breakdown it falls back to a
    conservative global nudge so learning still makes measurable progress.

    The clock is injectable for deterministic recency in tests; production uses
    ``datetime.now``.
    """

    def __init__(
        self,
        weights: Optional[Mapping[str, float]] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        base = dict(_DEFAULT_WEIGHTS if weights is None else weights)
        # Ensure all three components are present, then normalize to sum to 1.
        for key in _DEFAULT_WEIGHTS:
            base.setdefault(key, 0.0)
        self.weights: dict[str, float] = self._normalize(base)
        self._clock = clock or datetime.now
        # company-key -> last component breakdown, used by learn() to attribute
        # replies to the components that earned them.
        self._breakdowns: dict[str, dict[str, float]] = {}

    # -- public Scorer interface --------------------------------------------

    def score(self, candidate: Candidate, thesis: str) -> int:
        """Blend keyword/recency/signal components into a clamped [0,100] int."""
        components = self._components(candidate, thesis)
        # Cache the breakdown so a later learn() can attribute reply credit.
        self._breakdowns[self._candidate_key(candidate)] = components
        blended = sum(self.weights[name] * components[name] for name in self.weights)
        return _clamp_int(blended)

    def learn(self, outcomes: list[Outcome]) -> LearnResult:
        """Nudge blend weights toward components that earned replies.

        Replies are the positive signal. For each reply we try to recover the
        scored candidate's component breakdown (cached during :meth:`score`) and
        accumulate which components scored highest; weights then move toward
        those components by :data:`_LEARN_RATE` and are renormalized. When no
        replies are present, or none can be matched to a cached breakdown, a
        conservative global nudge keeps learning monotonic and observable.
        """
        outcomes = outcomes or []
        num_outcomes = len(outcomes)
        replies = [o for o in outcomes if getattr(o, "kind", None) == "reply"]
        num_replies = len(replies)

        if num_outcomes == 0:
            return LearnResult(
                num_outcomes=0,
                num_replies=0,
                weights=dict(self.weights),
                adjusted=False,
                note="no outcomes to learn from",
            )

        # Accumulate the component scores of candidates that drew a reply.
        credit: dict[str, float] = {name: 0.0 for name in self.weights}
        matched = 0
        for outcome in replies:
            breakdown = self._breakdown_for_outcome(outcome)
            if breakdown is None:
                continue
            matched += 1
            for name in self.weights:
                credit[name] += breakdown.get(name, 0.0)

        if matched > 0:
            # Move weights toward the components replied candidates scored on.
            total_credit = sum(credit.values())
            if total_credit > 0:
                target = {name: credit[name] / total_credit for name in self.weights}
                new_weights = {
                    name: (1 - _LEARN_RATE) * self.weights[name]
                    + _LEARN_RATE * target[name]
                    for name in self.weights
                }
                self.weights = self._normalize(new_weights)
                note = (
                    f"reinforced from {matched} replied candidate(s); "
                    f"weights now {self._fmt_weights()}"
                )
                return LearnResult(
                    num_outcomes=num_outcomes,
                    num_replies=num_replies,
                    weights=dict(self.weights),
                    adjusted=True,
                    note=note,
                )

        # Fallback global nudge when replies can't be attributed to a breakdown.
        adjusted = self._global_nudge(num_replies)
        note = (
            f"global nudge ({'reply' if num_replies else 'no-reply'} signal); "
            f"weights now {self._fmt_weights()}"
        )
        return LearnResult(
            num_outcomes=num_outcomes,
            num_replies=num_replies,
            weights=dict(self.weights),
            adjusted=adjusted,
            note=note,
        )

    # -- component scoring ---------------------------------------------------

    def _components(self, candidate: Candidate, thesis: str) -> dict[str, float]:
        """Compute the three 0..100 component scores for a candidate."""
        return {
            "keyword": self._keyword_score(candidate, thesis),
            "recency": self._recency_score(candidate),
            "signal": self._signal_score(candidate),
        }

    @staticmethod
    def _keyword_score(candidate: Candidate, thesis: str) -> float:
        """Fraction of meaningful thesis terms present in the candidate text."""
        terms = _extract_terms(thesis or "")
        if not terms:
            return 0.0
        parts = [candidate.name or "", candidate.url or ""]
        signals = candidate.signals or {}
        if isinstance(signals, Mapping):
            parts.extend(str(v) for v in signals.values())
        candidate_text = " ".join(parts)
        candidate_terms = _extract_terms(candidate_text)
        if not candidate_terms:
            return 0.0
        matched = sum(1 for term in terms if term in candidate_terms)
        return 100.0 * matched / len(terms)

    def _recency_score(self, candidate: Candidate) -> float:
        """Linear recency: 100 at activity-now, decaying to 0 at the 90-day horizon."""
        first_activity = candidate.first_activity
        if first_activity is None:
            return 50.0  # unknown recency -> neutral
        now = self._clock()
        # Coerce both to naive for a safe subtraction (matches core convention).
        if first_activity.tzinfo is not None:
            first_activity = first_activity.replace(tzinfo=None)
        if now.tzinfo is not None:
            now = now.replace(tzinfo=None)
        age_days = (now - first_activity).total_seconds() / 86400.0
        if age_days <= 0:
            return 100.0
        if age_days >= _RECENCY_HORIZON_DAYS:
            return 0.0
        return 100.0 * (1.0 - age_days / _RECENCY_HORIZON_DAYS)

    @staticmethod
    def _signal_score(candidate: Candidate) -> float:
        """Log-scaled engagement volume across known signal keys (0..100)."""
        signals = candidate.signals or {}
        if not isinstance(signals, Mapping):
            return 0.0
        total = 0.0
        for key in _SIGNAL_KEYS:
            value = signals.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and value > 0:
                total += float(value)
        if total <= 0:
            return 0.0
        scaled = math.log10(1.0 + total) / math.log10(1.0 + _SIGNAL_SATURATION)
        return max(0.0, min(100.0, scaled * 100.0))

    # -- learning helpers ----------------------------------------------------

    def _global_nudge(self, num_replies: int) -> bool:
        """Conservative weight nudge used when replies can't be attributed.

        With replies present we trust thesis targeting more (nudge ``keyword``
        up); with outcomes but no replies we explore fresher candidates (nudge
        ``recency`` up). Either way weights are renormalized.
        """
        target_component = "keyword" if num_replies > 0 else "recency"
        new_weights = dict(self.weights)
        new_weights[target_component] += _LEARN_RATE
        self.weights = self._normalize(new_weights)
        return True

    def _breakdown_for_outcome(self, outcome: Outcome) -> Optional[dict[str, float]]:
        """Look up the cached component breakdown for an outcome's candidate.

        Outcomes reference a candidate by ``company_id``; scoring caches the
        breakdown under the candidate key. We try the most likely identifiers
        and return ``None`` when there is no match (the global nudge then runs).
        """
        for key in (getattr(outcome, "company_id", None), getattr(outcome, "email_id", None)):
            if key and key in self._breakdowns:
                return self._breakdowns[key]
        return None

    @staticmethod
    def _candidate_key(candidate: Candidate) -> str:
        """Stable cache key for a candidate's component breakdown."""
        return candidate.source_unique_id or candidate.url or candidate.name

    @staticmethod
    def _normalize(weights: Mapping[str, float]) -> dict[str, float]:
        """Clamp negatives to 0 and rescale so the weights sum to 1.0."""
        clamped = {name: max(0.0, float(w)) for name, w in weights.items()}
        total = sum(clamped.values())
        if total <= 0:
            # Degenerate input -> fall back to an even split.
            n = len(clamped) or 1
            return {name: 1.0 / n for name in clamped}
        return {name: w / total for name, w in clamped.items()}

    def _fmt_weights(self) -> str:
        return "{" + ", ".join(f"{k}={v:.2f}" for k, v in self.weights.items()) + "}"
