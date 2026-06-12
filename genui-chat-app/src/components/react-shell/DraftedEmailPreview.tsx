// React_Shell (plain React, Impeccable-polishable, NOT OpenUI-generated)
// Plain TSX dashboard component. Contains NO OpenUI Lang markup and no <Renderer/>.
// Safe for the Impeccable polish pass (Requirement 15).
"use client";

export interface DraftedEmailPreviewProps {
  /** Email subject line. */
  subject: string;
  /** Email body prose. */
  body: string;
  /** Whether the draft has passed the human-approval Governance_Gate. */
  approved: boolean;
  /** Whether the email has been sent. */
  sent: boolean;
}

function StatusBadge({ label, active, activeClass }: { label: string; active: boolean; activeClass: string }) {
  return (
    <span
      className={`rounded-full px-2.5 py-1 text-xs font-semibold ring-1 ring-inset ${
        active ? activeClass : "bg-gray-100 text-gray-500 ring-gray-400/20"
      }`}
    >
      {label}
    </span>
  );
}

export default function DraftedEmailPreview({ subject, body, approved, sent }: DraftedEmailPreviewProps) {
  return (
    <article className="flex flex-col gap-3 rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
      <header className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-xs uppercase tracking-wide text-gray-400">Subject</p>
          <h3 className="truncate text-base font-semibold text-gray-900">{subject}</h3>
        </div>
        <div className="flex shrink-0 gap-1.5">
          <StatusBadge
            label={approved ? "Approved" : "Unapproved"}
            active={approved}
            activeClass="bg-emerald-100 text-emerald-800 ring-emerald-600/20"
          />
          <StatusBadge
            label={sent ? "Sent" : "Unsent"}
            active={sent}
            activeClass="bg-blue-100 text-blue-800 ring-blue-600/20"
          />
        </div>
      </header>

      <div className="rounded-lg bg-gray-50 p-3">
        <p className="whitespace-pre-wrap text-sm leading-relaxed text-gray-700">{body}</p>
      </div>
    </article>
  );
}
