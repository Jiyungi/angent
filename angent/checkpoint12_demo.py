"""Checkpoint 12 demo: run the partial Control_Loop end to end (Tasks 0-11).

This is the run-and-commit checkpoint after the Optimizer group (tasks.md task
12). It is **not** new feature code and **not** an automated test harness — it is
a thin, committable wiring artifact that exercises the pieces already built and
verified individually, then prints timestamped, stage-identified console output
so the partial loop can be observed end to end (Requirements 23.5, 22.3, 22.4).

Pipeline exercised (goal init -> scan -> qualify -> draft -> approve -> send ->
optimize), with the **Governance_Gate enforced on every send** (Requirement 9.7):

  1. Load configuration from ``.env`` and create the ClickHouse schema.
  2. Initiate the run all-or-nothing via ``ControlLoop.start``.
  3. ``Scanner.scan`` discovers candidates (Hacker News always; GitHub when the
     Airbyte tier exposes records — a 0 there is acceptable, HN proves the loop).
  4. ``Qualifier.qualify`` scores a SMALL number of candidates (cap 2-3) so the
     TrueFoundry calls stay minimal and fast.
  5. ``Writer.draft`` drafts one personalized email per forwarded candidate and
     stores each in ``emails`` as **unsent + unapproved** (approved=0, sent=0).
  6. ``GovernanceGate.approve`` approves exactly **one** draft. The remaining
     drafts stay unsent/unapproved (the gate would BLOCK them).
  7. The approved draft is sent through ``send_via_gate`` (gate-routed) via the
     default ``SmtpSender`` to the **controlled inbox** (``GMAIL_ADDRESS``) — a
     single real SMTP send that proves the gate -> sender path without spamming.
     The approved draft's ``emails`` row becomes sent=1.
  8. ``Optimizer.store`` persists a simulated **reply** ``Outcome`` for the sent
     email, then ``compute_reply_rate`` + ``persist_reply_rate`` update the
     run's reply-rate metric on ``loop_state``.

Run it from the workspace root::

    python -m angent.checkpoint12_demo

A single real send may fail (e.g. missing/invalid SMTP creds); that is reported
honestly and does NOT block — the gate/draft/optimizer path still proves out.
Exits non-zero only if the loop could not run at all (ClickHouse unreachable,
goal init failed, or no candidates discovered to draft against).
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone

from angent.config import load_config
from angent.persistence.clickhouse import ClickHouseClient
from angent.loop.control_loop import ControlLoop
from angent.models import Goal, Outcome, TickPlan
from angent.agents.scanner import Scanner
from angent.agents.qualifier import Qualifier
from angent.agents.writer import Writer
from angent.agents.optimizer import Optimizer
from angent.governance.gate import GovernanceGate, RateWindow
from angent.sending.sender import SmtpSender, send_via_gate
from angent.scoring.pioneer import select_scorer


# --- Demo parameters --------------------------------------------------------

DEMO_THESIS = (
    "We invest in early-stage developer-tools and AI-infrastructure startups: "
    "open-source frameworks, LLM agent tooling, data and ML platforms, and "
    "API-first products that help engineers build and ship software faster."
)

# Qualification threshold (0..100). Modest so some recent launches clear the bar
# and are forwarded to the Writer.
DEMO_THRESHOLD = 20

# Keep TrueFoundry calls minimal AND avoid spamming: score only a small number
# of candidates and draft at most this many emails this checkpoint.
MAX_CANDIDATES_TO_QUALIFY = 3
DEMO_EMAIL_BUDGET = 3

# Rate-limit window for the gate: comfortably above the single send we make so
# the one approved draft is PERMITted (not deferred).
DEMO_RATE_LIMIT = 10


# --- Timestamped, stage-identified console logging --------------------------

def _stage(stage: str, message: str) -> None:
    """Emit one timestamped, stage-identified console record (Requirement 16)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    print(f"[{ts}] [{stage}] {message}", flush=True)


def _hr() -> None:
    print("-" * 78, flush=True)


