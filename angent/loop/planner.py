"""The Planner: turns ``LoopState`` into a per-Tick plan and reflects outcomes.

The Planner owns the two pure-ish decisions of the control loop's outer cycle
(design "Control Loop and Planner"):

  * ``plan(state) -> TickPlan`` — read the planner-tunable knobs off the current
    ``LoopState`` and emit the concrete ``TickPlan`` the downstream agents
    (Scanner / Qualifier / Writer) consume for this Tick.
  * ``reflect(state, outcomes) -> LoopState`` — fold the observed Tick results
    (notably the reply-rate / target_metric) back into the *next* ``LoopState``:
    append to ``metric_history``, update ``reply_rate``, advance ``tick_index``,
    and — when progress stalls — adapt at least one knob.

Stall adaptation (Requirement 2.7): when the improvement in ``target_metric``
stays below ``progress_threshold`` across ``stall_window`` consecutive Ticks,
the Planner changes at least one of thesis breadth, email angle, or send
volume so the next Tick explores a different part of the space.

Neither method performs I/O; ``reflect`` never mutates the input ``LoopState``
(it returns a new instance) and preserves the immutable run identity
(``run_id`` / ``goal`` / ``started_at``).
"""

from __future__ import annotations

import dataclasses
from typing import List

from ..models import LoopState, TickPlan, TickOutcome


# Default signal sources enabled each Tick (design: Scanner reads ``sources``).
DEFAULT_SOURCES: List[str] = ["github", "hackernews"]

# Sensible default Qualifier cutoff on the 0..100 fit scale. 50 keeps roughly
# the upper half of scored candidates: permissive enough to fill the funnel
# early while still filtering out clear non-fits.
DEFAULT_QUALIFICATION_THRESHOLD: int = 50

# Knob bounds / steps used during stall adaptation.
_MAX_THESIS_BREADTH = 1.0
_BREADTH_STEP = 0.2
_VOLUME_STEP = 5
_DEFAULT_VOLUME_FLOOR = 10

# Rotation pool for the email angle knob. When stalled and the current angle is
# unknown/empty we start at the front; otherwise we advance to the next entry.
_ANGLE_ROTATION: List[str] = [
    "technical-depth",
    "traction-and-growth",
    "founder-market-fit",
    "vision-and-mission",
]


class Planner:
    """Plans each Tick and reflects outcomes into the next ``LoopState``.

    Constructor knobs control stall detection:

    * ``stall_window`` (N): number of consecutive Ticks over which improvement
      must stay below ``progress_threshold`` before adaptation kicks in.
    * ``progress_threshold``: minimum per-Tick improvement in ``target_metric``
      considered "real progress". Improvements at or below this are a stall.
    """

    def __init__(
        self,
        *,
        stall_window: int = 3,
        progress_threshold: float = 0.01,
        sources: List[str] | None = None,
        qualification_threshold: int = DEFAULT_QUALIFICATION_THRESHOLD,
    ) -> None:
        if stall_window < 2:
            # Need at least two samples to measure an improvement between Ticks.
            raise ValueError("stall_window must be >= 2")
        self.stall_window = stall_window
        self.progress_threshold = progress_threshold
        self.sources = list(sources) if sources is not None else list(DEFAULT_SOURCES)
        self.qualification_threshold = qualification_threshold

    # -- plan ---------------------------------------------------------------

    def plan(self, state: LoopState) -> TickPlan:
        """Build the ``TickPlan`` for the Tick described by ``state``.

        The plan simply projects the current planner-tunable knobs plus the
        Planner's configured sources / qualification threshold onto a concrete
        per-Tick decision. Adaptation happens in ``reflect`` (which produces the
        next state), so ``plan`` is a faithful read of the state it is given.
        """
        return TickPlan(
            tick_index=state.tick_index,
            thesis_breadth=state.thesis_breadth,
            email_angle=state.email_angle,
            send_volume=state.send_volume,
            sources=list(self.sources),
            qualification_threshold=self.qualification_threshold,
        )

    # -- reflect ------------------------------------------------------------

    def reflect(self, state: LoopState, outcomes: TickOutcome) -> LoopState:
        """Fold ``outcomes`` into the next ``LoopState``.

        Returns a new ``LoopState`` (the input is not mutated) with:
          * ``metric_history`` extended by this Tick's observed target_metric,
          * ``reply_rate`` and ``emails_sent`` updated from the outcome,
          * ``tick_index`` advanced by one,
          * one or more knobs adapted when a stall is detected.
        """
        observed = outcomes.reply_rate

        new_history = list(state.metric_history)
        new_history.append(observed)

        thesis_breadth = state.thesis_breadth
        email_angle = state.email_angle
        send_volume = state.send_volume

        if self._is_stalled(new_history):
            thesis_breadth, email_angle, send_volume = self._adapt_knobs(
                thesis_breadth, email_angle, send_volume
            )

        return dataclasses.replace(
            state,
            tick_index=state.tick_index + 1,
            emails_sent=state.emails_sent + outcomes.emails_sent,
            reply_rate=observed,
            thesis_breadth=thesis_breadth,
            email_angle=email_angle,
            send_volume=send_volume,
            metric_history=new_history,
        )

    # -- stall detection / adaptation --------------------------------------

    def _is_stalled(self, history: List[float]) -> bool:
        """True when improvement stayed < threshold across the last N Ticks.

        We need ``stall_window`` consecutive Ticks to judge, which requires
        ``stall_window + 1`` samples (N gaps between N+1 points). If every gap
        in that trailing window improved by at most ``progress_threshold``, the
        run is stalled.
        """
        needed = self.stall_window + 1
        if len(history) < needed:
            return False
        window = history[-needed:]
        for prev, curr in zip(window, window[1:]):
            if (curr - prev) > self.progress_threshold:
                return False  # a real improvement in the window -> not stalled
        return True

    def _adapt_knobs(
        self, thesis_breadth: float, email_angle: str, send_volume: int
    ) -> tuple[float, str, int]:
        """Change at least one knob to escape the stall.

        Strategy: widen the thesis breadth first (explore more candidates); if
        breadth is already maxed out, rotate the email angle and bump send
        volume so *something* always changes (Requirement 2.7).
        """
        changed = False

        if thesis_breadth < _MAX_THESIS_BREADTH:
            thesis_breadth = min(_MAX_THESIS_BREADTH, round(thesis_breadth + _BREADTH_STEP, 4))
            changed = True

        # Always rotate the angle when stalled; cheap and guarantees variation
        # even when breadth is saturated.
        email_angle = self._next_angle(email_angle)
        changed = True

        if not changed:  # defensive; rotation above always sets changed
            send_volume = max(_DEFAULT_VOLUME_FLOOR, send_volume + _VOLUME_STEP)

        # If breadth was already maxed, also push volume so the adaptation has
        # real teeth rather than just an angle swap.
        if thesis_breadth >= _MAX_THESIS_BREADTH:
            send_volume = max(_DEFAULT_VOLUME_FLOOR, send_volume + _VOLUME_STEP)

        return thesis_breadth, email_angle, send_volume

    @staticmethod
    def _next_angle(current: str) -> str:
        """Advance the email angle to the next entry in the rotation pool."""
        if current in _ANGLE_ROTATION:
            idx = _ANGLE_ROTATION.index(current)
            return _ANGLE_ROTATION[(idx + 1) % len(_ANGLE_ROTATION)]
        return _ANGLE_ROTATION[0]
