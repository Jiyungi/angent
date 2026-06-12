// React_Shell (plain React, Impeccable-polishable, NOT OpenUI-generated)
// Plain TSX dashboard component. Contains NO OpenUI Lang markup and no <Renderer/>.
// Safe for the Impeccable polish pass (Requirement 15).
"use client";

export interface QualifiedCompanyCardProps {
  /** Company name. */
  name: string;
  /** Real source URL (GitHub repo or Hacker News post). */
  url: string;
  /** Originating Signal_Source attribution (e.g. "GitHub", "Hacker News"). */
  source: string;
  /** Numeric fit score in the inclusive range 0-100. */
  fitScore: number;
  /** Natural-language explanation of the fit against the Thesis. */
  fitExplanation: string;
  /** Signals that drove qualification (stars, launch, commits, etc.). */
  signals: string[];
}

function scoreTone(score: number): string {
  if (score >= 75) return "bg-emerald-100 text-emerald-800 ring-emerald-600/20";
  if (score >= 50) return "bg-amber-100 text-amber-800 ring-amber-600/20";
  return "bg-rose-100 text-rose-800 ring-rose-600/20";
}

export default function QualifiedCompanyCard({
  name,
  url,
  source,
  fitScore,
  fitExplanation,
  signals,
}: QualifiedCompanyCardProps) {
  const clamped = Math.max(0, Math.min(100, fitScore));

  return (
    <article className="flex flex-col gap-3 rounded-xl border border-gray-200 bg-white p-4 shadow-sm transition hover:shadow-md">
      <header className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="truncate text-base font-semibold text-gray-900">{name}</h3>
          <a
            href={url}
            target="_blank"
            rel="noreferrer noopener"
            className="block truncate text-sm text-blue-600 hover:underline"
          >
            {url}
          </a>
        </div>
        <span
          className={`shrink-0 rounded-full px-2.5 py-1 text-sm font-semibold ring-1 ring-inset ${scoreTone(clamped)}`}
          title="Fit score (0-100)"
        >
          {clamped}
        </span>
      </header>

      <div className="flex items-center gap-2 text-xs text-gray-500">
        <span className="rounded bg-gray-100 px-2 py-0.5 font-medium text-gray-600">{source}</span>
      </div>

      <p className="text-sm leading-relaxed text-gray-700">{fitExplanation}</p>

      {signals.length > 0 && (
        <ul className="flex flex-wrap gap-1.5">
          {signals.map((signal, i) => (
            <li
              key={`${signal}-${i}`}
              className="rounded-md bg-indigo-50 px-2 py-0.5 text-xs font-medium text-indigo-700"
            >
              {signal}
            </li>
          ))}
        </ul>
      )}
    </article>
  );
}
