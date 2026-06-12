"""Checkpoint 7 demo: run the partial Control_Loop end to end (Tasks 0-6).

This is the run-and-commit checkpoint after the Qualifier group (tasks.md task
7). It is **not** new feature code and **not** an automated test harness — it is
a thin, committable wiring artifact that exercises the pieces already built and
verified individually, then prints timestamped, stage-identified console output
so the partial loop can be observed end to end (Requirements 23.5, 22.3, 22.4).

Pipeline exercised (goal init -> scanner -> qualifier):

  1. Load configuration from ``.env`` (``angent.config.load_config``).
  2. Create the ClickHouse blackboard schema
     (``ClickHouseClient.create_schema``).
  3. Initiate the run all-or-nothing via ``ControlLoop.start`` with a demo
     thesis + a valid Goal (``angent.loop.control_loop``).
  4. Build a ``Scanner`` (Hacker News always; GitHub when Airbyte credentials
     are present) and run ``scan(plan)`` for a TickPlan enabling
     ``github`` + ``hackernews`` (``angent.agents.scanner``).
  5. Select the active scorer (``select_scorer``) and run ``Qualifier.qualify``
     on the discovered candidates with a qualification threshold
     (``angent.agents.qualifier``).
  6. Confirm persistence by reading the scored rows back out of the
     ``companies`` table.

GitHub may legitimately return 0 candidates on the current Airbyte tier; that is
acceptable for this checkpoint — the Hacker News path proves the loop. Whatever
each source returns is reported honestly.

Run it from the workspace root::

    python -m angent.checkpoint7_demo

It exits non-zero only if the loop could not run at all (e.g. ClickHouse
unreachable or goal init failed); a zero-GitHub result is a success.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone

from angent.config import load_config
from angent.persistence.clickhouse import ClickHouseClient
from angent.loop.control_loop import ControlLoop
from angent.models import Goal, TickPlan
from angent.agents.scanner import Scanner
from angent.agents.qualifier import Qualifier
from angent.scoring.pioneer import select_scorer


# --- Demo parameters --------------------------------------------------------

# A focused thesis so the heuristic keyword component has meaningful terms to
# match against discovered Show HN / Launch HN launches and GitHub repos.
DEMO_THESIS = (
    "We invest in early-stage developer-tools and AI-infrastructure startups: "
    "open-source frameworks, LLM agent tooling, data and ML platforms, and "
    "API-first products that help engineers build and ship software faster."
)

# Qualification threshold (0..100). Kept modest so at least some real, recent
# launches clear the bar and are forwarded to the (future) Writer, while the
# Qualifier still scores and persists every candidate regardless.
DEMO_THRESHOLD = 20

# Cap how many candidates we score this checkpoint so a TrueFoundry explanation
# per candidate keeps the demo fast and within free-tier budgets. Every scanned
# candidate is still persisted by the Scanner; this only bounds the Qualifier
# pass for the checkpoint run.
MAX_CANDIDATES_TO_QUALIFY = 6


# --- Timestamped, stage-identified console logging --------------------------

def _stage(stage: str, message: str) -> None:
    """Emit one timestamped, stage-identified console record (Requirement 16)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    print(f"[{ts}] [{stage}] {message}", flush=True)


def _hr() -> None:
    print("-" * 78, flush=True)


