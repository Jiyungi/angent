"use client";

// OpenUI_Surface — On-demand company deep-dive (Requirement 14).
//
// This is an OpenUI-GENERATED surface, NOT plain React markup. When the investor
// opens a company, the deep-dive view is generated at runtime via OpenUI Lang
// and rendered by the prebuilt layout's internal `<Renderer/>` (reusing the
// `library.prompt()` -> `/api/chat` -> `<Renderer/>` pipeline). The view is
// generated from the same ClickHouse company data the Publisher consumes.
//
// Every rendered element comes from the generation step (Req 14.5) and each
// generated view exposes at least one investor input/action element (Req 14.6).
// Generation is bound to a 5s budget (Req 14.3); on failure/timeout the prior
// view is retained and an error indication is shown (Req 14.4).

import "@openuidev/react-ui/components.css";
import "@openuidev/react-ui/styles/index.css";

import { useCallback, useMemo, useState } from "react";
import { openAIReadableStreamAdapter } from "@openuidev/react-headless";
import { FullScreen } from "@openuidev/react-ui";
import { openuiLibrary } from "@openuidev/react-ui/genui-lib";

import {
  buildDeepDiveSystemPrompt,
  createBudgetedProcessMessage,
  type DeepDiveCompany,
  type GenerationState,
} from "./openuiSurface";

import { SurfaceErrorBanner } from "./SurfaceErrorBanner";

export interface CompanyDeepDiveProps {
  /** The company to deep-dive on (typically selected from a React_Shell card). */
  company?: DeepDiveCompany;
}

/**
 * Runtime-generated, on-demand company deep-dive surface.
 *
 * The conversation starter acts as the "open" action: selecting it triggers a
 * budget-bound OpenUI Lang generation for the selected company.
 */
export function CompanyDeepDive({ company }: CompanyDeepDiveProps) {
  const [gen, setGen] = useState<GenerationState>({ status: "idle" });

  const systemPrompt = useMemo(() => buildDeepDiveSystemPrompt(company), [company]);

  const processMessage = useMemo(
    () => createBudgetedProcessMessage(() => systemPrompt, setGen),
    [systemPrompt],
  );

  const dismissError = useCallback(() => setGen({ status: "idle" }), []);

  const companyName = company?.name ?? "the selected company";

  return (
    <div className="relative h-full w-full">
      <SurfaceErrorBanner state={gen} onDismiss={dismissError} />
      {/* Stays mounted so the prior generated deep-dive view is retained on
          a failed/timed-out regeneration. */}
      <FullScreen
        processMessage={processMessage}
        streamProtocol={openAIReadableStreamAdapter()}
        componentLibrary={openuiLibrary}
        agentName="Angent · Company Deep-Dive"
        welcomeMessage={{
          title: `Deep-dive: ${companyName}`,
          description:
            "Open a company to generate an at-a-glance deep-dive with its fit score, reasoning, signals, and next actions.",
        }}
        conversationStarters={{
          variant: "short",
          options: [
            {
              displayText: `Open deep-dive for ${companyName}`,
              prompt: `Generate the deep-dive view for ${companyName}.`,
            },
            {
              displayText: "Why is this a fit?",
              prompt: `Explain the thesis fit for ${companyName} and show the supporting signals.`,
            },
            {
              displayText: "Draft outreach action",
              prompt: `Show ${companyName}'s deep-dive with an action to draft outreach.`,
            },
          ],
        }}
      />
    </div>
  );
}

export default CompanyDeepDive;
