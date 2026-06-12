# Implementation Plan: Angent

## Overview

This plan builds the Angent self-improving deal-sourcing agent incrementally as a lean, single-Builder effort, Python core first (the critical path), with each sponsor integration layered behind a stable interface or feature flag so no single integration can block the loop. There is no separate automated test harness (Requirement 23): each change is verified by running the demo Control_Loop end to end and watching the backend-log/console output (plus the live cited.md and x402 demo), then committed and pushed to `main` at https://github.com/Jiyungi/angent (Requirement 22). The order is: repository/version-control setup → foundation and data models → ClickHouse blackboard → goal validation → scorer interface → scanner → qualifier → writer → governance → sender → optimizer → control loop + planner → observability → logging → Guild wrapper → UI surfaces → demo wiring → Publisher → Payment Gate, with run-and-commit checkpoints at natural breaks.

## Tasks

- [x] 0. Repository and version-control setup
  - [x] 0.1 Initialize Git and connect the GitHub remote
    - Ensure the workspace root is a Git repository on branch `main` (run `git init` if needed and `git branch -M main`)
    - Add the remote `origin = https://github.com/Jiyungi/angent` (or update it if it already points elsewhere) and verify connectivity to the remote
    - Stage the currently tracked working files (excluding anything that will be ignored in 0.2) and make the initial commit on `main` with a descriptive message, then push with `git push -u origin main`
    - _Requirements: 22.1, 22.2, 22.3_

  - [x] 0.2 Author `.gitignore` to keep secrets and artifacts untracked
    - Create/overwrite `.gitignore` so `.env`, `node_modules/`, the Python virtualenv (e.g. `.venv/`/`venv/`), build artifacts (`.next/`, `__pycache__/`, `*.pyc`), and local Deal_Memo fallback files are untracked
    - Confirm `.env.template` IS tracked (placeholder keys committed) and that `.env` (real secrets) is excluded; run `git status`/`git check-ignore` to verify
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 22.6, 22_

  - [x] 0.3 Remove non-runtime checking/probe scripts
    - Delete `test_airbyte.py` and `check_connectors.py` (their confirmed Airbyte OAuth + Agents-API pattern is folded into the Scanner instead of living as standalone scripts)
    - Remove any generated test artifacts if present (`.pytest_cache/`, `.hypothesis/`, coverage output) and ensure they are ignored going forward
    - Commit each removal to `main` as its own descriptive commit and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 23.2, 23.4, 22_

- [x] 1. Set up project structure and core value objects
  - [x] 1.1 Create Python project skeleton and environment configuration
    - Create the `angent/` package layout (agents, persistence, scoring, sending, governance, loop, observability modules)
    - Add dependencies (`clickhouse-connect`, `openai`, `langfuse`, `requests`) and a `pyproject.toml`/`requirements.txt`
    - Implement an env loader (`angent/config.py`) reading `TRUEFOUNDRY_API_KEY`, `TRUEFOUNDRY_BASE_URL`, ClickHouse host/port/user/password/database, Airbyte and Pioneer credentials, and `GMAIL_ADDRESS`/`GMAIL_APP_PASSWORD` from `.env`
    - Also read the Publisher and Payment Gate credentials: `SENSO_API_KEY`, `SENSO_BASE_URL`, `X402_FACILITATOR_URL`, `X402_NETWORK`, `X402_PAY_TO_ADDRESS`, `X402_PRICE`, and `EVM_PRIVATE_KEY`
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 17.1, 18.3, 18.6, 20, 21, 22_

  - [x] 1.2 Define core value objects and enums
    - Implement `Goal`, `LoopState`, `Candidate`, `Qualified`, `Draft`, `Outcome`, `TickPlan`, `TickOutcome`, and the `StopReason` enum as dataclasses in `angent/models.py` matching the design (fields, types, and the `StopReason` values goal-met/deadline-reached/email-budget-exhausted)
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 1.1, 2.3, 8.1, 12.1, 12.2, 22_