def main() -> int:
    # Quiet the library loggers a touch so the stage records stand out; warnings
    # and errors from the modules still surface.
    logging.basicConfig(level=logging.WARNING, format="    (log) %(name)s: %(message)s")

    _hr()
    _stage("START", "Checkpoint 7 — partial Control_Loop (goal init -> scanner -> qualifier)")
    _hr()

    # 1. Configuration ------------------------------------------------------
    config = load_config()
    summary = config.summary()
    _stage("CONFIG", "Loaded configuration (secret-free integration status):")
    for name, configured in summary.items():
        _stage("CONFIG", f"    {name:16s}: {'configured' if configured else 'not set'}")

    if not summary.get("clickhouse"):
        _stage("CONFIG", "ClickHouse is not configured (CLICKHOUSE_HOST missing) — cannot run.")
        return 2

    # 2. ClickHouse schema --------------------------------------------------
    client = ClickHouseClient.from_config(config)
    try:
        client.connect()
    except Exception as exc:  # noqa: BLE001
        _stage("SCHEMA", f"Could not connect to ClickHouse: {exc}")
        return 2

    schema_results = client.create_schema()
    ok_tables = [t for t, r in schema_results.items() if r.ok]
    bad_tables = {t: r.error for t, r in schema_results.items() if not r.ok}
    _stage("SCHEMA", f"Ensured tables: {', '.join(sorted(ok_tables)) or '(none)'}")
    if bad_tables:
        _stage("SCHEMA", f"Tables that failed to create: {bad_tables}")
        return 2

    # 3. Goal initiation (all-or-nothing) -----------------------------------
    goal = Goal(
        target_metric=0.2,                                   # reply-rate target in [0,1]
        deadline=datetime.now() + timedelta(minutes=10),     # valid: now+1min..now+365d
        email_budget=8,                                      # 1..1000
    )
    loop = ControlLoop(client)
    start_result = loop.start(DEMO_THESIS, goal)
    if not start_result.ok:
        _stage(
            "GOAL-INIT",
            f"Goal initiation failed ({start_result.error_kind}): {start_result.message}",
        )
        return 2
    run_handle = start_result.run_handle
    assert run_handle is not None
    _stage(
        "GOAL-INIT",
        f"Run initiated run_id={run_handle.run_id} — initial LoopState persisted "
        f"(target_metric={goal.target_metric}, budget={goal.email_budget}, "
        f"deadline={goal.deadline:%Y-%m-%d %H:%M:%S}).",
    )

    # 4. Scanner ------------------------------------------------------------
    # HN always; GitHub auto-added when Airbyte credentials are present.
    scanner = Scanner(client, config=config)
    source_names = [getattr(s, "name", s.__class__.__name__) for s in scanner.sources]
    _stage("SCANNER", f"Configured signal sources: {', '.join(source_names)}")

    plan = TickPlan(
        tick_index=run_handle.state.tick_index,
        thesis_breadth=run_handle.state.thesis_breadth,
        email_angle=run_handle.state.email_angle,
        send_volume=run_handle.state.send_volume,
        sources=["github", "hackernews"],
        qualification_threshold=DEMO_THRESHOLD,
    )
    _stage("SCANNER", f"Scanning enabled sources for plan.sources={plan.sources} (last 90 days)...")
    scan_result = scanner.scan(plan)

    # Per-source discovery counts (honest reporting per source).
    per_source: dict[str, int] = {}
    for cand in scan_result.candidates:
        per_source[cand.source] = per_source.get(cand.source, 0) + 1
    for name in source_names:
        per_source.setdefault(name, 0)

    _stage(
        "SCANNER",
        f"Discovered {len(scan_result.candidates)} candidate(s): "
        + ", ".join(f"{src}={cnt}" for src, cnt in sorted(per_source.items())),
    )
    _stage(
        "SCANNER",
        f"Persisted to companies: {scan_result.inserted} inserted, "
        f"{scan_result.upserted} upserted, {scan_result.persist_errors} persist error(s).",
    )
    for failure in scan_result.failures:
        _stage(
            "SCANNER",
            f"Source '{failure.source}' failed after {failure.attempts} attempt(s): {failure.error}",
        )
    if per_source.get("github", 0) == 0:
        _stage(
            "SCANNER",
            "GitHub returned 0 candidates — acceptable for this checkpoint "
            "(Airbyte tier may not expose repo records); the HN path proves the loop.",
        )

    if not scan_result.candidates:
        _stage("SCANNER", "No candidates discovered from any source — cannot exercise the Qualifier.")
        client.close()
        return 1

    # 5. Qualifier ----------------------------------------------------------
    scorer = select_scorer(config)
    _stage("QUALIFIER", f"Active scorer: {scorer.__class__.__name__}")

    to_qualify = scan_result.candidates[:MAX_CANDIDATES_TO_QUALIFY]
    _stage(
        "QUALIFIER",
        f"Scoring {len(to_qualify)} of {len(scan_result.candidates)} candidate(s) "
        f"(threshold={DEMO_THRESHOLD}); explanations via TrueFoundry...",
    )
    qualifier = Qualifier(client, config=config)
    qualify_result = qualifier.qualify(to_qualify, DEMO_THESIS, scorer, DEMO_THRESHOLD)

    for q in qualify_result.all:
        snippet = (q.fit_explanation or "").replace("\n", " ").strip()
        if len(snippet) > 140:
            snippet = snippet[:140].rstrip() + "..."
        forwarded = "FORWARDED" if q.fit_score >= DEMO_THRESHOLD else "withheld "
        _stage(
            "QUALIFIER",
            f"[{forwarded}] {q.source}:{q.name[:48]!r} score={q.fit_score:3d} "
            f"explanation=\"{snippet}\"",
        )

    _stage(
        "QUALIFIER",
        f"Pass complete: {len(qualify_result.all)} scored, "
        f"{len(qualify_result.qualified)} forwarded (>= {DEMO_THRESHOLD}), "
        f"{qualify_result.fallback_count} heuristic fallback(s).",
    )

    # 6. Persistence confirmation -------------------------------------------
    scored_read = client.query(
        "SELECT count() FROM (SELECT source_unique_id, argMax(fit_score, version) AS fs "
        "FROM companies GROUP BY source, source_unique_id) WHERE fs >= 0"
    )
    if scored_read.ok and scored_read.rows:
        _stage("PERSIST", f"companies rows with a fit_score persisted: {scored_read.rows[0][0]}")

    sample_read = client.query(
        "SELECT source, name, argMax(fit_score, version) AS fs, "
        "argMax(fit_explanation, version) AS fe "
        "FROM companies GROUP BY source, name, source_unique_id "
        "HAVING fs >= 0 ORDER BY fs DESC LIMIT 5"
    )
    if sample_read.ok and sample_read.rows:
        _stage("PERSIST", "Top persisted scored companies (read back from ClickHouse):")
        for source, name, fs, fe in sample_read.rows:
            expl = (fe or "").replace("\n", " ").strip()
            if len(expl) > 100:
                expl = expl[:100].rstrip() + "..."
            _stage("PERSIST", f"    {source:11s} score={int(fs):3d} {str(name)[:40]!r} :: \"{expl}\"")

    _hr()
    _stage("DONE", "Checkpoint 7 partial loop completed: goal init -> scanner -> qualifier.")
    _hr()

    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
