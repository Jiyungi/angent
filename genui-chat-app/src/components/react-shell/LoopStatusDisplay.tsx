// React_Shell (plain React, Impeccable-polishable, NOT OpenUI-generated)
// Plain TSX dashboard component. Contains NO OpenUI Lang markup and no <Renderer/>.
// Safe for the Impeccable polish pass (Requirement 15).
"use client";

export type LoopStatus = "running" | "stopped";

export type LoopStopReason = "goal-met" | "deadline-reached" | "email-budget-exhausted" | null;

export interface LoopStatusDisplayProps {
  /** Index of the current Tick in the Control_Loop. */
  tickIndex: number;
  /** Number of emails sent so far. */
  emailsSent: number;
  /** Hard email budget cap. */
  budget: number;
  /** Reply rate as a fraction in [0, 1]. */
  replyRate: number;
  /** Whether the loop is running or stopped. */
  status: LoopStatus;
  /** Termination reason when stopped, otherwise null. */
  stopReason: LoopStopReason;
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-xs uppercase tracking-wide text-gray-400">{label}</span>
      <span className="text-lg font-semibold text-gray-900">{value}</span>
    </div>
  );
}

const STOP_REASON_LABELS: Record<NonNullable<LoopStopReason>, string> = {
  "goal-met": "Goal met",
  "deadline-reached": "Deadline reached",
  "email-budget-exhausted": "Email budget exhausted",
};

export default function LoopStatusDisplay({
  tickIndex,
  emailsSent,
  budget,
  replyRate,
  status,
  stopReason,
}: LoopStatusDisplayProps) {
  const isRunning = status === "running";
  const safeBudget = Math.max(0, budget);
  const budgetPct = safeBudget > 0 ? Math.min(100, (emailsSent / safeBudget) * 100) : 0;
  const replyPct = Math.max(0, Math.min(100, replyRate * 100));

  return (
    <section className="flex flex-col gap-4 rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
      <header className="flex items-center justify-between">
        <h3 className="text-base font-semibold text-gray-900">Loop Status</h3>
        <span
          className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold ring-1 ring-inset ${
            isRunning
              ? "bg-emerald-100 text-emerald-800 ring-emerald-600/20"
              : "bg-gray-100 text-gray-600 ring-gray-400/20"
          }`}
        >
          <span
            className={`h-2 w-2 rounded-full ${isRunning ? "bg-emerald-500 animate-pulse" : "bg-gray-400"}`}
          />
          {isRunning ? "Running" : "Stopped"}
        </span>
      </header>

      <div className="grid grid-cols-3 gap-4">
        <Stat label="Tick" value={String(tickIndex)} />
        <Stat label="Emails Sent" value={`${emailsSent} / ${safeBudget}`} />
        <Stat label="Reply Rate" value={`${replyPct.toFixed(1)}%`} />
      </div>

      <div className="flex flex-col gap-2">
        <div>
          <div className="mb-1 flex justify-between text-xs text-gray-500">
            <span>Email budget</span>
            <span>{budgetPct.toFixed(0)}%</span>
          </div>
          <div className="h-2 w-full overflow-hidden rounded-full bg-gray-100">
            <div className="h-full rounded-full bg-blue-500" style={{ width: `${budgetPct}%` }} />
          </div>
        </div>
        <div>
          <div className="mb-1 flex justify-between text-xs text-gray-500">
            <span>Reply rate</span>
            <span>{replyPct.toFixed(1)}%</span>
          </div>
          <div className="h-2 w-full overflow-hidden rounded-full bg-gray-100">
            <div className="h-full rounded-full bg-indigo-500" style={{ width: `${replyPct}%` }} />
          </div>
        </div>
      </div>

      {!isRunning && stopReason && (
        <p className="rounded-lg bg-amber-50 px-3 py-2 text-sm font-medium text-amber-800">
          Stopped: {STOP_REASON_LABELS[stopReason]}
        </p>
      )}
    </section>
  );
}