- [x] 2. Implement the ClickHouse blackboard persistence layer
  - [x] 2.1 Implement ClickHouse client wrapper and schema creation
    - Create a connection wrapper (`angent/persistence/clickhouse.py`) over the ClickHouse Cloud HTTPS interface (host/port 8443/user/password/database from config) using `clickhouse-connect`
    - Implement a bounded-retry helper (up to 3 attempts) that retains the unwritten payload in memory and returns an error indication on total failure (inputs: SQL/params; outputs: rows or error)
    - Create the `companies`, `emails`, `outcomes`, `loop_state`, `publications`, and `fetches` tables using the `ReplacingMergeTree(version)`/`MergeTree` engines and column definitions from the design (the `publications` and `fetches` tables back the Publisher and Payment Gate respectively)
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 12.1, 12.2, 12.6, 20.4, 21.1, 22_

  - [x] 2.3 Implement loop-state read/write with latest-version-wins semantics
    - Add `write_loop_state(state)` and `read_loop_state(run_id)` to the persistence layer, writing to `loop_state` with an incrementing `version`/`updated_at` so the latest version wins on the `ReplacingMergeTree`
    - Persist loop-state updates within 2 seconds and read back the most recent version (`FINAL`/`argMax` on `version`) so any subsequent read by another agent returns the updated state
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 12.3, 22_

- [x] 3. Implement goal validation and loop initiation
  - [x] 3.1 Implement the `validate_goal` pure validator
    - Implement `validate_goal(thesis, goal_input)` in `angent/loop/validation.py` returning a `ValidationResult{ok, offending_field}`
    - Validate thesis length `[1,5000]`, presence of `target_metric`/`deadline`/`email_budget`, `email_budget` in `[1,1000]`, and `deadline` between now+1min and now+365days, returning the specific offending field on rejection
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 1.1, 1.3, 1.4, 22_

  - [x] 3.3 Implement all-or-nothing goal initiation
    - Implement `start(thesis, goal)` on `ControlLoop` (`angent/loop/control_loop.py`) that validates the goal, then on success persists goal + start time + initial `LoopState` to ClickHouse within 2 seconds and before the first Tick
    - If persistence fails, do not start the loop, leave no partial record (delete any partial write), and return an init-incomplete error indication
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 1.2, 1.5, 1.6, 22_

- [x] 4. Implement the pluggable Scorer interface
  - [x] 4.1 Define the `Scorer` protocol and the `HeuristicScorer` default
    - Define the `Scorer` protocol (`score(candidate, thesis) -> int` clamped to `[0,100]`, `learn(outcomes) -> LearnResult`) in `angent/scoring/scorer.py`
    - Implement `HeuristicScorer` as a keyword/recency/signal-weight blend clamped to `[0,100]`, plus a `learn` method that adjusts the blend weights from outcomes
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 7.1, 7.2, 5.1, 22_

  - [x] 4.2 Implement `PioneerScorer` and `select_scorer`
    - Implement `PioneerScorer` (Fastino adaptive-inference) behind the identical `Scorer` interface in `angent/scoring/pioneer.py`
    - Implement `select_scorer(env)` that returns `PioneerScorer` when credentials are present and reachable, else `HeuristicScorer`, recording an indication that it is operating in Heuristic mode when Pioneer is absent
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 7.2, 7.4, 18.4, 22_

- [x] 5. Implement the Scanner and signal sources
  - [x] 5.1 Implement the `SignalSource` protocol and the Hacker News Algolia source
    - Define the `SignalSource` protocol (`name`, `fetch(plan, since) -> list[Candidate]`) in `angent/agents/scanner.py`
    - Implement the Hacker News source against the Algolia API to retrieve Show HN / Launch HN signals posted within the last 90 days with a 30-second timeout, mapping each hit to a `Candidate` with `source="hackernews"`, source-specific unique id, name, url, and signals (points, comments)
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 4.2, 22_

  - [x] 5.2 Implement the GitHub source via the Airbyte_Agent
    - Implement the GitHub source using the confirmed two-step OAuth flow: `POST https://api.airbyte.com/v1/applications/token` (client_id/client_secret/grant_type=client_credentials) for a bearer token, then the Agents API at `https://api.airbyte.ai/api/v1/integrations/connectors` with `Authorization: Bearer` plus `X-Organization-Id`
    - Retrieve repo/stargazer/commit signals within the last 90 days with a 30-second timeout, mapping each to a `Candidate` with `source="github"`
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 4.1, 18.1, 22_

  - [x] 5.3 Implement the Hugging Face Hub stretch source
    - Behind a feature flag, implement a Hugging Face Hub source that retrieves AI orgs/models created within the last 90 days with a 30-second timeout, mapping each to a `Candidate` with `source="huggingface"`
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 4.6, 22_

  - [x] 5.4 Implement the Scanner with dedup/upsert and per-source retry orchestration
    - Implement `Scanner.scan(plan)` that runs each enabled `SignalSource`, inserts new candidates into the `companies` table with source attribution, and upserts matches on `(source, source_unique_id)` preserving the original `created_at` (bump `version`/`updated_at` on the `ReplacingMergeTree`)
    - Retry each source up to 3 times; on total failure record a failure entry, continue with remaining sources, and still complete the Tick
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 4.3, 4.4, 4.5, 22_

