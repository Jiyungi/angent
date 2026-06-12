"""Demo configuration + seeded-history loader for the Angent control loop.

This module centralizes everything needed to run a *demo* of the goal-driven
Control_Loop with realistic starting conditions:

  * :func:`demo_goal` returns the configured demo :class:`~angent.models.Goal`
    (a ``target_metric`` in ``[0, 1]``, a ``now + 120s`` deadline, an 8-email
    budget) that passes :func:`~angent.loop.validation.validate_goal`
    (Requirements 1, 19.1).
  * :data:`TICK_INTERVAL_MIN_S` / :data:`TICK_INTERVAL_MAX_S` /
    :func:`tick_interval_seconds` express the demo Tick cadence (3-10s) so the
    loop driver paces Ticks like a live run (Requirement 19.9).
  * :func:`load_seeded_outcomes` inserts a realistic mix of historical
    reply/open :class:`~angent.models.Outcome` rows (``seeded=True``) into the
    ``outcomes`` table **before the first Tick**, so the scorer/analytics start
    with prior signal rather than a cold start (Requirements 12.5, 22).

Design references:
  * design.md -> Data Models -> ``outcomes`` (MergeTree; ``seeded`` flag).
  * Requirement 12.5 -- seeded historical outcomes loaded before the first Tick.
  * Requirement 19.1 / 19.9 -- demo goal + bounded Tick interval.
  * Requirement 22 -- ClickHouse blackboard is the shared source of truth.

The seed loader reuses :class:`~angent.agents.optimizer.Optimizer` so seeded
outcomes are normalized + stored through the same durable, bounded-retry path
the live loop uses (``Outcome.seeded`` is persisted as the ``outcomes.seeded``
column). It works against any object exposing the ClickHouse client's
``insert``/``query`` contract.
"""

from __future__ import annotations

import logging
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .agents.optimizer import Optimizer
from .models import Goal, Outcome

logger = logging.getLogger("angent.demo_config")


# --- Demo Goal --------------------------------------------------------------

# target_metric is a reply rate in [0, 1] (Requirement 19.1). A modest 0.2
# target is achievable within the short demo window while still requiring the
# loop to make real progress.
DEMO_TARGET_METRIC: float = 0.2
# The demo runs against a tight wall-clock window so termination is observable
# live. 120s is comfortably above validate_goal's "at least 1 minute" floor.
DEMO_DEADLINE_SECONDS: int = 120
# Hard cap on emails the demo may send (Requirement 19.1).
DEMO_EMAIL_BUDGET: int = 8

# Demo Tick cadence bounds in seconds (Requirement 19.9): each Tick is paced by
# a delay drawn from [3, 10]s so the demo feels like a live, deliberate loop.
TICK_INTERVAL_MIN_S: float = 3.0
TICK_INTERVAL_MAX_S: float = 10.0


def demo_goal(now: Optional[datetime] = None) -> Goal:
    """Return the configured demo :class:`~angent.models.Goal`.

    The deadline is ``now + DEMO_DEADLINE_SECONDS`` (tz-aware UTC), the
    ``target_metric`` is a reply rate in ``[0, 1]`` and the ``email_budget`` is
    the demo cap. The resulting Goal satisfies
    :func:`~angent.loop.validation.validate_goal` for any reasonable thesis.

    Args:
        now: Optional base instant (tz-aware preferred). Defaults to UTC now.
    """
    base = now or datetime.now(timezone.utc)
    return Goal(
        target_metric=DEMO_TARGET_METRIC,
        deadline=base + timedelta(seconds=DEMO_DEADLINE_SECONDS),
        email_budget=DEMO_EMAIL_BUDGET,
    )


def tick_interval_seconds(rng: Optional[random.Random] = None) -> float:
    """Return a demo Tick delay in seconds, drawn from ``[3, 10]`` (Req 19.9).

    Args:
        rng: Optional :class:`random.Random` for deterministic tests. Uses the
            module-global RNG when omitted.
    """
    r = rng or random
    return r.uniform(TICK_INTERVAL_MIN_S, TICK_INTERVAL_MAX_S)


# --- Seeded historical outcomes ---------------------------------------------

