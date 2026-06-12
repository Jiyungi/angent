// Shared helpers for the OpenUI_Surface (Requirement 14).
//
// These surfaces are GENERATED AT RUNTIME via OpenUI Lang. Every rendered
// element in the conversation area is produced by the OpenUI Lang generation
// step (Req 14.5) and rendered through the OpenUI `<Renderer/>` that the
// prebuilt `@openuidev/react-ui` chat layouts drive internally. We reuse the
// existing `genui-chat-app` pipeline exactly:
//
//     openuiLibrary.prompt(openuiPromptOptions)  ->  /api/chat  ->  <Renderer/>
//
// On top of that pipeline we add a hard 5-second generation budget per request
// (Req 14.1 / 14.3) and surface an error indication while retaining the last
// successfully rendered state (Req 14.2 / 14.4).

import { openAIMessageFormat } from "@openuidev/react-headless";
import { openuiLibrary, openuiPromptOptions } from "@openuidev/react-ui/genui-lib";
import type { Message } from "@openuidev/react-headless";

/** Per-generation budget in milliseconds (Req 14.1, 14.3, 18.8). */
export const GENERATION_BUDGET_MS = 5000;

/**
 * Instructions appended to the auto-generated OpenUI Lang system prompt so the
 * generation step satisfies the OpenUI_Surface acceptance criteria:
 *  - every view is fully generated as OpenUI Lang (Req 14.5), and
 *  - every view contains at least one input/action element (Req 14.6).
 */
const SURFACE_RULES = [
  "You are Angent's generative UI surface for a solo angel investor.",
  "Render the ENTIRE response as OpenUI Lang (never plain prose or markdown).",
  "The first statement MUST assign to `root`. Generate top-down: layout, then components, then data.",
  "Every visible element MUST be produced by this generation step. Do not assume any element exists outside of what you generate.",
  "CRITICAL: Each view you generate MUST include at least one interactive element that accepts investor input or triggers an investor-initiated action (for example an Input, TextArea, Select, RadioGroup, Slider, or a Button). Never produce a read-only view.",
].join("\n");

/** Build the thesis-refinement chat system prompt from the shared library prompt. */
export function buildThesisChatSystemPrompt(): string {
  const base = openuiLibrary.prompt(openuiPromptOptions);
  return [
    base,
    "\n---\n",
    SURFACE_RULES,
    "\nSurface: THESIS-REFINEMENT CHAT.",
    "Help the investor iteratively sharpen an investment thesis (sectors, stage, geography, signals, anti-patterns).",
    "Always render a way for the investor to keep refining: e.g. a TextArea or Input for the next thesis edit plus action Buttons for common refinements (broaden, narrow, add constraint).",
  ].join("\n");
}

/** Minimal shape of the company data a deep-dive renders (mirrors ClickHouse columns). */
export interface DeepDiveCompany {
  name: string;
  url?: string;
  source?: string;
  fit_score?: number;
  fit_explanation?: string;
  signals?: Record<string, unknown>;
}

/**
 * Build the on-demand deep-dive system prompt. The concrete company data is
 * embedded so the generation step renders that company's deep-dive view; the
 * generated view still owns every element (Req 14.5) and must expose at least
 * one input/action element (Req 14.6).
 */
export function buildDeepDiveSystemPrompt(company?: DeepDiveCompany): string {
  const base = openuiLibrary.prompt(openuiPromptOptions);
  const companyJson = company ? JSON.stringify(company, null, 2) : "(no company selected yet)";
  return [
    base,
    "\n---\n",
    SURFACE_RULES,
    "\nSurface: COMPANY DEEP-DIVE.",
    "Generate an at-a-glance deep-dive for the selected company using ONLY the data provided below.",
    "Show the fit score, the fit explanation, the source/provenance link, and the raw signals in a readable layout.",
    "Always include at least one investor action (for example a Button to draft outreach, mark a follow-up, or request a re-score, or an Input/TextArea to capture an investor note).",
    "\nCompany data (JSON):\n",
    companyJson,
  ].join("\n");
}

/** Status of the current/last generation attempt for a surface. */
export type GenerationStatus = "idle" | "generating" | "ok" | "error" | "timeout";

export interface GenerationState {
  status: GenerationStatus;
  /** Human-readable error indication shown to the investor (Req 14.2, 14.4). */
  error?: string;
}

export interface BudgetedProcessMessageArgs {
  threadId: string;
  messages: Message[];
  abortController: AbortController;
}

/**
 * Wrap the existing `/api/chat` call with a hard generation budget.
 *
 * Reuses the pipeline exactly: it POSTs the `library.prompt()`-derived system
 * prompt plus the conversation to `/api/chat`, whose streamed OpenUI Lang is
 * rendered by the prebuilt layout's `<Renderer/>`.
 *
 * If the request does not begin streaming within {@link GENERATION_BUDGET_MS}
 * (or otherwise fails), the in-flight request is aborted, an error/timeout
 * status is reported via {@link onStatus}, and the rejection propagates so the
 * chat layout keeps its last successfully rendered state (Req 14.2, 14.4).
 */
export function createBudgetedProcessMessage(
  getSystemPrompt: () => string,
  onStatus: (state: GenerationState) => void,
  budgetMs: number = GENERATION_BUDGET_MS,
) {
  return async ({ messages, abortController }: BudgetedProcessMessageArgs): Promise<Response> => {
    onStatus({ status: "generating" });

    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      abortController.abort();
    }, budgetMs);

    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          systemPrompt: getSystemPrompt(),
          messages: openAIMessageFormat.toApi(messages),
        }),
        signal: abortController.signal,
      });

      if (!response.ok) {
        onStatus({
          status: "error",
          error: `Generation did not complete (server responded ${response.status}). Showing the last rendered view.`,
        });
        return response;
      }

      // Headers received within budget: the OpenUI Lang stream renders live.
      onStatus({ status: "ok" });
      return response;
    } catch (err) {
      if (timedOut) {
        onStatus({
          status: "timeout",
          error: `Generation exceeded the ${Math.round(budgetMs / 1000)}s budget. Showing the last successfully rendered view.`,
        });
      } else {
        const message = err instanceof Error ? err.message : "Unknown error";
        onStatus({
          status: "error",
          error: `Generation did not complete (${message}). Showing the last successfully rendered view.`,
        });
      }
      throw err;
    } finally {
      clearTimeout(timer);
    }
  };
}