- [x] 6. Implement the Qualifier
  - [x] 6.1 Implement scoring with TrueFoundry explanation and placeholder fallback
    - Implement `Qualifier.qualify(...)` (`angent/agents/qualifier.py`) producing a `[0,100]` score per candidate within 30s and a 50-1000 char explanation referencing the thesis via the TrueFoundry_Gateway (OpenAI SDK against `TRUEFOUNDRY_BASE_URL`)
    - On gateway timeout/error at 30s store the score with an "explanation unavailable" placeholder and retain the record; persist score + explanation to `companies` with bounded retry (3 attempts), never discarding the computed values on persistence failure
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 18.3, 22_

  - [x] 6.2 Implement per-candidate Pioneer fallback and threshold forwarding
    - Use the active scorer: when Pioneer returns within its 10s timeout use its score, else fall back to the `HeuristicScorer` for that candidate only, record the fallback, and continue the remaining candidates without aborting the Tick
    - Pass candidates with score ≥ the configured qualification threshold (integer `[0,100]`) to the Writer and withhold those below
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 5.6, 5.7, 5.8, 5.9, 7.3, 18.5, 22_

- [x] 7. Run-and-commit checkpoint (after the Qualifier group)
  - Run the partial Control_Loop built so far (goal init → scanner → qualifier) end to end against the demo config and confirm the expected backend-log/console output: at least one GitHub and one HN candidate discovered, scored, and persisted to ClickHouse with explanations
  - Confirm all changes so far are committed and pushed to `main` at https://github.com/Jiyungi/angent
  - _Requirements: 23.5, 22.3, 22.4_

- [x] 8. Implement the Writer
  - [x] 8.1 Implement budget-respecting personalized drafting
    - Implement `Writer.draft(qualified, plan, remaining_budget)` (`angent/agents/writer.py`) drafting exactly one email per qualified candidate up to the remaining budget via TrueFoundry, incorporating the candidate's signals and the plan's email angle
    - Store each draft in the `emails` table as unsent and unapproved with bounded retry (3 attempts); if the gateway fails/times out (30s) for a candidate, skip it without consuming budget, retain completed drafts, and record the drafting failure
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 18.3, 22_

- [x] 9. Implement the Governance Gate
  - [x] 9.1 Implement approval and edit-invalidation
    - Implement `GovernanceGate.approve(draft_id, investor_id)` to mark a specific draft approved, and `on_draft_modified(draft_id)` to revert an approved draft to unapproved (setting `approved=0`) so a modified draft requires fresh approval before sending
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 9.1, 9.2, 9.3, 22_

  - [x] 9.2 Implement the `authorize_send` pure decision function
    - Implement `authorize_send(draft, sent_count, budget, window) -> SendDecision` returning PERMIT/BLOCK/DEFER: block unapproved drafts, block (`email-budget`) when `sent_count + 1 > budget`, defer (`rate-limit`) while sends in the current window ≥ the configured limit, and surface a pending indication when deferred beyond 3600s
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 9.4, 9.5, 9.6, 3.5, 22_

- [x] 10. Implement the Sender interface
  - [x] 10.1 Implement the `Sender` protocol and `SmtpSender` default
    - Define the `Sender` protocol (`send(draft) -> SendResult{ok, sent_at, error}`) in `angent/sending/sender.py`
    - Implement `SmtpSender` wrapping `angent/email_sender.py` as the default backend; on success mark the `emails` record `sent` with the returned timestamp
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 10.1, 10.2, 10.3, 22_

  - [x] 10.2 Implement `GmailAgentSender`, backend selection, and gate-routed sending
    - Implement `GmailAgentSender` (Airbyte Gmail alternate, only when the tier is unlocked) behind the same `Sender` interface, plus backend selection defaulting to `SmtpSender`
    - Route every send through the `GovernanceGate` and reject any send the gate did not permit; treat a 30s timeout as failure, leave failed/timed-out drafts eligible for retry without decrementing budget, and mark the `emails` record `failed` after 3 consecutive failures
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 10.4, 10.5, 10.6, 10.7, 9.7, 18.2, 22_

