"""HTTP endpoints the Guild orchestrator calls to drive the loop (Reqs 11.3, 11.4, 18.7).

The Guild orchestrator (TypeScript, task 16.1) orchestrates the run by calling
the Python core over HTTP. This module exposes that surface using only the
Python standard library (:mod:`http.server`) so it adds **no** new dependency
and starts instantly.

The single most important guarantee here is **governance parity** (Requirement
11.4): the ``/authorize_send`` endpoint calls the *exact same*
:meth:`~angent.governance.gate.GovernanceGate.authorize_send` decision function
that the self-driven :class:`~angent.loop.control_loop.ControlLoop` routes every
send through. So whether a send is initiated by the loop self-driving or by the
Guild orchestrator over HTTP, it passes through identical PERMIT/BLOCK/DEFER
governance — there is no second, weaker code path.

Endpoints:
  * ``GET  /health``         — liveness probe; ``{"status": "ok"}``.
  * ``POST /tick``           — advance one Tick for a run. Body ``{"run_id": ...}``
    (or an inline ``state``); reads the latest :class:`LoopState` from the
    blackboard, calls :meth:`ControlLoop.run_tick`, returns the
    :class:`TickOutcome` as JSON.
  * ``POST /authorize_send`` — given a draft + budget/window params, calls the
    shared :meth:`GovernanceGate.authorize_send` and returns the
    :class:`SendDecision` (decision/reason/pending/message) as JSON.
  * ``POST /approve``        — ``{"draft_id": ..., "investor_id": ...}``; calls
    :meth:`GovernanceGate.approve`.

Wiring:
  * :func:`make_server` builds a configured ``ThreadingHTTPServer`` from an
    injected ``control_loop`` and ``gate`` (tests inject fakes).
  * :func:`run_server` / ``python -m angent.loop.server`` builds a default
    production wiring (a :class:`ControlLoop` and its shared ``gate``) and serves
    until interrupted.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from ..governance.gate import RateWindow
from ..models import Draft

logger = logging.getLogger("angent.loop.server")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8077


# --- JSON helpers -----------------------------------------------------------


def _to_jsonable(value: Any) -> Any:
    """Recursively convert dataclasses / enums / datetimes to JSON-safe values."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _to_jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    # Fall back to the object's attribute dict, else its string form.
    if hasattr(value, "__dict__"):
        return {k: _to_jsonable(v) for k, v in vars(value).items()}
    return str(value)


def _draft_from_payload(payload: dict[str, Any]) -> Draft:
    """Build a :class:`Draft` from a request body for ``authorize_send``.

    Only the fields the decision inspects (``approved``, ``email_id``) are
    required; the rest default. Accepts a nested ``"draft"`` object or the draft
    fields at the top level.
    """
    src = payload.get("draft", payload)
    return Draft(
        email_id=str(src.get("email_id", "")),
        company_id=str(src.get("company_id", "")),
        subject=str(src.get("subject", "")),
        body=str(src.get("body", "")),
        angle=str(src.get("angle", "")),
        run_id=str(src.get("run_id", "")),
        approved=bool(src.get("approved", False)),
        sent=bool(src.get("sent", False)),
        failed=bool(src.get("failed", False)),
        attempt_count=int(src.get("attempt_count", 0) or 0),
    )


def _window_from_payload(payload: dict[str, Any]) -> RateWindow:
    """Build a :class:`RateWindow` from a request body for ``authorize_send``."""
    window = payload.get("window", {}) or {}
    start_raw = window.get("window_start")
    window_start: Optional[datetime] = None
    if isinstance(start_raw, str) and start_raw:
        try:
            window_start = datetime.fromisoformat(start_raw)
        except ValueError:
            window_start = None
    return RateWindow(
        sent_in_window=int(window.get("sent_in_window", 0) or 0),
        limit=int(window.get("limit", payload.get("rate_limit", 1000)) or 0),
        window_start=window_start,
    )


# --- request handler --------------------------------------------------------