# A realistic demo history skews toward opens with a smaller slice of replies,
# roughly matching a believable early-stage outreach reply rate.
SEEDED_REPLY_FRACTION: float = 0.25
# Default number of historical outcomes to seed before the first Tick.
DEFAULT_SEED_COUNT: int = 12
# Spread seeded events across the recent past (days) so analytics buckets show a
# trend rather than a single instant.
SEED_HISTORY_DAYS: int = 14


@dataclass
class SeedLoadResult:
    """Summary of a :func:`load_seeded_outcomes` call.

    Attributes:
        run_id: The run the seeded outcomes were scoped to.
        requested: How many seeded outcomes were built.
        stored: Outcomes successfully persisted to ``outcomes``.
        unstored: Outcomes that could not be stored (retained by the Optimizer).
        errors: Human-readable error indications for any unstored outcomes.
    """

    run_id: str
    requested: int
    stored: list[Outcome] = field(default_factory=list)
    unstored: list[Outcome] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when every requested seeded outcome was stored."""
        return not self.unstored and len(self.stored) == self.requested


def build_seeded_outcomes(
    run_id: str,
    count: int = DEFAULT_SEED_COUNT,
    *,
    now: Optional[datetime] = None,
    rng: Optional[random.Random] = None,
) -> list[Outcome]:
    """Build ``count`` seeded historical :class:`~angent.models.Outcome` objects.

    Each outcome carries ``seeded=True``, a stable ``outcome_id``, the supplied
    ``run_id``, synthetic ``email_id``/``company_id`` references, a ``kind`` of
    ``'reply'`` or ``'open'`` (a realistic mix), and a tz-aware UTC
    ``occurred_at`` spread across the recent past (Requirement 12.5).

    This is pure (no I/O) so it is easy to unit-test; :func:`load_seeded_outcomes`
    persists the result.
    """
    base = now or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    r = rng or random.Random()

    outcomes: list[Outcome] = []
    for i in range(max(0, int(count))):
        is_reply = r.random() < SEEDED_REPLY_FRACTION
        # Spread occurred_at over the past SEED_HISTORY_DAYS, newest-ish last.
        age_seconds = r.uniform(0, SEED_HISTORY_DAYS * 24 * 3600)
        occurred_at = base - timedelta(seconds=age_seconds)
        company_id = f"seed-company-{i % 6:02d}"
        outcomes.append(
            Outcome(
                email_id=f"seed-email-{i:03d}",
                company_id=company_id,
                kind="reply" if is_reply else "open",
                occurred_at=occurred_at,
                seeded=True,
                run_id=run_id,
                outcome_id=str(uuid.uuid4()),
            )
        )
    return outcomes


def load_seeded_outcomes(
    client: Any,
    *,
    run_id: str,
    count: int = DEFAULT_SEED_COUNT,
    now: Optional[datetime] = None,
    rng: Optional[random.Random] = None,
) -> "SeedLoadResult":
    """Insert seeded historical outcomes into ``outcomes`` before the first Tick.

    Builds ``count`` seeded :class:`~angent.models.Outcome` rows (via
    :func:`build_seeded_outcomes`) and stores them through an
    :class:`~angent.agents.optimizer.Optimizer`, which writes them to the
    ``outcomes`` ClickHouse table (``seeded`` persisted as ``1``) using the
    durable bounded-retry path the live loop uses (Requirements 12.5, 22).

    Args:
        client: A ClickHouse client exposing ``insert``/``query`` (e.g.
            :class:`~angent.persistence.clickhouse.ClickHouseClient`).
        run_id: The demo run id the seeded rows are scoped to.
        count: How many historical outcomes to seed (default 12).
        now: Optional base instant for ``occurred_at`` spread.
        rng: Optional RNG for deterministic seeding in tests.

    Returns:
        A :class:`SeedLoadResult` with the stored + unstored outcomes.
    """
    outcomes = build_seeded_outcomes(run_id, count, now=now, rng=rng)
    optimizer = Optimizer(client, run_id=run_id)
    store_result = optimizer.store(outcomes)
    logger.info(
        "Seeded %d/%d historical outcome(s) into outcomes for run_id=%s",
        len(store_result.stored),
        len(outcomes),
        run_id,
    )
    return SeedLoadResult(
        run_id=run_id,
        requested=len(outcomes),
        stored=store_result.stored,
        unstored=store_result.unstored,
        errors=[str(e) for e in store_result.errors],
    )