- [ ] 11. Implement the Optimizer and reply-rate analytics
  - [x] 11.1 Implement outcome collection, storage, and learning feed
    - Implement `Optimizer.collect()`/`store(outcomes)`/`feed(scorer, outcomes)` (`angent/agents/optimizer.py`): store each reply/open outcome against its email + company in the `outcomes` table within 5s with up to 3 retries (retain unstored outcomes on failure)
    - Feed newly stored outcomes to the active scorer's `learn` before the next Tick; on Pioneer update failure keep the previous model, continue scoring with it, and record an error indication
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 22_

  - [x] 11.2 Implement reply-rate computation (scalar and 24-hour buckets)
    - Implement `compute_reply_rate(run_id)` computing `replies / emails_sent` for the run (0 when none sent) from the `emails`/`outcomes` tables, persist it per Tick to `loop_state`, and expose per-24-hour-interval aggregation across the run duration
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 6.7, 6.8, 12.4, 22_

- [-] 12. Run-and-commit checkpoint (after the Optimizer group)
  - Run the partial Control_Loop built so far (scanner → qualifier → writer → sender → optimizer, with the Governance_Gate enforced on every send) end to end against the demo config and confirm the expected backend-log/console output: drafts created unsent/unapproved, an approved draft sent to the controlled inbox, outcomes stored, and the reply-rate metric updating per Tick
  - Confirm all changes so far are committed and pushed to `main` at https://github.com/Jiyungi/angent
  - _Requirements: 23.5, 22.3, 22.4_

- [ ] 13. Implement the Planner and Control Loop
  - [ ] 13.1 Implement `evaluate_termination` as a pure function
    - Implement `evaluate_termination(state, now) -> Optional[StopReason]` returning `None` to continue, otherwise the single highest-priority `StopReason` in order goal-met > deadline-reached > email-budget-exhausted
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.6, 2.2, 22_

  - [ ] 13.3 Implement the Planner `plan`/`reflect` with stall adaptation
    - Implement `Planner.plan(state) -> TickPlan` producing a per-Tick plan from state + goal, and `Planner.reflect(state, outcomes) -> LoopState` incorporating the observed reply-rate into the next plan
    - When target_metric improvement stays below the configured threshold across N consecutive Ticks (from `metric_history`), change at least one of thesis breadth, email angle, or send volume
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 2.3, 2.7, 6.7, 22_

  - [ ] 13.5 Implement `ControlLoop.run_tick` orchestration and termination/persistence
    - Implement `run_tick(state)` that calls `evaluate_termination` first each Tick, then runs Scanner→Qualifier→Writer→Sender→Optimizer in that order using the plan
    - Contain stage failure (stop remaining stages, record the failed stage, preserve prior state, proceed to next Tick), update loop state + reply-rate in ClickHouse after each Tick, run at most one Tick at a time, and on stop persist final state + stop reason with up to 3 retries (retaining them in memory on total failure)
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 2.1, 2.4, 2.5, 2.6, 2.8, 3.5, 3.7, 3.8, 11.1, 11.2, 22_

- [ ] 14. Implement Langfuse observability
  - [ ] 14.1 Implement the tracing wrapper with disabled fallback
    - Implement a tracing wrapper (`angent/observability/tracing.py`) recording a trace per agent step (step id, input, output, start/end timestamps) within 2s and each TrueFoundry call as a linked span (prompt, response, token count)
    - If Langfuse is unconfigured at startup run untraced with a log entry; on trace/span write failure after 3 retries continue the step uninterrupted and log the failure
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 18.9, 22_

- [ ] 15. Implement backend logging for front-end-optional operation
  - [ ] 15.1 Implement the per-stage log/console emitter
    - Implement a logging helper (`angent/observability/logging.py`) that emits a timestamped, stage-identified record for progress, qualifications, drafts, sends, and reply-rate trend within 2s of stage completion
    - On a failed record write continue remaining stages and emit an error record naming the failed stage; on any sponsor-integration failure preserve shared state and surface an error naming the technology
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 16.1, 16.2, 16.3, 16.4, 18.10, 22_

