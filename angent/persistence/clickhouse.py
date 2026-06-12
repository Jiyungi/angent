"""ClickHouse blackboard client wrapper and schema creation.

This module wraps the ClickHouse Cloud HTTPS interface (port 8443, ``secure=True``)
via the ``clickhouse-connect`` driver and exposes a small, dependency-light client
the rest of the Angent core uses as its shared blackboard + analytics store.

Design highlights:
  * :class:`ClickHouseClient` lazily opens a single client from :class:`~angent.config.Config`.
  * Every write/read goes through a **bounded-retry helper** (up to 3 attempts).
    On total failure the helper *does not raise* — it returns a structured
    :class:`RetryResult` carrying an error indication while **retaining the
    unwritten payload** (the SQL/params it was asked to run) in memory so the
    caller can inspect or re-queue it. This implements the "retain unwritten
    payload, return an error indication on total failure" contract
    (Requirements 12.6, 20.4, 21.1).
  * :meth:`ClickHouseClient.create_schema` issues idempotent ``CREATE TABLE IF
    NOT EXISTS`` statements for all six blackboard tables — ``companies``,
    ``emails``, ``outcomes``, ``loop_state``, ``publications`` and ``fetches`` —
    using the exact column definitions and engines from the design
    (Requirements 12.1, 12.2).

The schema mirrors design.md → Data Models → ClickHouse Blackboard verbatim.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Sequence

from ..config import Config, load_config
from ..models import Goal, LoopState, StopReason

logger = logging.getLogger("angent.persistence.clickhouse")

# Number of attempts for any bounded-retry operation (Requirement 12.6).
DEFAULT_MAX_ATTEMPTS = 3
# Base back-off (seconds) between attempts; grows linearly per attempt.
DEFAULT_RETRY_BACKOFF = 0.5


# --- Table DDL (verbatim from design.md Data Models) ------------------------
# Each statement is idempotent via ``IF NOT EXISTS`` so create_schema can run
# repeatedly (e.g. on every demo boot) without error.

COMPANIES_DDL = """
CREATE TABLE IF NOT EXISTS companies (
    company_id       String,
    source           LowCardinality(String),
    source_unique_id String,
    name             String,
    url              String,
    signals          String,
    first_activity   DateTime,
    fit_score        Int32,
    fit_explanation  String,
    created_at       DateTime,
    updated_at       DateTime,
    version          UInt64
) ENGINE = ReplacingMergeTree(version)
ORDER BY (source, source_unique_id)
""".strip()

EMAILS_DDL = """
CREATE TABLE IF NOT EXISTS emails (
    email_id      String,
    run_id        String,
    company_id    String,
    subject       String,
    body          String,
    angle         LowCardinality(String),
    approved      UInt8,
    sent          UInt8,
    failed        UInt8,
    attempt_count UInt8,
    sender_backend LowCardinality(String),
    sent_at       Nullable(DateTime),
    failure_reason Nullable(String),
    created_at    DateTime,
    updated_at    DateTime,
    version       UInt64
) ENGINE = ReplacingMergeTree(version)
ORDER BY email_id
""".strip()

OUTCOMES_DDL = """
CREATE TABLE IF NOT EXISTS outcomes (
    outcome_id  String,
    run_id      String,
    email_id    String,
    company_id  String,
    kind        LowCardinality(String),
    occurred_at DateTime,
    seeded      UInt8
) ENGINE = MergeTree
ORDER BY (run_id, occurred_at)
""".strip()

LOOP_STATE_DDL = """
CREATE TABLE IF NOT EXISTS loop_state (
    run_id        String,
    tick_index    UInt32,
    goal_target   Float64,
    goal_deadline DateTime,
    goal_email_budget UInt32,
    emails_sent   UInt32,
    reply_rate    Float64,
    thesis_breadth Float64,
    email_angle   String,
    send_volume   UInt32,
    status        LowCardinality(String),
    stop_reason   Nullable(String),
    started_at    DateTime,
    updated_at    DateTime,
    version       UInt64
) ENGINE = ReplacingMergeTree(version)
ORDER BY run_id
""".strip()

PUBLICATIONS_DDL = """
CREATE TABLE IF NOT EXISTS publications (
    publication_id String,
    run_id        String,
    cited_md_url  String,
    slug          String,
    handle        String,
    local_path    Nullable(String),
    published_ok  UInt8,
    published_at  DateTime,
    updated_at    DateTime,
    version       UInt64
) ENGINE = ReplacingMergeTree(version)
ORDER BY run_id
""".strip()

FETCHES_DDL = """
CREATE TABLE IF NOT EXISTS fetches (
    fetch_id     String,
    run_id       String,
    paid         UInt8,
    amount       String,
    network      LowCardinality(String),
    payer        String,
    tx_reference Nullable(String),
    settled_at   DateTime
) ENGINE = MergeTree
ORDER BY (run_id, settled_at)
""".strip()

# Ordered so dependent/most-used tables come first; order is not strictly
# required since there are no FKs in ClickHouse, but keeps logs readable.
SCHEMA_DDL: dict[str, str] = {
    "companies": COMPANIES_DDL,
    "emails": EMAILS_DDL,
    "outcomes": OUTCOMES_DDL,
    "loop_state": LOOP_STATE_DDL,
    "publications": PUBLICATIONS_DDL,
    "fetches": FETCHES_DDL,
}


@dataclass
class RetryResult:
    """Outcome of a bounded-retry operation.

    On success ``ok`` is True and ``rows`` carries the result (query rows, or
    ``None`` for commands). On total failure ``ok`` is False, ``error`` holds the
    last exception message, and ``payload`` retains the *unwritten* operation
    (the statement + parameters) so the caller can re-queue or inspect it
    rather than losing it (Requirement 12.6).
    """

    ok: bool
    rows: Optional[Any] = None
    error: Optional[str] = None
    attempts: int = 0
    payload: Optional[dict[str, Any]] = None

    def __bool__(self) -> bool:  # truthiness mirrors success
        return self.ok


class ClickHouseError(Exception):
    """Raised only when explicitly requested via ``raise_on_failure=True``."""


class ClickHouseClient:
    """Thin wrapper over ``clickhouse-connect`` with bounded-retry semantics.

    Typical usage::

        client = ClickHouseClient.from_config()
        client.connect()
        client.create_schema()
        result = client.query("SELECT count() FROM companies")
        if result.ok:
            print(result.rows)
    """

    def __init__(
        self,
        config: Optional[Config] = None,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
    ) -> None:
        self._config = config or load_config()
        self._ch = self._config.clickhouse
        self._client: Any = None
        self.max_attempts = max_attempts
        self.retry_backoff = retry_backoff
        # Retained unwritten payloads from operations that exhausted all retries.
        self.failed_payloads: list[dict[str, Any]] = []

    # -- construction --------------------------------------------------------

    @classmethod
    def from_config(cls, config: Optional[Config] = None, **kwargs: Any) -> "ClickHouseClient":
        return cls(config=config, **kwargs)

    # -- connection ----------------------------------------------------------

    def connect(self) -> Any:
        """Open (once) and return the underlying ClickHouse client.

        Uses the ClickHouse Cloud HTTPS interface: host/port (default 8443),
        username, password and database from :class:`~angent.config.Config`,
        with ``secure=True`` (TLS).
        """
        if self._client is not None:
            return self._client

        if not self._ch.is_configured:
            raise ClickHouseError(
                "ClickHouse is not configured: CLICKHOUSE_HOST is missing. "
                "Populate it in .env before connecting."
            )

        import clickhouse_connect  # imported lazily so the module imports without creds

        logger.info(
            "Connecting to ClickHouse host=%s port=%s db=%s (secure=True)",
            self._ch.host,
            self._ch.port,
            self._ch.database,
        )
        self._client = clickhouse_connect.get_client(
            host=self._ch.host,
            port=self._ch.port,
            username=self._ch.user,
            password=self._ch.password or "",
            database=self._ch.database,
            secure=True,
        )
        return self._client

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # pragma: no cover - best-effort close
                logger.debug("Error closing ClickHouse client", exc_info=True)
            finally:
                self._client = None

    # -- bounded-retry core --------------------------------------------------

    def _run_with_retry(
        self,
        op_name: str,
        fn,
        payload: dict[str, Any],
        *,
        raise_on_failure: bool = False,
    ) -> RetryResult:
        """Run ``fn`` up to ``max_attempts`` times with linear back-off.

        ``payload`` describes the operation (statement + params) and is retained
        on total failure so the unwritten work is never silently lost.
        """
        last_error: Optional[str] = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                rows = fn()
                return RetryResult(ok=True, rows=rows, attempts=attempt, payload=payload)
            except Exception as exc:  # noqa: BLE001 - we deliberately catch broadly to retry
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "ClickHouse %s attempt %d/%d failed: %s",
                    op_name,
                    attempt,
                    self.max_attempts,
                    last_error,
                )
                if attempt < self.max_attempts:
                    time.sleep(self.retry_backoff * attempt)

        # All attempts exhausted: retain payload, return an error indication.
        self.failed_payloads.append(payload)
        logger.error(
            "ClickHouse %s failed after %d attempts; payload retained in memory",
            op_name,
            self.max_attempts,
        )
        result = RetryResult(
            ok=False,
            error=last_error,
            attempts=self.max_attempts,
            payload=payload,
        )
        if raise_on_failure:
            raise ClickHouseError(
                f"{op_name} failed after {self.max_attempts} attempts: {last_error}"
            )
        return result

    # -- public operations ---------------------------------------------------

    def command(
        self,
        statement: str,
        parameters: Optional[dict[str, Any]] = None,
        *,
        raise_on_failure: bool = False,
    ) -> RetryResult:
        """Execute a DDL/DML statement (no row results) with bounded retry."""
        client = self.connect()
        payload = {"kind": "command", "statement": statement, "parameters": parameters}

        def _do() -> None:
            client.command(statement, parameters=parameters)
            return None

        return self._run_with_retry(
            "command", _do, payload, raise_on_failure=raise_on_failure
        )

    def query(
        self,
        statement: str,
        parameters: Optional[dict[str, Any]] = None,
        *,
        raise_on_failure: bool = False,
    ) -> RetryResult:
        """Run a SELECT-style query, returning result rows with bounded retry.

        On success ``result.rows`` is the list of row tuples
        (``query(...).result_rows``).
        """
        client = self.connect()
        payload = {"kind": "query", "statement": statement, "parameters": parameters}

        def _do() -> Any:
            qr = client.query(statement, parameters=parameters)
            return qr.result_rows

        return self._run_with_retry(
            "query", _do, payload, raise_on_failure=raise_on_failure
        )

    def insert(
        self,
        table: str,
        data: Sequence[Sequence[Any]],
        column_names: Sequence[str],
        *,
        raise_on_failure: bool = False,
    ) -> RetryResult:
        """Insert rows into ``table`` with bounded retry.

        ``data`` is a sequence of row sequences aligned to ``column_names``.
        """
        client = self.connect()
        payload = {
            "kind": "insert",
            "table": table,
            "column_names": list(column_names),
            "data": data,
        }

        def _do() -> None:
            client.insert(table, data, column_names=list(column_names))
            return None

        return self._run_with_retry(
            "insert", _do, payload, raise_on_failure=raise_on_failure
        )

    # -- schema --------------------------------------------------------------

    def create_schema(self, *, raise_on_failure: bool = False) -> dict[str, RetryResult]:
        """Create all six blackboard tables (idempotent).

        Returns a per-table map of :class:`RetryResult` so callers can see which
        tables succeeded. Each ``CREATE TABLE`` runs through the bounded-retry
        helper. Tables: companies, emails, outcomes, loop_state, publications,
        fetches (Requirements 12.1, 12.2).
        """
        results: dict[str, RetryResult] = {}
        for table, ddl in SCHEMA_DDL.items():
            logger.info("Ensuring ClickHouse table exists: %s", table)
            results[table] = self.command(ddl, raise_on_failure=raise_on_failure)
        return results

    # Alias matching the task wording ("create_tables").
    def create_tables(self, *, raise_on_failure: bool = False) -> dict[str, RetryResult]:
        return self.create_schema(raise_on_failure=raise_on_failure)

    # -- loop_state read/write (latest-version-wins) -------------------------

    # Column order matches the ``loop_state`` table DDL above and is reused for
    # both insert (write) and select (read) so the two stay in lock-step.
    LOOP_STATE_COLUMNS: tuple[str, ...] = (
        "run_id",
        "tick_index",
        "goal_target",
        "goal_deadline",
        "goal_email_budget",
        "emails_sent",
        "reply_rate",
        "thesis_breadth",
        "email_angle",
        "send_volume",
        "status",
        "stop_reason",
        "started_at",
        "updated_at",
        "version",
    )

    def _next_loop_state_version(self, run_id: str) -> int:
        """Return ``max(version) + 1`` for ``run_id`` (1 if no rows yet).

        On a read failure we fall back to a time-based version so the new write
        still sorts after older rows on the ``ReplacingMergeTree`` and is never
        silently dropped.
        """
        result = self.query(
            "SELECT max(version) FROM loop_state WHERE run_id = {run_id:String}",
            parameters={"run_id": run_id},
        )
        if result.ok and result.rows:
            current = result.rows[0][0]
            if current is not None:
                return int(current) + 1
            return 1
        # Read failed entirely — use a monotonic-ish fallback so latest still wins.
        logger.warning(
            "Could not read current loop_state version for run_id=%s; "
            "falling back to time-based version",
            run_id,
        )
        return int(time.time())

    def write_loop_state(
        self, state: LoopState, *, raise_on_failure: bool = False
    ) -> RetryResult:
        """Persist ``state`` to ``loop_state`` with an incremented ``version``.

        The new row carries ``max(version)+1`` for the run and ``updated_at =
        now`` so the ``ReplacingMergeTree(version)`` keeps this row as the
        winner — any subsequent :meth:`read_loop_state` (by this or another
        agent) returns the just-written state (Requirements 12.3, 22).
        """
        version = self._next_loop_state_version(state.run_id)
        updated_at = datetime.now()
        stop_reason = state.stop_reason.value if state.stop_reason is not None else None

        row = [
            state.run_id,
            int(state.tick_index),
            float(state.goal.target_metric),
            state.goal.deadline,
            int(state.goal.email_budget),
            int(state.emails_sent),
            float(state.reply_rate),
            float(state.thesis_breadth),
            state.email_angle,
            int(state.send_volume),
            str(state.status),
            stop_reason,
            state.started_at,
            updated_at,
            version,
        ]

        return self.insert(
            "loop_state",
            [row],
            list(self.LOOP_STATE_COLUMNS),
            raise_on_failure=raise_on_failure,
        )

    def read_loop_state(
        self, run_id: str, *, raise_on_failure: bool = False
    ) -> Optional[LoopState]:
        """Return the most recent :class:`LoopState` for ``run_id`` or ``None``.

        Selects the highest ``version`` row (``ORDER BY version DESC LIMIT 1``),
        which on the ``ReplacingMergeTree`` is the latest-written state, and
        reconstructs the :class:`LoopState` (nested :class:`Goal` and the
        :class:`StopReason` enum). Returns ``None`` when the run has no rows.
        """
        columns = ", ".join(self.LOOP_STATE_COLUMNS)
        result = self.query(
            f"SELECT {columns} FROM loop_state "
            "WHERE run_id = {run_id:String} "
            "ORDER BY version DESC LIMIT 1",
            parameters={"run_id": run_id},
            raise_on_failure=raise_on_failure,
        )
        if not result.ok or not result.rows:
            return None

        (
            r_run_id,
            tick_index,
            goal_target,
            goal_deadline,
            goal_email_budget,
            emails_sent,
            reply_rate,
            thesis_breadth,
            email_angle,
            send_volume,
            status,
            stop_reason,
            started_at,
            _updated_at,
            _version,
        ) = result.rows[0]

        goal = Goal(
            target_metric=float(goal_target),
            deadline=goal_deadline,
            email_budget=int(goal_email_budget),
        )
        return LoopState(
            run_id=r_run_id,
            goal=goal,
            started_at=started_at,
            tick_index=int(tick_index),
            emails_sent=int(emails_sent),
            reply_rate=float(reply_rate),
            thesis_breadth=float(thesis_breadth),
            email_angle=email_angle,
            send_volume=int(send_volume),
            status=status,
            stop_reason=StopReason(stop_reason) if stop_reason else None,
        )

    def __enter__(self) -> "ClickHouseClient":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


if __name__ == "__main__":  # Manual smoke check against live ClickHouse, if configured.
    logging.basicConfig(level=logging.INFO)
    client = ClickHouseClient.from_config()
    if not client._ch.is_configured:
        print("ClickHouse not configured (CLICKHOUSE_HOST unset); skipping live check.")
    else:
        results = client.create_schema()
        for name, res in results.items():
            print(f"  {name:14s}: {'ok' if res.ok else 'FAILED -> ' + str(res.error)}")
        client.close()
