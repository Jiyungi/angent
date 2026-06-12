"use client";

// Angent — home dashboard.
//
// Two surfaces, switchable via tabs:
//   • "Dashboard" — the React_Shell (plain React) loop-status + qualified-company
//     cards + drafted-email preview. Renders reliably from representative data,
//     independent of any LLM gateway state (Requirement 15, 16).
//   • "Thesis Chat" — the OpenUI_Surface generated at runtime via OpenUI Lang
//     (Requirement 14), reusing the library.prompt() -> /api/chat -> <Renderer/>
//     pipeline.

import { useCallback, useEffect, useState } from "react";

import LoopStatusDisplay from "../components/react-shell/LoopStatusDisplay";
import QualifiedCompanyCard from "../components/react-shell/QualifiedCompanyCard";
import DraftedEmailPreview from "../components/react-shell/DraftedEmailPreview";

// --- Representative demo data (shape matches the ClickHouse `companies` /
// `emails` / `loop_state` rows the Python core produces). ---------------------

const DEMO_THESIS =
  "Early-stage developer-tools and AI-infrastructure startups: open-source " +
  "frameworks, LLM agent tooling, data/ML platforms, and API-first products " +
  "that help engineers ship faster.";

// --- ClickHouse signals (JSON string) -> a few readable chips ---------------
function signalsToChips(raw: unknown): string[] {
  if (!raw) return [];
  let obj: Record<string, unknown> | null = null;
  if (typeof raw === "string") {
    try {
      obj = JSON.parse(raw);
    } catch {
      return raw ? [String(raw)] : [];
    }
  } else if (typeof raw === "object") {
    obj = raw as Record<string, unknown>;
  }
  if (!obj) return [];
  const label: Record<string, string> = {
    points: "points",
    num_comments: "comments",
    stars: "stars",
    commits: "commits",
    forks: "forks",
    downloads: "downloads",
    likes: "likes",
    language: "",
    pipeline_tag: "",
    kind: "",
  };
  const chips: string[] = [];
  for (const [k, v] of Object.entries(obj)) {
    if (v === null || v === undefined || v === "") continue;
    if (!(k in label)) continue;
    chips.push(label[k] ? `${v} ${label[k]}` : String(v));
    if (chips.length >= 4) break;
  }
  return chips;
}

interface ApiCompany {
  source?: string;
  name?: string;
  url?: string;
  fit_score?: number;
  fit_explanation?: string;
  signals?: unknown;
}
interface ApiLoopState {
  tick_index?: number;
  emails_sent?: number;
  budget?: number;
  reply_rate?: number;
  status?: string;
  stop_reason?: string | null;
}
interface ApiDraft {
  subject?: string;
  body?: string;
  approved?: number;
  sent?: number;
}
interface DashboardData {
  source: string;
  companies: ApiCompany[];
  loopState: ApiLoopState | null;
  draft: ApiDraft | null;
}