- [ ] 16. Implement the Guild orchestrator wrapper (TypeScript)
  - [ ] 16.1 Implement the Guild orchestrator and 5-second unavailability fallback
    - Implement the Guild orchestrator (TypeScript) that orchestrates the loop by calling the Python backend over HTTP and routes every send through the Governance_Gate
    - Classify Guild as unavailable when it does not respond within 5s so the Python core self-drives and enforces the gate
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 11.3, 11.4, 11.5, 11.6, 18.7, 22_

  - [ ] 16.2 Implement the Python HTTP endpoint for Guild orchestration
    - Expose tick-advance and send-authorization HTTP endpoints (`angent/loop/server.py`) that the Guild orchestrator calls, backed by the same `GovernanceGate` so governance is identical whether self-driven or Guild-orchestrated
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 11.3, 11.4, 18.7, 22_

- [ ] 17. Implement the UI surfaces
  - [ ] 17.1 Implement the OpenUI_Surface thesis chat and deep-dive views
    - Generate the thesis-refinement chat and on-demand deep-dive at runtime via OpenUI Lang within a 5s budget each, reusing the `genui-chat-app` pipeline (`library.prompt()` → `/api/chat` → `<Renderer/>`)
    - On failure/timeout retain the last good rendered state and show an error; ensure every element comes from the generation step and each view includes at least one input/action element
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.5, 14.6, 18.8, 22_

  - [ ] 17.2 Implement the React_Shell plain components
    - Build qualified-company cards, the drafted-email preview, and the loop-status display as plain React components with no OpenUI Lang markup
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 15.1, 22_

  - [ ] 17.3 Implement the OpenUI / React_Shell separation and Impeccable guard
    - Maintain unambiguous classification of each component as either OpenUI-generated or plain React, and ensure an Impeccable polish pass (`npx impeccable detect`) changes only plain React components, leaving OpenUI-generated components unchanged and continuing the pass when it encounters one
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 15.2, 15.3, 15.4, 15.5, 22_

- [ ] 18. Wire components together and build the demo run
  - [ ] 18.1 Wire all agents, gate, persistence, and tracing into a runnable entrypoint
    - Connect goal initiation → control loop → six agents → governance gate → sender → optimizer → ClickHouse + Langfuse + logging into a single runnable Python core entrypoint (`angent/main.py`) with no orphaned components
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 11.1, 12.1, 12.2, 12.3, 16.3, 22_

  - [ ] 18.2 Implement the demo configuration and seed loader
    - Load seeded historical outcomes (`seeded=1`) into the `outcomes` table before the first Tick and configure the demo Goal (target in `[0,1]`, 120s deadline, 8-email budget, 3-10s Tick interval) in a demo config module/script
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 12.5, 19.1, 19.9, 22_

- [ ] 20. Implement the Publisher (cited.md via Senso)
  - [ ] 20.1 Implement the Deal_Memo markdown serializer
    - Implement `Publisher.serialize(companies)` (`angent/publisher.py`) reading the qualified companies from ClickHouse (name, URL, source, fit_score, fit_explanation, signals) and emitting a Deal_Memo in markdown with one section per company plus a provenance citation per company linking to its real source URL (GitHub repo or Hacker News post)
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 20.1, 20.2, 22_

  - [ ] 20.2 Implement the Senso publish call with persistence and local fallback
    - Implement `Publisher.publish(deal_memo)` calling `senso engine publish` with `SENSO_API_KEY`/`SENSO_BASE_URL` (X-API-Key header against `https://apiv2.senso.ai/api/v1`), persisting the returned cited.md URL/slug/handle to the `publications` table on success
    - Fall back to a non-blocking local file write when Senso is unreachable (record `published_ok=0` and `local_path`, surface the error, never abort the loop)
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 20.3, 20.4, 20.5, 18.11, 18.13, 22_

  - [ ] 20.3 Keep the Deal_Memo separate from the OpenUI deep-dive
    - Ensure the Deal_Memo is markdown (never OpenUI Lang) produced by a separate code path, while the OpenUI deep-dive renders the same ClickHouse data as the in-app human surface
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 20.6, 22_

