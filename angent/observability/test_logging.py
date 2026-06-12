"""Unit tests for the per-stage log/console emitter (Requirement 16)."""

from __future__ import annotations

import io
import logging
import re

from angent.observability.logging import STAGES, StageLogger, log_stage

# [<iso-timestamp>] [<stage>] <message>
_RECORD_RE = re.compile(
    r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\] \[(?P<stage>[^\]]+)\] "
)


def _make_logger() -> tuple[StageLogger, io.StringIO, list[logging.LogRecord]]:
    console = io.StringIO()
    backend = logging.getLogger("angent.stage.test")
    backend.handlers.clear()
    backend.setLevel(logging.DEBUG)
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    backend.addHandler(_Capture())
    return StageLogger(logger=backend, console=console), console, captured


def test_each_stage_emits_timestamped_identified_record_to_both_sinks():
    """Req 16.1: a record per stage with an ISO timestamp + stage id, to both sinks."""
    sl, console, captured = _make_logger()

    sl.progress("tick 1 of N")
    sl.qualifications("scored 5 candidates")
    sl.drafts("drafted 2 emails")
    sl.sends("sent 1 email")
    sl.reply_rate_trend("reply_rate=0.20")

    lines = [ln for ln in console.getvalue().splitlines() if ln.strip()]
    assert len(lines) == len(STAGES)
    emitted_stages = []
    for line in lines:
        m = _RECORD_RE.match(line)
        assert m is not None, f"record missing timestamp/stage id: {line!r}"
        emitted_stages.append(m.group("stage"))
    assert emitted_stages == list(STAGES)
    # Backend log received the same number of records.
    assert len(captured) == len(STAGES)


def test_log_stage_includes_structured_fields():
    sl, console, _ = _make_logger()
    assert sl.log_stage("progress", "tick complete", tick=3, replies=2) is True
    out = console.getvalue()
    assert "tick=3" in out and "replies=2" in out


def test_failed_write_continues_and_emits_error_record_naming_stage(capsys):
    """Req 16.3: a failed record write is caught; an error record names the stage."""

    class _Broken(io.StringIO):
        def write(self, *_a, **_k):  # noqa: ANN002
            raise OSError("disk full")

    backend = logging.getLogger("angent.stage.broken")
    backend.handlers.clear()
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    backend.addHandler(_Capture())
    sl = StageLogger(logger=backend, console=_Broken())

    # The write fails but does NOT raise, so remaining stages can continue.
    ok = sl.sends("sent 1 email")
    assert ok is False
    # An error record naming the failed stage was emitted.
    err = capsys.readouterr().err
    assert "log-error" in err
    assert "sends" in err


def test_sponsor_failure_names_technology_without_crashing():
    """Req 16.4 / 18.10: surface a sponsor failure naming the technology."""
    sl, console, captured = _make_logger()
    ok = sl.sponsor_failure("pioneer", "connection timeout")
    assert ok is True
    out = console.getvalue()
    assert "pioneer" in out
    assert "[sponsor]" in out
    # Surfaced at error level on the backend.
    assert captured and captured[-1].levelno == logging.ERROR


def test_module_level_log_stage_api_does_not_raise():
    assert log_stage("progress", "module-level record") is True
