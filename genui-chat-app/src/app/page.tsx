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

import { useState } from "react";

import LoopStatusDisplay from "../components/react-shell/LoopStatusDisplay";
import QualifiedCompanyCard from "../components/react-shell/QualifiedCompanyCard";
import DraftedEmailPreview from "../components/react-shell/DraftedEmailPreview";
import ThesisChat from "../components/openui/ThesisChat";

// --- Representative demo data (shape matches the ClickHouse `companies` /
// `emails` / `loop_state` rows the Python core produces). ---------------------

const DEMO_THESIS =
  "Early-stage developer-tools and AI-infrastructure startups: open-source " +
  "frameworks, LLM agent tooling, data/ML platforms, and API-first products " +
  "that help engineers ship faster.";

const COMPANIES = [
  {
    name: "vectorforge",
    url: "https://github.com/vectorforge/vectorforge",
    source: "GitHub",
    fitScore: 91,
    fitExplanation:
      "High-velocity open-source vector database with strong early stargazer " +
      "growth — squarely matches the AI-infrastructure thesis.",
    signals: ["1.8k stars", "312 commits/90d", "OSS"],
  },
  {
    name: "Show HN: Orchestra — typed LLM agent runtime",
    url: "https://news.ycombinator.com/item?id=48509968",
    source: "Hacker News",
    fitScore: 84,
    fitExplanation:
      "Launch HN for a typed agent-orchestration runtime; developer-first and " +
      "API-driven, aligned with the LLM agent-tooling part of the thesis.",
    signals: ["247 points", "96 comments", "Launch HN"],
  },
  {
    name: "lakehouse-rs",
    url: "https://github.com/lakehouse-rs/lakehouse",
    source: "GitHub",
    fitScore: 76,
    fitExplanation:
      "Rust data-lake engine targeting ML feature pipelines — fits the data/ML " +
      "platform angle, though earlier-stage traction than the top pick.",
    signals: ["640 stars", "Rust", "data/ML"],
  },
];

const DRAFT = {
  subject: "Congrats on the vectorforge launch — quick question on traction",
  body:
    "Hi there,\n\nI saw vectorforge cross 1.8k stars in under three months — " +
    "impressive velocity for an open-source vector DB. I invest in early-stage " +
    "AI-infrastructure and dev-tools companies and would love to hear how you're " +
    "thinking about the commercial layer.\n\nWould you be open to a short call " +
    "next week?\n\nBest,\nAngent (on behalf of the investor)",
  approved: true,
  sent: false,
};

type Tab = "dashboard" | "thesis";

export default function Home() {
  const [tab, setTab] = useState<Tab>("dashboard");

  return (
    <div className="flex h-screen w-screen flex-col overflow-hidden bg-gray-50">
      {/* Header */}
      <header className="flex items-center justify-between border-b border-gray-200 bg-white px-6 py-3">
        <div className="flex items-center gap-3">
          <span className="text-lg font-bold text-gray-900">Angent</span>
          <span className="hidden text-sm text-gray-500 sm:inline">
            self-improving deal sourcing
          </span>
        </div>
        <nav className="flex gap-1 rounded-lg bg-gray-100 p-1 text-sm">
          <button
            onClick={() => setTab("dashboard")}
            className={`rounded-md px-3 py-1.5 font-medium transition ${
              tab === "dashboard"
                ? "bg-white text-gray-900 shadow-sm"
                : "text-gray-500 hover:text-gray-800"
            }`}
          >
            Dashboard
          </button>
          <button
            onClick={() => setTab("thesis")}
            className={`rounded-md px-3 py-1.5 font-medium transition ${
              tab === "thesis"
                ? "bg-white text-gray-900 shadow-sm"
                : "text-gray-500 hover:text-gray-800"
            }`}
          >
            Thesis Chat (OpenUI)
          </button>
        </nav>
      </header>

      {/* Body */}
      <main className="min-h-0 flex-1 overflow-auto">
        {tab === "dashboard" ? (
          <div className="mx-auto flex max-w-6xl flex-col gap-6 p-6">
            <div className="rounded-xl border border-gray-200 bg-white p-4">
              <p className="text-xs uppercase tracking-wide text-gray-400">Thesis</p>
              <p className="mt-1 text-sm text-gray-700">{DEMO_THESIS}</p>
            </div>

            <LoopStatusDisplay
              tickIndex={4}
              emailsSent={3}
              budget={8}
              replyRate={0.18}
              status="running"
              stopReason={null}
            />

            <section className="flex flex-col gap-3">
              <h2 className="text-sm font-semibold text-gray-900">
                Qualified companies
              </h2>
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
                {COMPANIES.map((c) => (
                  <QualifiedCompanyCard key={c.url} {...c} />
                ))}
              </div>
            </section>

            <section className="flex flex-col gap-3">
              <h2 className="text-sm font-semibold text-gray-900">
                Drafted email (awaiting send)
              </h2>
              <DraftedEmailPreview {...DRAFT} />
            </section>
          </div>
        ) : (
          <div className="h-full w-full">
            <ThesisChat initialThesis={DEMO_THESIS} />
          </div>
        )}
      </main>
    </div>
  );
}
