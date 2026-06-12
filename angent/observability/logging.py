"""Per-stage log/console emitter for the Angent Control_Loop.

The front end is an optional enhancement (Requirement 16): the loop must be
demoable from backend logs and console alone. This module provides a
``StageLogger`` that emits a timestamped, stage-identified record for each of
the canonical Control_Loop stages to BOTH the Python logging backend and the
console (stdout), within 2 seconds of stage completion.

It mirrors the thin ``_stage`` console helper used in ``checkpoint7_demo.py``
(``[<iso-timestamp>] [<STAGE>] <message>``) and hardens it for production use:

- A failed record write is caught so the remaining stages keep running, and an
  error record naming the failed stage is emitted instead (Requirement 16.3).
- ``sponsor_failure(technology, error)`` surfaces an error naming the failing
  technology without crashing the loop, so shared state is preserved and the
  loop continues on its fallback path (Requirement 16.4 / 18.10).

Stages (Requirement 16.1):
    progress, qualifications, drafts, sends, reply_rate_trend
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import Any, Optional, TextIO

__all__ = [
    "STAGES",
    "StageLogger",
    "get_stage_logger",
    "log_stage",
]

# Canonical Control_Loop stages that MUST emit a record (Requirement 16.1).
STAGES = ("progress", "qualifications", "drafts", "sends", "reply_rate_trend")

# Stage identifier used when the loop reports a sponsor-integration failure.
_SPONSOR_STAGE = "sponsor"
# Stage identifier used when emitting a record itself failed (Requirement 16.3).
_LOG_ERROR_STAGE = "log-error"


def _iso_now() -> str:
    """Return an ISO-8601 UTC timestamp with millisecond precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _format_fields(fields: dict[str, Any]) -> str:
    """Render structured ``key=value`` pairs appended to a record message."""
    if not fields:
        return ""
    parts = []
    for key, value in fields.items():
        parts.append(f"{key}={value!r}" if isinstance(value, str) else f"{key}={value}")
    return " " + " ".join(parts)


class StageLogger:
    """Emit timestamped, stage-identified records to logs + console.

    Each emitted record carries an ISO timestamp and a stage identifier and is
    written to both the Python logging backend and the console within 2 seconds
    of stage completion (the write is synchronous and the console stream is
    flushed immediately).
    """

    def __init__(
        self,
        logger: Optional[logging.Logger] = None,
        console: Optional[TextIO] = None,
    ) -> None:
        self._logger = logger or logging.getLogger("angent.stage")
        # Default to stdout but capture it lazily so tests can patch sys.stdout.
        self._console = console
        # Levels chosen so backend filtering can separate errors from progress.
        self._level_for = {
            _LOG_ERROR_STAGE: logging.ERROR,
            _SPONSOR_STAGE: logging.ERROR,
        }

    # -- core API -----------------------------------------------------------

    def log_stage(self, stage: str, message: str, **fields: Any) -> bool:
        """Emit one timestamped, stage-identified record.

        Returns ``True`` if the record was written to both sinks, ``False`` if
        the write failed. A failed write never raises: it is caught and an
        error record naming the failed stage is emitted instead so the caller
        can continue with the remaining stages (Requirement 16.3).
        """
        ts = _iso_now()
        rendered = f"{message}{_format_fields(fields)}"
        record = f"[{ts}] [{stage}] {rendered}"
        level = self._level_for.get(stage, logging.INFO)
        try:
            # Backend log sink.
            self._logger.log(level, "[%s] %s", stage, rendered)
            # Console sink (flushed so it lands within the 2s budget).
            stream = self._console if self._console is not None else sys.stdout
            print(record, file=stream, flush=True)
            return True
        except Exception as exc:  # noqa: BLE001 - never let logging crash the loop
            self._emit_log_failure(stage, exc)
            return False

    def _emit_log_failure(self, failed_stage: str, exc: Exception) -> None:
        """Emit an error record naming the stage whose write failed (Req 16.3).

        This uses only the backend logger and a guarded ``print`` so that a
        broken console stream cannot cascade into another exception.
        """
        ts = _iso_now()
        msg = f"failed to write '{failed_stage}' stage record: {exc}"
        try:
            self._logger.error("[%s] %s", _LOG_ERROR_STAGE, msg)
        except Exception:  # noqa: BLE001
            pass
        try:
            print(f"[{ts}] [{_LOG_ERROR_STAGE}] {msg}", file=sys.stderr, flush=True)
        except Exception:  # noqa: BLE001
            pass

    # -- convenience methods, one per canonical stage -----------------------

    def progress(self, message: str, **fields: Any) -> bool:
        """Emit a loop-progress record (Requirement 16.1)."""
        return self.log_stage("progress", message, **fields)

    def qualifications(self, message: str, **fields: Any) -> bool:
        """Emit a qualification-stage record (Requirement 16.1)."""
        return self.log_stage("qualifications", message, **fields)

    def drafts(self, message: str, **fields: Any) -> bool:
        """Emit a drafting-stage record (Requirement 16.1)."""
        return self.log_stage("drafts", message, **fields)

    def sends(self, message: str, **fields: Any) -> bool:
        """Emit a sending-stage record (Requirement 16.1)."""
        return self.log_stage("sends", message, **fields)

    def reply_rate_trend(self, message: str, **fields: Any) -> bool:
        """Emit a reply-rate-trend record (Requirement 16.1)."""
        return self.log_stage("reply_rate_trend", message, **fields)

    # -- sponsor-integration failure ---------------------------------------

    def sponsor_failure(self, technology: str, error: Any, **fields: Any) -> bool:
        """Surface a sponsor-integration failure naming the technology.

        Records an error that names the failing technology (e.g. "pioneer",
        "airbyte", "langfuse", "guild", "senso", "x402") without crashing, so
        the loop preserves shared state and proceeds on its fallback path
        (Requirement 16.4 / 18.10). Returns whether the record was written.
        """
        return self.log_stage(
            _SPONSOR_STAGE,
            f"sponsor integration '{technology}' failed: {error}",
            technology=technology,
            **fields,
        )


# Module-level default logger so callers can use a simple function API.
_default_logger = StageLogger()


def get_stage_logger() -> StageLogger:
    """Return the process-wide default :class:`StageLogger`."""
    return _default_logger


def log_stage(stage: str, message: str, **fields: Any) -> bool:
    """Emit a record via the default :class:`StageLogger` (module API)."""
    return _default_logger.log_stage(stage, message, **fields)
