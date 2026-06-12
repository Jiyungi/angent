"""Single runnable entrypoint for the Angent core (Requirements 11.1, 12, 16, 22).

This module wires every major piece of the system into one runnable Python core
that can be launched with::

    python -m angent.main                 # demo run with sensible defaults
    python -m angent.main --smoke          # fast 1-tick smoke (Pioneer may fall
                                           #   back to the heuristic scorer)

It is the integration seam where the goal-driven Control_Loop, the six agents
(Planner, Scanner, Qualifier, Writer, Optimizer — plus the Scorer), the
Governance_Gate, the Sender, ClickHouse persistence, Langfuse tracing, and the
per-stage StageLogger all come together with **no orphaned components**: every
major piece is reachable from this entrypoint.

Flow (mirrors the lifecycle in design.md → "Control Loop and Planner"):

  1. ``load_config`` — read credentials/settings from the environment/.env.
  2. ``ClickHouseClient.from_config`` + ``create_schema`` — ensure the
     blackboard tables exist (Requirement 12.1, 12.2, 12.3, 22).
  3. Build a :class:`Tracer` (Langfuse with a safe disabled fallback, Req 13.3)
     and a :class:`StageLogger` (console + backend logs, Requirement 16).
  4. Construct the :class:`ControlLoop`. It lazily builds the Planner, Scanner,
     Qualifier, Writer, Scorer, Governance_Gate, Sender and Optimizer from the
     shared client/config, so constructing + running the loop reaches all of
     them — nothing is left orphaned.
  5. Initiate the run all-or-nothing via ``ControlLoop.start`` (Requirement 1).
  6. Drive it with ``ControlLoop.run`` inside a tracer step, then emit a
     per-stage StageLogger record for every Tick (progress, qualifications,
     drafts, sends, reply_rate_trend) plus a final summary (Requirement 16.1).

The run is robust to slow/unavailable sponsor integrations: the loop already
contains per-stage fallbacks (e.g. a Pioneer timeout falls back to the heuristic
scorer), so a smoke invocation wires everything together without long live work.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

from .config import load_config
from .demo_config import demo_goal, load_seeded_outcomes
from .loop.control_loop import ControlLoop, RunResult
from .models import Goal
from .observability.logging import StageLogger
from .observability.tracing import Tracer
from .persistence.clickhouse import ClickHouseClient

logger = logging.getLogger("angent.main")


# --- Sensible demo defaults -------------------------------------------------

DEFAULT_THESIS = (
    "We invest in early-stage developer-tools and AI-infrastructure startups: "
    "open-source frameworks, LLM agent tooling, data and ML platforms, and "
    "API-first products that help engineers build and ship software faster."
)
DEFAULT_TARGET_METRIC = 0.2
DEFAULT_EMAIL_BUDGET = 3
DEFAULT_DEADLINE_MINUTES = 10


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m angent.main",
        description="Run the Angent goal-driven Control_Loop end to end.",
    )
    parser.add_argument(
        "--thesis",
        default=DEFAULT_THESIS,
        help="The investor thesis threaded into the Qualifier/Writer.",
    )
    parser.add_argument(
        "--target",
        type=float,
        default=DEFAULT_TARGET_METRIC,
        help="Goal target_metric (e.g. target reply rate).",
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=DEFAULT_EMAIL_BUDGET,
        help="Goal email_budget (hard cap on emails sent, 1..1000).",
    )
    parser.add_argument(
        "--deadline-minutes",
        type=int,
        default=DEFAULT_DEADLINE_MINUTES,
        help="Minutes from now until the goal deadline.",
    )
    parser.add_argument(
        "--max-ticks",
        type=int,
        default=None,
        help="Safety cap on the number of Ticks (default: run to termination).",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Fast smoke run: tiny budget + 1 Tick so wiring is exercised quickly.",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help=(
            "Demo run: use the configured demo Goal (target in [0,1], now+120s "
            "deadline, 8-email budget) and seed historical outcomes before the "
            "first Tick."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Wire and run the full Angent core. Returns a process exit code."""
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING, format="    (log) %(name)s: %(message)s"
    )

    stage = StageLogger()

    # In smoke mode keep the run tiny so we exercise the wiring without a long
    # live run: tiny budget and a single Tick (Pioneer may time out -> heuristic).
    thesis = args.thesis
    target_metric = args.target
    email_budget = 1 if args.smoke else args.budget
    deadline_minutes = args.deadline_minutes
    max_ticks = 1 if args.smoke else args.max_ticks

    stage.progress(
        "Angent core starting",
        smoke=args.smoke,
        target_metric=target_metric,
        email_budget=email_budget,
        max_ticks=max_ticks,
    )

    # 1. Configuration ------------------------------------------------------
    config = load_config()
    summary = config.summary()
    stage.progress("Configuration loaded (secret-free integration status)", **summary)

    if not summary.get("clickhouse"):
        stage.sponsor_failure(
            "clickhouse",
            "CLICKHOUSE_HOST is not set; the blackboard is required to run.",
        )
        return 2

    # 2. Persistence: ClickHouse schema (Req 12.1, 12.2, 12.3, 22) ----------
    client = ClickHouseClient.from_config(config)
    try:
        client.connect()
    except Exception as exc:  # noqa: BLE001 - report honestly, do not crash
        stage.sponsor_failure("clickhouse", f"connect failed: {exc}")
        return 2

    schema_results = client.create_schema()
    ok_tables = sorted(t for t, r in schema_results.items() if r.ok)
    bad_tables = {t: r.error for t, r in schema_results.items() if not r.ok}
    stage.progress("ClickHouse schema ensured", tables=",".join(ok_tables) or "(none)")
    if bad_tables:
        stage.sponsor_failure("clickhouse", f"schema creation failed: {bad_tables}")
        client.close()
        return 2

    # 3. Observability: tracing (disabled fallback) + per-stage logging -----
    tracer = Tracer()  # Langfuse if configured, else a safe disabled no-op.
    stage.progress("Tracing initialized", langfuse_enabled=tracer.enabled)

    # 4. Build the Control_Loop. It lazily constructs the Planner, Scanner,
    #    Qualifier, Writer, Scorer, Governance_Gate, Sender and Optimizer from
    #    the shared client/config, so the six agents + gate + sender + optimizer
    #    are all reachable from here (no orphaned components).
    loop = ControlLoop(client, thesis=thesis, config=config)

    # 5. Goal initiation (all-or-nothing) -----------------------------------
    if args.demo:
        goal = demo_goal()
    else:
        goal = Goal(
            target_metric=target_metric,
            deadline=datetime.now() + timedelta(minutes=deadline_minutes),
            email_budget=email_budget,
        )
    start_result = loop.start(thesis, goal)
    if not start_result.ok:
        stage.progress(
            "Goal initiation failed",
            error_kind=start_result.error_kind,
            offending_field=start_result.offending_field,
            message=start_result.message,
        )
        client.close()
        return 2

    run_handle = start_result.run_handle
    assert run_handle is not None
    run_id = run_handle.run_id
    stage.progress(
        "Run initiated",
        run_id=run_id,
        target_metric=goal.target_metric,
        email_budget=goal.email_budget,
    )

    # In demo mode, seed historical outcomes BEFORE the first Tick so the
    # scorer/analytics start with prior reply/open signal (Requirement 12.5).
    if args.demo:
        seed_result = load_seeded_outcomes(client, run_id=run_id)
        stage.progress(
            "Seeded historical outcomes",
            run_id=run_id,
            seeded=len(seed_result.stored),
            requested=seed_result.requested,
            unstored=len(seed_result.unstored),
        )

    # 6. Drive the loop inside a tracer step, then emit per-stage records ----
    with tracer.trace_step("control_loop.run", input={"run_id": run_id}) as step:
        try:
            result: RunResult = loop.run(
                run_handle.state, thesis=thesis, max_ticks=max_ticks
            )
        except Exception as exc:  # noqa: BLE001 - surface, persist nothing partial
            stage.sponsor_failure("control_loop", f"run raised: {exc}")
            client.close()
            return 1
        step.output = {
            "ticks": result.tick_count,
            "stop_reason": result.stop_reason.value if result.stop_reason else None,
            "persisted": result.persisted,
        }
    tracer.flush()

    _emit_stage_records(stage, result)

    # Final summary --------------------------------------------------------
    total_sent = sum(o.emails_sent for o in result.outcomes)
    total_qualified = sum(o.qualified_count for o in result.outcomes)
    total_drafts = sum(o.drafts_created for o in result.outcomes)
    total_candidates = sum(o.candidates_found for o in result.outcomes)
    stop_reason = result.stop_reason.value if result.stop_reason else "max_ticks"
    stage.progress(
        "Run complete",
        run_id=run_id,
        ticks=result.tick_count,
        candidates=total_candidates,
        qualified=total_qualified,
        drafts=total_drafts,
        emails_sent=total_sent,
        final_reply_rate=round(result.final_state.reply_rate, 4),
        stop_reason=stop_reason,
        final_state_persisted=result.persisted,
    )

    client.close()
    return 0


def _emit_stage_records(stage: StageLogger, result: RunResult) -> None:
    """Emit a per-stage StageLogger record for each Tick (Requirement 16.1).

    Covers all canonical stages: progress, qualifications, drafts, sends and
    reply_rate_trend — so the run is fully observable from the console/logs.
    """
    for outcome in result.outcomes:
        tick = outcome.tick_index
        if outcome.failed_stage:
            stage.progress(
                "Tick contained a stage failure",
                tick=tick,
                failed_stage=outcome.failed_stage,
            )
        stage.progress("Tick progress", tick=tick, candidates=outcome.candidates_found)
        stage.qualifications("Qualified candidates", tick=tick, count=outcome.qualified_count)
        stage.drafts("Drafts created", tick=tick, count=outcome.drafts_created)
        stage.sends("Emails sent", tick=tick, count=outcome.emails_sent)
        stage.reply_rate_trend(
            "Reply-rate after Tick",
            tick=tick,
            replies=outcome.replies,
            reply_rate=round(outcome.reply_rate, 4),
        )


if __name__ == "__main__":
    sys.exit(main())