- [ ] 21. Implement the x402 Payment Gate
  - [ ] 21.1 Implement the seller paywall over the deal-memo fetch endpoint
    - Wrap the deal-memo fetch endpoint with `@x402/express` `paymentMiddleware` (composing `@x402/evm` `ExactEvmScheme` and `@x402/core` `HTTPFacilitatorClient`), reusing the `x402-test/server.mjs` pattern, configured from `X402_FACILITATOR_URL`/`X402_NETWORK`/`X402_PAY_TO_ADDRESS`/`X402_PRICE` with the "exact" USDC scheme
    - Return HTTP 402 without a valid payment; on a valid payment settle via the Facilitator and return the Deal_Memo content; record settled fetches to the `fetches` table; deny access without serving unpaid content when the Facilitator is unreachable
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 21.1, 21.2, 21.3, 21.4, 18.12, 18.14, 22_

  - [ ] 21.2 Implement/wire the buyer demo
    - Reuse `x402-test/buyer.mjs` (`@x402/fetch` + `viem` `privateKeyToAccount`) to pay automatically with `EVM_PRIVATE_KEY` (~0.001 testnet USDC) and then receive the Deal_Memo content
    - Commit this change to `main` with a descriptive message and push to https://github.com/Jiyungi/angent (Requirement 22)
    - _Requirements: 21.5, 22_

- [ ] 22. Final run-and-commit checkpoint (after the Payment Gate group)
  - Run the full demo Control_Loop end to end against the Requirement 19 config and confirm the expected backend-log/console output: at least one GitHub and one HN candidate, a budget block at the 9th send, a non-decreasing reply-rate across Ticks, self-termination within one Tick on goal-met/deadline/budget with a single prioritized stop reason, a Langfuse trace available within 10s of termination (when configured), a live cited.md Deal_Memo URL, and the x402 buyer receiving a 402, paying, then receiving the content
  - Confirm all changes are committed and pushed to `main` at https://github.com/Jiyungi/angent
  - _Requirements: 23.5, 22.3, 22.4_

## Notes

- There is NO separate automated test suite (no property-based, unit, integration, or smoke suites). Verification is by running the demo Control_Loop end to end and watching the timestamped, stage-identified backend-log/console records, plus the live cited.md publication and the x402 pay-per-fetch demo (Requirement 23).
- Commit-per-change discipline: every implementation sub-task ends in at least one Git commit on `main` pushed to https://github.com/Jiyungi/angent, staging only the files that change, with a descriptive message; larger sub-tasks yield several commits. If a push fails, resolve it and retry before continuing (Requirement 22).
- The run-and-commit checkpoints (tasks 7, 12, 22) replace the old "ensure all tests pass" checkpoints: each runs the loop (or the partial loop built so far) end to end, confirms the expected output, and confirms all changes are committed and pushed to `main`.
- The Python core is the critical path; the enhancement layers (OpenUI_Surface, React_Shell, Impeccable, Langfuse, Publisher, Payment_Gate) come after and are owned solely by the Builder, layered behind interfaces/flags so no single integration blocks the loop (Requirement 17).
- The x402 reference implementation already exists at `x402-test/` (`server.mjs` seller, `buyer.mjs` buyer) and is reused by the Payment Gate tasks; `angent/email_sender.py` and the `genui-chat-app/` pipeline are likewise retained runtime references.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["0.1"] },
    { "id": 1, "tasks": ["0.2", "0.3"] },
    { "id": 2, "tasks": ["1.1"] },
    { "id": 3, "tasks": ["1.2"] },
    { "id": 4, "tasks": ["2.1", "3.1", "4.1"] },
    { "id": 5, "tasks": ["2.3", "4.2", "5.1", "5.2", "5.3"] },
    { "id": 6, "tasks": ["3.3", "5.4"] },
    { "id": 7, "tasks": ["6.1", "6.2"] },
    { "id": 8, "tasks": ["7"] },
    { "id": 9, "tasks": ["8.1", "9.1", "9.2"] },
    { "id": 10, "tasks": ["10.1", "10.2"] },
    { "id": 11, "tasks": ["11.1", "11.2"] },
    { "id": 12, "tasks": ["12"] },
    { "id": 13, "tasks": ["13.1", "13.3"] },
    { "id": 14, "tasks": ["13.5"] },
    { "id": 15, "tasks": ["14.1", "15.1"] },
    { "id": 16, "tasks": ["16.1", "16.2"] },
    { "id": 17, "tasks": ["17.1", "17.2", "17.3"] },
    { "id": 18, "tasks": ["18.1"] },
    { "id": 19, "tasks": ["18.2"] },
    { "id": 20, "tasks": ["20.1"] },
    { "id": 21, "tasks": ["20.2", "20.3"] },
    { "id": 22, "tasks": ["21.1"] },
    { "id": 23, "tasks": ["21.2"] },
    { "id": 24, "tasks": ["22"] }
  ]
}
```
