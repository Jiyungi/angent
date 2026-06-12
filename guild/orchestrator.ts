/**
 * Guild Orchestrator (TypeScript)
 * ===============================
 *
 * Optional Guild wrapper around the Angent Python core (Requirement 11).
 *
 * The Python `ControlLoop` runs all six agents on its own with no dependency on
 * Guild. When this orchestrator is deployed it orchestrates the loop by calling
 * the Python backend over HTTP and routes *every* send through the
 * `Governance_Gate` (Req 11.3, 11.4). If the backend / Guild does not respond
 * within 5 seconds, Angent classifies Guild as unavailable so the Python core
 * self-drives the loop and enforces the gate itself (Req 11.5, 11.6, 18.7).
 *
 * This module is intentionally self-contained and standalone — it has no
 * dependency on the Next.js app. It uses the global `fetch` available in
 * Node 18+ / modern runtimes.
 *
 * Endpoints (implemented Python-side in task 16.2):
 *   POST {baseUrl}/tick            -> advance one Tick
 *   POST {baseUrl}/authorize_send  -> Governance_Gate decision for a draft
 */

/** The single decision the Governance_Gate can return for a send. */
export type GateDecision = "PERMIT" | "BLOCK" | "DEFER";

/** Result of asking the gate to authorize a send. */
export interface SendAuthorization {
  decision: GateDecision;
  /** Reason supplied by the gate when the send is not permitted. */
  reason?: string;
}

/** A draft handed to the gate for a send-authorization decision. */
export interface SendRequest {
  draftId: string;
  runId: string;
}

/** Outcome of advancing a single Tick via the Python backend. */
export interface TickResult {
  runId: string;
  tickIndex: number;
  status: "running" | "stopped";
  stopReason?: string | null;
}

/**
 * Sentinel returned (or thrown) when the Guild/backend is classified as
 * unavailable. The caller falls back to letting the Python core self-drive
 * the loop and enforce the Governance_Gate (Req 11.5, 11.6).
 */
export class GuildUnavailableError extends Error {
  readonly guildUnavailable = true as const;
  constructor(
    message: string,
    readonly cause?: unknown,
  ) {
    super(message);
    this.name = "GuildUnavailableError";
  }
}

/** Type guard so callers can branch on the unavailability fallback. */
export function isGuildUnavailable(err: unknown): err is GuildUnavailableError {
  return err instanceof GuildUnavailableError;
}

export interface GuildOrchestratorOptions {
  /** Base URL of the Python backend, e.g. "http://127.0.0.1:8000". */
  baseUrl: string;
  /**
   * Unavailability timeout in milliseconds. Per Requirement 11.6 the Guild is
   * classified as unavailable if the backend does not respond within 5s.
   */
  timeoutMs?: number;
  /** Injectable fetch (for testing); defaults to the global `fetch`. */
  fetchImpl?: typeof fetch;
}

/** Default unavailability threshold: 5 seconds (Requirement 11.6). */
export const GUILD_UNAVAILABLE_TIMEOUT_MS = 5_000;

/**
 * Orchestrates the Angent loop by calling the Python backend over HTTP and
 * routes every send through the Governance_Gate.
 */
export class GuildOrchestrator {
  private readonly baseUrl: string;
  private readonly timeoutMs: number;
  private readonly fetchImpl: typeof fetch;

  constructor(options: GuildOrchestratorOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.timeoutMs = options.timeoutMs ?? GUILD_UNAVAILABLE_TIMEOUT_MS;
    const f = options.fetchImpl ?? globalThis.fetch;
    if (typeof f !== "function") {
      throw new Error(
        "global fetch is not available; pass options.fetchImpl (Node 18+ required)",
      );
    }
    this.fetchImpl = f;
  }

  /**
   * Advance one Tick of the Control_Loop via the Python backend.
   * @throws {GuildUnavailableError} if the backend does not respond within the
   *   5-second timeout, so the caller falls back to the self-driving Python core.
   */
  async advanceTick(runId: string): Promise<TickResult> {
    const body = await this.post<TickResult>("/tick", { runId });
    return body;
  }

  /**
   * Route a send through the Governance_Gate. Returns the gate decision; the
   * caller must only proceed with the send when the decision is PERMIT.
   *
   * @throws {GuildUnavailableError} if the gate does not respond within 5s.
   */
  async authorizeSend(request: SendRequest): Promise<SendAuthorization> {
    return this.post<SendAuthorization>("/authorize_send", request);
  }

  /**
   * Convenience guard enforcing Requirement 11.4: every send is routed through
   * the gate and any send the gate did not PERMIT is rejected. Returns the
   * result of `perform` only when the gate permits; otherwise throws.
   *
   * @throws {GuildUnavailableError} if the gate is unreachable within 5s.
   * @throws {Error} if the gate did not permit the send (BLOCK/DEFER).
   */
  async sendThroughGate<T>(
    request: SendRequest,
    perform: () => Promise<T>,
  ): Promise<T> {
    const auth = await this.authorizeSend(request);
    if (auth.decision !== "PERMIT") {
      throw new Error(
        `send rejected by Governance_Gate: ${auth.decision}` +
          (auth.reason ? ` (${auth.reason})` : ""),
      );
    }
    return perform();
  }

  /**
   * POST JSON to the backend with a 5-second abort. Any timeout, network error,
   * or non-OK response is normalized into a GuildUnavailableError so the Python
   * core can self-drive (Req 11.5, 11.6).
   */
  private async post<T>(path: string, payload: unknown): Promise<T> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const res = await this.fetchImpl(`${this.baseUrl}${path}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
        signal: controller.signal,
      });
      if (!res.ok) {
        throw new GuildUnavailableError(
          `backend ${path} responded with HTTP ${res.status}`,
        );
      }
      return (await res.json()) as T;
    } catch (err) {
      if (err instanceof GuildUnavailableError) {
        throw err;
      }
      // AbortError (timeout) or any network failure => Guild unavailable.
      const aborted =
        err instanceof Error &&
        (err.name === "AbortError" || controller.signal.aborted);
      throw new GuildUnavailableError(
        aborted
          ? `Guild backend did not respond within ${this.timeoutMs}ms; classified as unavailable`
          : `Guild backend request to ${path} failed; classified as unavailable`,
        err,
      );
    } finally {
      clearTimeout(timer);
    }
  }
}
