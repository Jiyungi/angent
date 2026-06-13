# Angent

**A self-improving, goal-driven deal-sourcing agent for solo angel investors.**

Angent is an autonomous agent that finds emerging startups before they hit the mainstream radar, qualifies them against your investment thesis, drafts personalized outreach, sends it only after you approve, and learns from real reply data to get better over time.

It's built for solo angels who are priced out of enterprise tools like PitchBook and Harmonic. The differentiator isn't more cold email (a saturated, declining channel) — it's a **self-improving outreach loop** that keeps volume low and quality high while the scoring model improves from actual replies.

> Built for the Harness Engineering Hack (June 12, 2026). Challenge: ship an autonomous agent that does real work on the open web, grounded in real sources, using 3+ sponsor tools.

---

## How it works

Angent is a **goal-driven control loop**, not a one-pass pipeline. You give it a thesis and a structured goal `{ target_metric, deadline, email_budget }`. A Planner then re-decides what to do on every iteration (called a Tick) based on progress so far. The loop stops when the goal is met, the deadline passes, or the email budget is spent.

Six cooperating agents operate over a shared ClickHouse "blackboard":

| Agent | Responsibility |
|-------|----------------|
| **Planner** | Owns the goal; each Tick decides whether to widen the thesis, change email angle, send more, or stop. |
| **Scanner** | Pulls candidate startups from public signals (Hacker News, GitHub via Airbyte, optional Hugging Face), limited to the last 90 days. |
| **Qualifier** | Scores each candidate against the thesis (0–100) with a natural-language explanation. |
| **Writer** | Drafts personalized outreach emails for qualified candidates via the LLM gateway. |
| **Sender** | Sends approved emails through a pluggable backend (Gmail SMTP by default). |
| **Optimizer** | Collects reply/open outcomes and feeds them back into scoring to improve targeting. |

Every send must pass a **non-bypassable Governance Gate**: emails go out only after explicit human approval, within the email budget, and within rate limits. Editing an approved draft reverts it to unapproved, forcing fresh approval.

```
Investor sets thesis + goal
        │
        ▼
  Control Loop ──► ClickHouse (goal + state persisted before first Tick)
        │
        ▼  (each Tick)
  Scanner ─► Qualifier ─► Writer ─► Governance Gate ─► Sender ─► Optimizer
        │        │                      (human approval,            │
        │        │                       budget, rate limit)        │
        │        └─► Publisher ─► Deal Memo ─► cited.md (+ optional x402 paywall)
        │                                                           │
        └───────────────── reply outcomes feed back into scoring ◄─┘
        │
        ▼
  Stop when: goal met │ deadline reached │ budget exhausted
```

See [`flowchart.md`](flowchart.md) for the full diagram.

---

## Project structure

```
angent/
├── angent/                  # Python core (the agent system)
│   ├── main.py              # Single runnable entrypoint — wires everything together
│   ├── config.py            # Typed env/.env configuration loader
│   ├── models.py            # Core value objects (Goal, LoopState, Candidate, Draft, Outcome...)
│   ├── loop/                # Control loop, Planner, validation, server
│   ├── agents/              # Scanner, Qualifier, Writer, Optimizer
│   ├── scoring/             # Pioneer adaptive scorer + heuristic fallback
│   ├── governance/          # Non-bypassable human-approval Gate
│   ├── sending/             # Sender backends (Gmail SMTP / Airbyte)
│   ├── persistence/         # ClickHouse blackboard client + schema
│   ├── observability/       # Langfuse tracing + per-stage logging
│   └── publisher.py         # Deal Memo serializer + cited.md publishing
└── genui-chat-app/          # Next.js + OpenUI generative-UI frontend
    └── src/components/       # OpenUI surfaces + static React shell
```

---

## Sponsor integrations

Each integration is layered on with an explicit fallback, so no single one blocks an end-to-end run.

- **TrueFoundry** — OpenAI-compatible AI gateway that routes all LLM calls (reasoning + email prose).
- **ClickHouse** — the analytical database used as the shared agent blackboard and outcome store. **Required.**
- **Pioneer** (Fastino) — self-improving inference for the qualification scorer; falls back to a built-in heuristic scorer when unavailable.
- **Airbyte** — context/data layer for GitHub discovery (and Gmail send if the tier unlocks).
- **OpenUI** (Thesys) — generative UI for the thesis-refinement chat and on-demand company deep-dives.
- **Langfuse** — tracing for every agent step and LLM call; safely disabled if unconfigured.
- **Senso / cited.md** — publishes the run's Deal Memo to the open web.
- **x402** — optional pay-per-fetch paywall (USDC on Base Sepolia testnet) protecting the Deal Memo endpoint.

---

## Getting started

### Prerequisites

- Python 3.11+
- Node.js (for the frontend)
- A ClickHouse Cloud instance (required to run the core)

### 1. Configure environment

```bash
cp .env.template .env
```

Fill in your credentials. At minimum, set the ClickHouse variables (`CLICKHOUSE_HOST`, `CLICKHOUSE_PASSWORD`, etc.). Other integrations are optional and degrade gracefully — see `.env.template` for the full list.

### 2. Run the Python core

```bash
pip install -r requirements.txt

# Fast smoke run (tiny budget, single Tick — verifies wiring end to end)
python -m angent.main --smoke

# Demo run (seeded historical outcomes, then runs to termination)
python -m angent.main --demo

# Custom run
python -m angent.main --thesis "..." --target 0.2 --budget 3 --deadline-minutes 10
```

The entrypoint loads config, ensures the ClickHouse schema exists, initializes tracing, builds the control loop, and drives it Tick by Tick with per-stage logging.

### 3. Run the frontend

```bash
cd genui-chat-app
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

---

## Design principles

- **Goal-driven, not pipeline-driven.** The Planner re-plans each Tick instead of running a fixed sequence.
- **All-or-nothing initiation.** The goal and initial state are durably persisted before the first Tick; a failed init leaves no partial record.
- **Per-stage failure containment.** A stage failure stops the rest of that Tick, preserves prior state, and the loop continues on the next Tick rather than crashing.
- **Human-in-the-loop by default.** No email is ever sent without explicit approval, and the gate cannot be bypassed.
- **Graceful degradation.** Every sponsor integration has a fallback so the core always runs end to end.

---

## Documentation

- [`flowchart.md`](flowchart.md) — full system flow diagram
- `.kiro/specs/angent/` — requirements, design, and task breakdown
