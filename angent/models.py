"""Core value objects and enums for the Angent control loop.

These dataclasses are the in-memory shapes that flow between the six agents
(Planner, Scanner, Qualifier, Writer, Sender, Optimizer) and the ClickHouse
blackboard. They mirror the design's "Core Value Objects" and the ClickHouse
table column definitions so persistence is a thin, mechanical mapping.

Design references:
  * ``Goal`` / ``LoopState`` / ``StopReason`` — Control Loop and Planner.
  * ``Candidate`` / ``Qualified``            — Scanner / Qualifier (``companies``).
  * ``Draft``                                — Writer (``emails``).
  * ``Outcome``                              — Optimizer (``outcomes``).
  * ``TickPlan`` / ``TickOutcome``           — Planner ``plan``/``reflect`` and
                                               ``ControlLoop.run_tick``.

Nothing here performs I/O; these are plain data holders with sensible defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal, Optional


# --- Termination ------------------------------------------------------------


class StopReason(str, Enum):
    """Why the Control_Loop stopped, in strict priority order.

    Subclassing ``str`` makes the value JSON/ClickHouse friendly: the enum
    member compares and serializes as its string value (e.g. ``"goal-met"``),
    matching the ``loop_state.stop_reason`` column. Priority order is
    goal-met > deadline-reached > email-budget-exhausted (Requirement 3.6).
    """

    GOAL_MET = "goal-met"
    DEADLINE_REACHED = "deadline-reached"
    EMAIL_BUDGET_EXHAUSTED = "email-budget-exhausted"


# --- Goal + loop state ------------------------------------------------------


@dataclass
class Goal:
    """The structured objective the loop pursues (Requirement 1).

    target_metric: measurable target (e.g. reply rate in [0,1] or a count of
        contacted thesis-fit companies).
    deadline: wall-clock stop time (between now+1min and now+365days).
    email_budget: hard cap on emails that may be sent, in 1..1000.
    """

    target_metric: float
    deadline: datetime
    email_budget: int


@dataclass
class LoopState:
    """The full state of a single run, read at Tick start and written at end.

    Maps to the ``loop_state`` ClickHouse table (latest version wins). The
    planner-tunable knobs (``thesis_breadth``, ``email_angle``, ``send_volume``)
    are what the Planner adapts when progress stalls (Requirement 2.7), and
    ``metric_history`` carries the per-Tick ``target_metric`` for stall
    detection.
    """

    run_id: str
    goal: Goal
    started_at: datetime
    tick_index: int = 0
    emails_sent: int = 0
    reply_rate: float = 0.0
    thesis_breadth: float = 0.5
    email_angle: str = ""
    send_volume: int = 0
    metric_history: list[float] = field(default_factory=list)
    status: Literal["running", "stopped"] = "running"
    stop_reason: Optional[StopReason] = None


# --- Candidates + qualification --------------------------------------------


@dataclass
class Candidate:
    """A startup discovered from a public signal source (the ``companies`` row).

    ``signals`` is the source-specific payload (stars/commits for GitHub,
    points/comments for Hacker News, etc.). ``source_unique_id`` is the
    source-specific natural key used to dedupe/upsert (Requirement 4.4).
    """

    source: str                 # 'github' | 'hackernews' | 'huggingface'
    source_unique_id: str        # source-specific natural dedupe key
    name: str
    url: str
    signals: dict = field(default_factory=dict)
    first_activity: Optional[datetime] = None  # must be within last 90 days at scan


@dataclass
class Qualified(Candidate):
    """A scored candidate that the Qualifier passes downstream (Requirement 5).

    Extends ``Candidate`` with the numeric fit score in ``[0,100]`` and the
    natural-language explanation (50..1000 chars) referencing the thesis.
    """

    fit_score: int = 0           # 0..100
    fit_explanation: str = ""


# --- Drafts (emails table) --------------------------------------------------


@dataclass
class Draft:
    """An outreach email draft (the ``emails`` row).

    Stored as unsent and unapproved on creation (Requirement 8.3). Modifying an
    approved draft reverts ``approved`` to False (Requirement 9.3). ``sent`` /
    ``failed`` / ``attempt_count`` track the send lifecycle (Requirement 10).
    """

    email_id: str
    company_id: str
    subject: str
    body: str
    angle: str = ""
    run_id: str = ""
    approved: bool = False
    sent: bool = False
    failed: bool = False
    attempt_count: int = 0
    sender_backend: str = "smtp"          # 'smtp' | 'gmail_agent'
    sent_at: Optional[datetime] = None
    failure_reason: Optional[str] = None


# --- Outcomes (outcomes table) ----------------------------------------------


@dataclass
class Outcome:
    """A reply/open event fed back into scoring + analytics (Requirement 6).

    ``seeded`` marks historical demo data loaded before the first Tick
    (Requirement 12.5). Maps to the ``outcomes`` ClickHouse row.
    """

    email_id: str
    company_id: str
    kind: Literal["reply", "open"]
    occurred_at: datetime
    seeded: bool = False
    run_id: str = ""
    outcome_id: str = ""


# --- Per-Tick plan + outcome ------------------------------------------------


@dataclass
class TickPlan:
    """The Planner's per-Tick decision (Requirement 2.3).

    Carries the planner-tunable knobs and which signal sources are enabled for
    this Tick. The Scanner reads ``sources`` (and the 90-day ``since`` window is
    derived at scan time), the Qualifier reads ``qualification_threshold``, and
    the Writer reads ``email_angle`` / ``send_volume``.
    """

    tick_index: int
    thesis_breadth: float = 0.5
    email_angle: str = ""
    send_volume: int = 0
    sources: list[str] = field(default_factory=list)   # enabled signal sources
    qualification_threshold: int = 0                   # 0..100


@dataclass
class TickOutcome:
    """The observed results of running one Tick (Requirement 2.6, 2.8).

    Fed to ``Planner.reflect`` to shape the next plan and persisted as part of
    the loop-state update after each Tick. ``failed_stage`` names the pipeline
    stage that failed (Scanner/Qualifier/Writer/Sender/Optimizer), if any
    (Requirement 2.5). ``stop_reason`` is set only on a **terminal** Tick — when
    ``ControlLoop.run_tick`` evaluates termination first and finds a stop
    condition, it returns an outcome carrying the single prioritized
    :class:`StopReason` and runs **no** stages (Requirements 2.2, 3.x); the run
    driver inspects it to stop the loop and persist the final state.
    """

    tick_index: int
    candidates_found: int = 0
    qualified_count: int = 0
    drafts_created: int = 0
    emails_sent: int = 0
    replies: int = 0
    reply_rate: float = 0.0
    failed_stage: Optional[str] = None
    stop_reason: Optional[StopReason] = None

    @property
    def terminal(self) -> bool:
        """True when this Tick stopped the loop (a ``stop_reason`` was set)."""
        return self.stop_reason is not None