export default function Home() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [live, setLive] = useState(false);
  const [running, setRunning] = useState(false);
  const [thesis, setThesis] = useState(DEMO_THESIS);

  const loadData = useCallback(async () => {
    try {
      const r = await fetch("/api/dashboard", { cache: "no-store" });
      const d = (await r.json()) as DashboardData & { ok: boolean };
      if (d.ok && Array.isArray(d.companies) && d.companies.length > 0) {
        setData(d);
        setLive(true);
      }
    } catch {
      /* keep demo fallback */
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const runAgent = useCallback(async () => {
    setRunning(true);
    try {
      await fetch("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ thesis }),
      });
    } catch {
      /* ignore */
    }
    // Poll the blackboard so new rows appear live while the agent runs.
    let n = 0;
    const id = setInterval(async () => {
      await loadData();
      n += 1;
      if (n >= 30) {
        clearInterval(id);
        setRunning(false);
      }
    }, 2000);
  }, [loadData]);

  // Only real rows from ClickHouse — no mock fallback.
  const companies = data
    ? data.companies.map((c) => ({
        name: c.name || "(unnamed)",
        url: c.url || "#",
        source:
          c.source === "github"
            ? "GitHub"
            : c.source === "hackernews"
            ? "Hacker News"
            : c.source === "huggingface"
            ? "Hugging Face"
            : c.source || "source",
        fitScore: Number(c.fit_score ?? 0),
        fitExplanation:
          c.fit_explanation && c.fit_explanation !== ""
            ? c.fit_explanation
            : "Scored by the heuristic scorer; explanation pending the LLM gateway.",
        signals: signalsToChips(c.signals),
      }))
    : [];

  const loop =
    data && data.loopState
      ? {
          tickIndex: Number(data.loopState.tick_index ?? 0),
          emailsSent: Number(data.loopState.emails_sent ?? 0),
          budget: Number(data.loopState.budget ?? 8),
          replyRate: Number(data.loopState.reply_rate ?? 0),
          status: (data.loopState.status === "stopped" ? "stopped" : "running") as
            | "running"
            | "stopped",
          stopReason: (data.loopState.stop_reason || null) as
            | "goal-met"
            | "deadline-reached"
            | "email-budget-exhausted"
            | null,
        }
      : null;

  const draft =
    data && data.draft
      ? {
          subject: data.draft.subject || "(no subject)",
          body: data.draft.body || "",
          approved: Number(data.draft.approved ?? 0) === 1,
          sent: Number(data.draft.sent ?? 0) === 1,
        }
      : null;

  return (
    <div className="flex h-screen w-screen flex-col overflow-hidden bg-linear-to-b from-slate-50 to-slate-100 text-slate-900">
      {/* Header */}
      <header className="flex items-center justify-between border-b border-slate-200/80 bg-white/80 px-6 py-3 backdrop-blur">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-linear-to-br from-indigo-500 to-emerald-500 text-sm font-black text-white shadow-sm">
            A
          </div>
          <div className="leading-tight">
            <div className="text-base font-bold tracking-tight text-slate-900">Angent</div>
            <div className="hidden text-xs text-slate-500 sm:block">
              self-improving deal sourcing for angel investors
            </div>
          </div>
        </div>
        <nav className="flex items-center gap-3">
          <span
            className={`hidden items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold ring-1 ring-inset sm:inline-flex ${
              live
                ? "bg-emerald-50 text-emerald-700 ring-emerald-600/20"
                : "bg-slate-100 text-slate-500 ring-slate-400/20"
            }`}
            title={live ? "Reading live rows from ClickHouse" : "Awaiting a run — no data loaded yet"}
          >
            <span className={`h-1.5 w-1.5 rounded-full ${live ? "bg-emerald-500" : "bg-slate-400"}`} />
            {live ? "live · ClickHouse" : "awaiting run"}
          </span>
        </nav>
      </header>

      {/* Body */}
      <main className="min-h-0 flex-1 overflow-auto">
        <div className="mx-auto flex max-w-6xl flex-col gap-6 p-6">
            {/* Thesis + Run */}
            <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm">
              <div className="flex flex-col gap-4 p-5 sm:flex-row sm:items-stretch">
                <div className="flex-1">
                  <label className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                    Investment thesis
                  </label>
                  <textarea
                    value={thesis}
                    onChange={(e) => setThesis(e.target.value)}
                    rows={3}
                    className="mt-2 w-full resize-none rounded-xl border border-slate-200 bg-slate-50 p-3 text-sm leading-relaxed text-slate-800 transition focus:border-emerald-400 focus:bg-white focus:outline-none focus:ring-4 focus:ring-emerald-100"
                    placeholder="Describe the startups you want to back…"
                  />
                  <p className="mt-2 text-xs text-slate-400">
                    Scans GitHub &amp; Hacker News (last 90 days), then scores each startup against this thesis.
                  </p>
                </div>
                <div className="flex shrink-0 flex-col items-stretch justify-center sm:w-44">
                  <button
                    onClick={runAgent}
                    disabled={running}
                    className={`flex items-center justify-center gap-2 rounded-xl px-4 py-3 text-sm font-bold text-white shadow-md transition ${
                      running
                        ? "cursor-wait bg-emerald-500/80"
                        : "bg-linear-to-br from-indigo-600 to-emerald-600 hover:from-indigo-500 hover:to-emerald-500 hover:shadow-lg"
                    }`}
                  >
                    {running ? (
                      <>
                        <span className="h-2 w-2 animate-pulse rounded-full bg-white" />
                        Sourcing deals…
                      </>
                    ) : (
                      <>▶ Run Agent</>
                    )}
                  </button>
                  <p className="mt-2 text-center text-[11px] text-slate-400">
                    {running ? "watching results populate" : "one click — no terminal"}
                  </p>
                </div>
              </div>
            </div>

            {loop && (
              <LoopStatusDisplay
                tickIndex={loop.tickIndex}
                emailsSent={loop.emailsSent}
                budget={loop.budget}
                replyRate={loop.replyRate}
                status={loop.status}
                stopReason={loop.stopReason}
              />
            )}

            <section className="flex flex-col gap-3">
              <div className="flex items-center gap-2">
                <h2 className="text-base font-bold tracking-tight text-slate-900">
                  Qualified companies
                </h2>
                <span className="rounded-full bg-indigo-50 px-2 py-0.5 text-xs font-semibold text-indigo-600">
                  {companies.length}
                </span>
              </div>
              {companies.length > 0 ? (
                <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
                  {companies.map((c) => (
                    <QualifiedCompanyCard key={c.url} {...c} />
                  ))}
                </div>
              ) : (
                <div className="flex flex-col items-center justify-center gap-2 rounded-2xl border border-dashed border-slate-300 bg-white/60 p-10 text-center">
                  <p className="text-sm font-medium text-slate-600">
                    {running ? "Sourcing and scoring startups…" : "No deals yet"}
                  </p>
                  <p className="text-xs text-slate-400">
                    {running
                      ? "Results will appear here as the agent qualifies companies."
                      : "Set your thesis above and click Run Agent to source live deals."}
                  </p>
                </div>
              )}
            </section>

            {draft && (
              <section className="flex flex-col gap-3">
                <h2 className="text-base font-bold tracking-tight text-slate-900">
                  Drafted outreach <span className="font-normal text-slate-400">· awaiting your approval</span>
                </h2>
                <DraftedEmailPreview {...draft} />
              </section>
            )}

            <div className="pb-2 text-center text-xs text-slate-400">
              Powered by ClickHouse · TrueFoundry · Langfuse · Airbyte · x402
            </div>
          </div>
      </main>
    </div>
  );
}
