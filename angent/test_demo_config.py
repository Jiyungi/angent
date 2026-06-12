"""Unit tests for the demo configuration + seeded-outcome loader.

These tests exercise the pure, I/O-free parts of :mod:`angent.demo_config`
(no live ClickHouse required) plus the seed loader against an in-memory fake
client, and assert the demo Goal passes the real goal validator.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone

from angent.demo_config import (
    DEMO_EMAIL_BUDGET,
    TICK_INTERVAL_MAX_S,
    TICK_INTERVAL_MIN_S,
    build_seeded_outcomes,
    demo_goal,
    load_seeded_outcomes,
    tick_interval_seconds,
)
from angent.loop.validation import validate_goal


def test_demo_goal_passes_validation():
    g = demo_goal()
    result = validate_goal(
        "x" * 50,
        {
            "target_metric": g.target_metric,
            "deadline": g.deadline,
            "email_budget": g.email_budget,
        },
    )
    assert result.ok, getattr(result, "offending_field", None)
    assert 0.0 <= g.target_metric <= 1.0
    assert g.email_budget == DEMO_EMAIL_BUDGET


def test_tick_interval_within_bounds():
    rng = random.Random(1234)
    for _ in range(100):
        delay = tick_interval_seconds(rng)
        assert TICK_INTERVAL_MIN_S <= delay <= TICK_INTERVAL_MAX_S


def test_build_seeded_outcomes_are_seeded_and_tz_aware():
    now = datetime.now(timezone.utc)
    outcomes = build_seeded_outcomes("run-1", count=20, now=now, rng=random.Random(7))
    assert len(outcomes) == 20
    for o in outcomes:
        assert o.seeded is True
        assert o.run_id == "run-1"
        assert o.outcome_id
        assert o.kind in ("reply", "open")
        assert o.occurred_at.tzinfo is not None  # tz-aware
        assert o.occurred_at <= now  # historical (in the past)
    # Realistic mix: both kinds appear over a reasonable sample.
    kinds = {o.kind for o in outcomes}
    assert kinds == {"reply", "open"}


class _FakeClient:
    """Captures inserts so the loader can be tested without ClickHouse."""

    def __init__(self):
        self.rows = []

    def insert(self, table, data, column_names=None):
        assert table == "outcomes"
        self.rows.extend(data)

        class _R:
            ok = True
            error = None

        return _R()


def test_load_seeded_outcomes_inserts_with_seeded_flag():
    client = _FakeClient()
    result = load_seeded_outcomes(client, run_id="run-x", count=8, rng=random.Random(3))
    assert result.ok
    assert result.requested == 8
    assert len(result.stored) == 8
    assert not result.unstored
    # The seeded column is the last in OUTCOMES_COLUMNS and must be 1.
    assert all(row[-1] == 1 for row in client.rows)
