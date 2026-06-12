"use client";

// OpenUI_Surface — Thesis-refinement chat (Requirement 14).
//
// This is an OpenUI-GENERATED surface, NOT plain React markup. The conversation
// content is generated at runtime via OpenUI Lang and rendered by the prebuilt
// `<FullScreen/>` layout's internal `<Renderer/>` (the same pipeline as
// `src/app/page.tsx`). Every rendered element in the conversation comes from the
// generation step (Req 14.5) and each generated view includes at least one
// input/action element (Req 14.6), enforced via the surface system prompt.
//
// A hard 5s generation budget is applied per message (Req 14.1); on failure or
// timeout the last successfully rendered state is retained and an error
// indication is shown (Req 14.2).

import "@openuidev/react-ui/components.css";
import "@openuidev/react-ui/styles/index.css";

import { useCallback, useMemo, useState } from "react";
import { openAIReadableStreamAdapter } from "@openuidev/react-headless";
import { FullScreen } from "@openuidev/react-ui";
import { openuiLibrary } from "@openuidev/react-ui/genui-lib";

import {
  buildThesisChatSystemPrompt,
  createBudgetedProcessMessage,
  type GenerationState,
} from "./openuiSurface";

import { SurfaceErrorBanner } from "./SurfaceErrorBanner";

export interface ThesisChatProps {
  /** Optional seed thesis to anchor the refinement conversation. */
  initialThesis?: string;
}

/**
 * Conversational, runtime-generated thesis-refinement surface.
 */
export function ThesisChat({ initialThesis }: ThesisChatProps) {
  const [gen, setGen] = useState<GenerationState>({ status: "idle" });

  const systemPrompt = useMemo(() => {
    const base = buildThesisChatSystemPrompt();
    return initialThesis ? `${base}\n\nCurrent working thesis:\n${initialThesis}` : base;
  }, [initialThesis]);

  // Budget-bound /api/chat call: reuses library.prompt() -> /api/chat -> Renderer.
  const processMessage = useMemo(
    () => createBudgetedProcessMessage(() => systemPrompt, setGen),
    [systemPrompt],
  );

  const dismissError = useCallback(() => setGen({ status: "idle" }), []);

  return (
    <div className="relative h-full w-full">
      <SurfaceErrorBanner state={gen} onDismiss={dismissError} />
      {/* The chat layout stays mounted so the last successfully rendered
          (generated) state is retained even when a regeneration fails. */}
      <FullScreen
        processMessage={processMessage}
        streamProtocol={openAIReadableStreamAdapter()}
        componentLibrary={openuiLibrary}
        agentName="Angent · Thesis Refinement"
        welcomeMessage={{
          title: "Refine your investment thesis",
          description:
            "Describe the startups you want to back. I'll generate a tailored refinement UI you can iterate on.",
        }}
        conversationStarters={{
          variant: "short",
          options: [
            { displayText: "Sharpen my thesis", prompt: "Help me sharpen my investment thesis." },
            { displayText: "Broaden the scope", prompt: "Broaden my thesis to adjacent sectors." },
            { displayText: "Add a constraint", prompt: "Add a stage and geography constraint to my thesis." },
          ],
        }}
      />
    </div>
  );
}

export default ThesisChat;