class _GuildHandler(BaseHTTPRequestHandler):
    """Stdlib request handler bound to a ``control_loop`` and ``gate``.

    The server instance carries ``control_loop`` and ``gate`` attributes (set by
    :func:`make_server`); each handler reads them off ``self.server``.
    """

    # Quiet the default noisy stderr access log; route through our logger.
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        logger.debug("%s - %s", self.address_string(), fmt % args)

    # -- low-level write helpers --------------------------------------------

    def _send_json(self, status: int, body: Any) -> None:
        payload = json.dumps(_to_jsonable(body)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("request body must be a JSON object")
        return parsed

    @property
    def _control_loop(self) -> Any:
        return getattr(self.server, "control_loop", None)

    @property
    def _gate(self) -> Any:
        return getattr(self.server, "gate", None)

    # -- routing ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path.rstrip("/") in ("", "/health"):
            self._send_json(200, {"status": "ok"})
            return
        self._send_json(404, {"error": "not found", "path": self.path})

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        route = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            payload = self._read_json_body()
        except (json.JSONDecodeError, ValueError) as exc:
            self._send_json(400, {"error": "invalid JSON body", "detail": str(exc)})
            return

        try:
            if route == "/tick":
                self._handle_tick(payload)
            elif route == "/authorize_send":
                self._handle_authorize_send(payload)
            elif route == "/approve":
                self._handle_approve(payload)
            else:
                self._send_json(404, {"error": "not found", "path": self.path})
        except Exception as exc:  # noqa: BLE001 - never leak a stack trace as 200
            logger.exception("Unhandled error serving %s", route)
            self._send_json(500, {"error": "internal error", "detail": str(exc)})

    # -- handlers -----------------------------------------------------------

    def _handle_tick(self, payload: dict[str, Any]) -> None:
        """Advance one Tick for a run and return its :class:`TickOutcome`."""
        loop = self._control_loop
        if loop is None:
            self._send_json(503, {"error": "control loop not configured"})
            return

        run_id = payload.get("run_id")
        if not run_id:
            self._send_json(400, {"error": "run_id is required"})
            return

        state = loop.client.read_loop_state(str(run_id))
        if state is None:
            self._send_json(404, {"error": f"run {run_id} not found"})
            return

        thesis = payload.get("thesis")
        outcome = loop.run_tick(state, thesis)
        self._send_json(200, outcome)

    def _handle_authorize_send(self, payload: dict[str, Any]) -> None:
        """Decide a send via the SHARED gate so governance is identical (Req 11.4)."""
        gate = self._gate
        if gate is None:
            self._send_json(503, {"error": "governance gate not configured"})
            return

        draft = _draft_from_payload(payload)
        window = _window_from_payload(payload)
        budget = int(payload.get("budget", 0) or 0)
        sent_count = int(payload.get("sent_count", 0) or 0)

        decision = gate.authorize_send(draft, sent_count, budget, window)
        self._send_json(200, decision)

    def _handle_approve(self, payload: dict[str, Any]) -> None:
        """Mark a draft approved via the shared gate."""
        gate = self._gate
        if gate is None:
            self._send_json(503, {"error": "governance gate not configured"})
            return

        draft_id = payload.get("draft_id")
        investor_id = payload.get("investor_id", "")
        if not draft_id:
            self._send_json(400, {"error": "draft_id is required"})
            return

        result = gate.approve(str(draft_id), str(investor_id))
        status = 200 if getattr(result, "ok", False) else 409
        self._send_json(status, result)


# --- server factory + entrypoint -------------------------------------------


def make_server(
    control_loop: Any,
    gate: Any,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> ThreadingHTTPServer:
    """Build a configured (not-yet-serving) HTTP server.

    Args:
        control_loop: The :class:`ControlLoop` (or a compatible object exposing
            ``client.read_loop_state`` and ``run_tick``) backing ``/tick``.
        gate: The :class:`GovernanceGate` (or compatible) backing
            ``/authorize_send`` and ``/approve``. This MUST be the same gate the
            self-driven loop uses so governance is identical (Requirement 11.4).
        host: Bind address.
        port: Bind port.

    Returns:
        A :class:`ThreadingHTTPServer` ready for ``serve_forever``; the caller
        owns its lifecycle (``serve_forever`` / ``shutdown`` / ``server_close``).
    """
    server = ThreadingHTTPServer((host, port), _GuildHandler)
    # Attach the dependencies the handler reads off ``self.server``.
    server.control_loop = control_loop  # type: ignore[attr-defined]
    server.gate = gate  # type: ignore[attr-defined]
    return server


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Build the default production wiring and serve until interrupted.

    Used by ``python -m angent.loop.server``. Creates a :class:`ControlLoop`
    (which lazily wires its ClickHouse client and shared gate) and serves with
    the loop's own gate so the HTTP path enforces identical governance.
    """
    from .control_loop import ControlLoop

    logging.basicConfig(level=logging.INFO)
    control_loop = ControlLoop()
    gate = control_loop.gate  # same gate the self-driven loop routes sends through
    server = make_server(control_loop, gate, host=host, port=port)
    logger.info("Angent Guild orchestration server listening on http://%s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down Angent Guild orchestration server.")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    import argparse

    parser = argparse.ArgumentParser(description="Angent Guild orchestration HTTP server")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    run_server(args.host, args.port)
