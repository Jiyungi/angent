"""Pure goal validation that gates Control_Loop initiation (Requirement 1).

The investor submits a free-text Thesis plus a structured Goal
(``{target_metric, deadline, email_budget}``). Before any persistence or Tick
runs, :func:`validate_goal` checks the submission against the design's "Goal
Validation" rules and reports the *single* offending field on rejection so the
caller can surface a precise message (Requirements 1.1, 1.3, 1.4).

This module performs **no I/O**. ``validate_goal`` is a pure function: the
current time is injected via the ``now`` parameter (defaulting to
``datetime.now()``), so deadline-bound checks are deterministic and testable.

Timezone convention: the rest of the core uses naive ``datetime`` values
(``Goal.deadline`` is a bare ``datetime`` and the ClickHouse columns are
``DateTime`` with no zone — see ``angent/models.py`` / ``angent/persistence``).
We follow that convention here: comparisons are performed against naive local
``datetime.now()``. To stay robust if a caller hands us a timezone-aware
deadline, both sides are coerced to naive before comparison.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Optional

# --- Validation bounds (from the design's "Goal Validation" section) --------

THESIS_MIN_LEN = 1
THESIS_MAX_LEN = 5000

EMAIL_BUDGET_MIN = 1
EMAIL_BUDGET_MAX = 1000

DEADLINE_MIN_AHEAD = timedelta(minutes=1)
DEADLINE_MAX_AHEAD = timedelta(days=365)

# Required keys in the goal_input dict, checked for presence in this order.
REQUIRED_FIELDS = ("target_metric", "deadline", "email_budget")


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of validating a (thesis, goal_input) submission.

    Attributes:
        ok: True when the submission passed every rule.
        offending_field: On rejection, the single field that failed
            (e.g. ``"thesis"``, ``"email_budget"``, ``"deadline"``). ``None``
            when ``ok`` is True.
        message: Human-readable reason, suitable for returning to the Investor.
            Empty on success.
    """

    ok: bool
    offending_field: Optional[str] = None
    message: str = ""


def _reject(field: str, message: str) -> ValidationResult:
    return ValidationResult(ok=False, offending_field=field, message=message)


def _as_naive(value: datetime) -> datetime:
    """Drop tzinfo so naive and aware datetimes can be compared safely.

    The core stores naive ``DateTime`` values; if a caller supplies a
    timezone-aware deadline we convert it to the equivalent naive wall-clock
    time rather than raising a ``TypeError`` on comparison.
    """
    if value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


def _coerce_deadline(value: Any) -> Optional[datetime]:
    """Best-effort coercion of a deadline value into a ``datetime``.

    Accepts an actual ``datetime`` (the expected shape), an ISO-8601 string,
    or a POSIX timestamp (int/float seconds). Returns ``None`` when the value
    cannot be interpreted as a point in time, so the caller treats it as an
    invalid deadline rather than crashing.
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, bool):  # guard: bool is an int subclass
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        # Support a trailing "Z" (UTC) which fromisoformat rejects on older runtimes.
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None
    return None


def validate_goal(
    thesis: str,
    goal_input: Mapping[str, Any],
    now: Optional[Callable[[], datetime] | datetime] = None,
) -> ValidationResult:
    """Validate a Thesis + Goal submission before the loop starts (Requirement 1).

    Rules, checked in this order (first failure wins so ``offending_field`` is
    unambiguous):

    1. ``thesis`` length must be in ``[1, 5000]`` characters.
    2. ``target_metric``, ``deadline``, and ``email_budget`` must all be present.
    3. ``email_budget`` must be an integer in ``[1, 1000]``.
    4. ``deadline`` must be between ``now + 1 minute`` and ``now + 365 days``.

    Args:
        thesis: The investor-provided thesis string.
        goal_input: A mapping that should contain ``target_metric``,
            ``deadline``, and ``email_budget``. May be missing fields.
        now: Injectable clock for testability. Either a zero-arg callable
            returning the current time, or a concrete ``datetime``. Defaults to
            ``datetime.now()`` (naive local time, matching the core convention).

    Returns:
        A :class:`ValidationResult`. ``ok=True`` with no offending field on
        success; otherwise ``ok=False`` with the single offending field and a
        descriptive message. No persistence or side effects occur either way.
    """
    # Resolve the injectable clock to a concrete instant.
    if callable(now):
        current = now()
    elif isinstance(now, datetime):
        current = now
    else:
        current = datetime.now()
    current = _as_naive(current)

    # 1. Thesis length.
    if not isinstance(thesis, str):
        return _reject("thesis", "thesis must be a string")
    thesis_len = len(thesis)
    if thesis_len < THESIS_MIN_LEN or thesis_len > THESIS_MAX_LEN:
        return _reject(
            "thesis",
            f"thesis length must be between {THESIS_MIN_LEN} and {THESIS_MAX_LEN} "
            f"characters (got {thesis_len})",
        )

    # 2. Required-field presence (return the first missing field).
    for field in REQUIRED_FIELDS:
        if field not in goal_input or goal_input[field] is None:
            return _reject(field, f"missing required field: {field}")

    # 3. email_budget range.
    email_budget = goal_input["email_budget"]
    if isinstance(email_budget, bool) or not isinstance(email_budget, int):
        return _reject("email_budget", "email_budget must be an integer")
    if email_budget < EMAIL_BUDGET_MIN or email_budget > EMAIL_BUDGET_MAX:
        return _reject(
            "email_budget",
            f"email_budget must be between {EMAIL_BUDGET_MIN} and "
            f"{EMAIL_BUDGET_MAX} (got {email_budget})",
        )

    # 4. deadline bounds (now+1min .. now+365days).
    deadline = _coerce_deadline(goal_input["deadline"])
    if deadline is None:
        return _reject("deadline", "deadline must be a valid datetime")
    deadline = _as_naive(deadline)
    earliest = current + DEADLINE_MIN_AHEAD
    latest = current + DEADLINE_MAX_AHEAD
    if deadline < earliest:
        return _reject(
            "deadline",
            "deadline must be at least 1 minute in the future",
        )
    if deadline > latest:
        return _reject(
            "deadline",
            "deadline must be no more than 365 days in the future",
        )

    return ValidationResult(ok=True)