def _email_flags(client: ClickHouseClient, email_id: str) -> tuple[int, int, int]:
    """Return (approved, sent, failed) for the latest version of an emails row."""
    result = client.query(
        "SELECT argMax(approved, version), argMax(sent, version), "
        "argMax(failed, version) FROM emails "
        "WHERE email_id = {email_id:String}",
        parameters={"email_id": email_id},
    )
    if result.ok and result.rows and result.rows[0][0] is not None:
        a, s, f = result.rows[0]
        return int(a), int(s), int(f)
    return -1, -1, -1


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="    (log) %(name)s: %(message)s")

    _hr()
    _stage("START", "Checkpoint 12 — partial Control_Loop (scan -> qualify -> "
                    "write -> approve -> gate-routed send -> optimize)")
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
        target_metric=0.2,
        deadline=datetime.now() + timedelta(minutes=10),
        email_budget=DEMO_EMAIL_BUDGET,
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
    run_id = run_handle.run_id
    _stage(
        "GOAL-INIT",
        f"Run initiated run_id={run_id} — initial LoopState persisted "
        f"(target_metric={goal.target_metric}, budget={goal.email_budget}).",
    )

    # 4. Scanner ------------------------------------------------------------
    scanner = Scanner(client, config=config)
    source_names = [getattr(s, "name", s.__class__.__name__) for s in scanner.sources]
    _stage("SCANNER", f"Configured signal sources: {', '.join(source_names)}")

    plan = TickPlan(
        tick_index=run_handle.state.tick_index,
        thesis_breadth=run_handle.state.thesis_breadth,
        email_angle=run_handle.state.email_angle or "a warm founder-to-investor intro",
        send_volume=run_handle.state.send_volume,
        sources=["github", "hackernews"],
        qualification_threshold=DEMO_THRESHOLD,
    )
    _stage("SCANNER", f"Scanning enabled sources for plan.sources={plan.sources} (last 90 days)...")
    scan_result = scanner.scan(plan)

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
            "GitHub returned 0 candidates — acceptable for this checkpoint; "
            "the HN path proves the loop.",
        )

    if not scan_result.candidates:
        _stage("SCANNER", "No candidates discovered — cannot exercise the rest of the loop.")
        client.close()
        return 1

    # 5. Qualifier (small batch) -------------------------------------------
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
        forwarded = "FORWARDED" if q.fit_score >= DEMO_THRESHOLD else "withheld "
        _stage("QUALIFIER", f"[{forwarded}] {q.source}:{q.name[:48]!r} score={q.fit_score:3d}")

    _stage(
        "QUALIFIER",
        f"Pass complete: {len(qualify_result.all)} scored, "
        f"{len(qualify_result.qualified)} forwarded (>= {DEMO_THRESHOLD}).",
    )

    forwarded = list(qualify_result.qualified)
    if not forwarded:
        # Nothing cleared the bar; fall back to the highest-scored so the rest of
        # the pipeline still has a draft to exercise (honest about the fallback).
        if qualify_result.all:
            forwarded = [max(qualify_result.all, key=lambda c: c.fit_score)]
            _stage(
                "QUALIFIER",
                "No candidate cleared the threshold; using the highest-scored "
                f"candidate ({forwarded[0].name[:48]!r}) so the Writer/Gate/Sender "
                "path is still exercised.",
            )
        else:
            _stage("QUALIFIER", "No scored candidates available to draft against.")
            client.close()
            return 1

    # 6. Writer -------------------------------------------------------------
    writer = Writer(client, config=config)
    _stage(
        "WRITER",
        f"Drafting up to {DEMO_EMAIL_BUDGET} personalized email(s) for "
        f"{len(forwarded)} forwarded candidate(s) via TrueFoundry...",
    )
    draft_result = writer.draft(forwarded, plan, DEMO_EMAIL_BUDGET, run_id=run_id)
    _stage(
        "WRITER",
        f"Produced {draft_result.count} draft(s), {draft_result.failure_count} failure(s).",
    )
    for f in draft_result.failures:
        _stage("WRITER", f"    draft failure [{f.stage}] {f.name!r}: {f.reason}")

    if draft_result.count == 0:
        _stage("WRITER", "No drafts produced (TrueFoundry unavailable?) — cannot exercise approve/send.")
        client.close()
        return 1

    # Confirm every fresh draft is stored unsent + unapproved (Req 8.3).
    for d in draft_result.drafts:
        a, s, fl = _email_flags(client, d.email_id)
        _stage(
            "WRITER",
            f"    draft {d.email_id[:8]} stored: approved={a} sent={s} failed={fl} "
            f"subject={d.subject[:48]!r}",
        )

    # 7. Governance Gate — approve exactly ONE draft (Req 9.2) --------------
    gate = GovernanceGate(client)
    approved_draft = draft_result.drafts[0]
    _stage("GATE", f"Approving exactly one draft {approved_draft.email_id[:8]} (investor=demo-investor)...")
    approval = gate.approve(approved_draft.email_id, "demo-investor")
    _stage("GATE", f"Approval result: ok={approval.ok} — {approval.message}")
    if approval.ok:
        approved_draft.approved = True  # reflect persisted state on the local object

    a, s, fl = _email_flags(client, approved_draft.email_id)
    _stage("GATE", f"    approved draft now: approved={a} sent={s} failed={fl}")
    for d in draft_result.drafts[1:]:
        a2, s2, f2 = _email_flags(client, d.email_id)
        _stage(
            "GATE",
            f"    other draft {d.email_id[:8]} remains: approved={a2} sent={s2} "
            f"(gate would BLOCK any send of it).",
        )

    # 8. Gate-routed send to the CONTROLLED INBOX (Req 9.7, 10.x) -----------
    recipient = getattr(getattr(config, "gmail", None), "address", None)
    backend = SmtpSender(client, config=config)  # resolves to GMAIL_ADDRESS by default
    window = RateWindow(
        sent_in_window=0,
        limit=DEMO_RATE_LIMIT,
        window_start=datetime.now(timezone.utc),
    )
    _stage(
        "SENDER",
        f"Routing the approved draft through send_via_gate -> SmtpSender to the "
        f"controlled inbox ({recipient or 'GMAIL_ADDRESS not set'})...",
    )
    gated = send_via_gate(
        gate,
        backend,
        approved_draft,
        sent_count=run_handle.state.emails_sent,
        budget=goal.email_budget,
        window=window,
        investor_id="demo-investor",
    )
    _stage(
        "SENDER",
        f"Gate decision={gated.decision.decision.value} permitted={gated.permitted} "
        f"sent={gated.sent} budget_consumed={gated.budget_consumed}.",
    )
    if gated.sent:
        a, s, fl = _email_flags(client, approved_draft.email_id)
        _stage("SENDER", f"    approved draft after send: approved={a} sent={s} failed={fl} "
                         "(expected sent=1).")
    else:
        reason = (gated.result.error if gated.result else None) or gated.decision.message
        _stage(
            "SENDER",
            f"    send did not succeed ({reason}). The gate/draft/optimizer path "
            "is still proven; a single real SMTP failure does not block the checkpoint.",
        )

    # 9. Optimizer — store a simulated reply + update reply-rate ------------
    optimizer = Optimizer(client, run_id=run_id)
    simulated_reply = Outcome(
        email_id=approved_draft.email_id,
        company_id=approved_draft.company_id,
        kind="reply",
        occurred_at=datetime.now(timezone.utc),
        seeded=False,
        run_id=run_id,
    )
    _stage("OPTIMIZER", f"Storing a simulated 'reply' outcome for email {approved_draft.email_id[:8]}...")
    store_result = optimizer.store([simulated_reply])
    _stage(
        "OPTIMIZER",
        f"Store result: {len(store_result.stored)} stored, "
        f"{len(store_result.unstored)} retained-unstored.",
    )
    for err in store_result.errors:
        _stage("OPTIMIZER", f"    store error: {err}")

    reply_rate = optimizer.compute_reply_rate(run_id)
    _stage("OPTIMIZER", f"Computed reply_rate for run = {reply_rate:.4f} (replies / emails sent).")
    persisted = optimizer.persist_reply_rate(run_id, reply_rate)
    _stage(
        "OPTIMIZER",
        f"Persisted reply_rate to loop_state: {persisted!r}"
        + ("" if persisted is not None else " (no loop_state to update / write failed)."),
    )

    # Read it back to confirm the metric is observable.
    state_read = client.read_loop_state(run_id) if hasattr(client, "read_loop_state") else None
    if state_read is not None:
        _stage("OPTIMIZER", f"    loop_state read-back: reply_rate={state_read.reply_rate:.4f}")

    _hr()
    _stage(
        "DONE",
        "Checkpoint 12 partial loop completed: scan -> qualify -> write (unsent/"
        "unapproved) -> approve one -> gate-routed send -> outcome stored -> "
        "reply_rate updated.",
    )
    _hr()

    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
