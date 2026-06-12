// Component classification manifest (Requirement 15.2, 15.3, 15.4, 15.5).
//
// SINGLE SOURCE OF TRUTH for whether each rendered component is an
// OpenUI-GENERATED surface (produced at runtime via OpenUI Lang and rendered
// through `<Renderer/>`) or a PLAIN React_Shell component (statically authored
// TSX with no OpenUI Lang markup).
//
// Why this file exists:
//   - Req 15.2: each rendered component must be unambiguously identifiable as
//     EXACTLY ONE of the two categories. This manifest enumerates every
//     component and assigns it a single category, and `assertUnambiguous()`
//     proves the partition is mutually exclusive and total at runtime.
//   - Req 15.3 / 15.4 / 15.5: the Impeccable polish pass (`npx impeccable
//     detect`) must touch ONLY React_Shell components and never the
//     OpenUI-generated surfaces. {@link IMPECCABLE_SCOPE} encodes the
//     include/exclude globs that the `impeccable` npm script and the
//     `.impeccableignore` file use to scope the pass.
//
// Directory convention reinforces the manifest:
//   src/components/openui/      -> "openui-generated"  (NEVER polished)
//   src/components/react-shell/ -> "react-shell"       (Impeccable-polishable)
//
// Cross-stack output-type separation (Requirement 20.6):
//   The OpenUI deep-dive (src/components/openui/CompanyDeepDive.tsx) renders
//   ClickHouse company data (name, url, source, fit_score, fit_explanation,
//   signals) as OpenUI Lang for the in-app HUMAN surface. The Python Publisher
//   (angent/publisher.py :: Publisher.serialize) reads the SAME ClickHouse
//   fields but emits a MARKDOWN Deal_Memo for cited.md — never OpenUI Lang —
//   via a separate code path. The two output types share their data source but
//   remain distinct and separate; the Publisher's `assert_markdown_not_openui`
//   guard enforces that no OpenUI Lang ever leaks into the Deal_Memo.

/** The two — and only two — component categories. */
export type ComponentCategory = "openui-generated" | "react-shell";

export interface ComponentEntry {
  /** Workspace-relative path from the Next.js app root (`genui-chat-app/`). */
  path: string;
  /** Exactly one category. */
  category: ComponentCategory;
  /** Short rationale for the classification. */
  reason: string;
}

/**
 * Every component surface in the app, each classified as exactly one category.
 *
 * OpenUI-generated entries render runtime OpenUI Lang via `<Renderer/>` and are
 * OFF-LIMITS to Impeccable. React_Shell entries are plain TSX with no OpenUI
 * Lang markup and ARE the polish targets.
 */
export const COMPONENT_MANIFEST: readonly ComponentEntry[] = [
  // --- OpenUI-generated surfaces (Requirement 14) — never polished ---
  {
    path: "src/components/openui/ThesisChat.tsx",
    category: "openui-generated",
    reason: "Thesis-refinement surface; conversation rendered at runtime via OpenUI Lang + <Renderer/>.",
  },
  {
    path: "src/components/openui/CompanyDeepDive.tsx",
    category: "openui-generated",
    reason: "On-demand deep-dive surface; view generated at runtime via OpenUI Lang + <Renderer/>.",
  },
  {
    path: "src/components/openui/SurfaceErrorBanner.tsx",
    category: "openui-generated",
    reason: "Failure/timeout indicator for the OpenUI surfaces; lives with and supports generated views.",
  },
  {
    path: "src/components/openui/openuiSurface.ts",
    category: "openui-generated",
    reason: "Shared OpenUI Lang prompt builders + budgeted generation pipeline helpers.",
  },

  // --- React_Shell plain components (Requirement 15.1) — Impeccable-polishable ---
  {
    path: "src/components/react-shell/QualifiedCompanyCard.tsx",
    category: "react-shell",
    reason: "Plain TSX dashboard card. No OpenUI Lang markup, no <Renderer/>.",
  },
  {
    path: "src/components/react-shell/DraftedEmailPreview.tsx",
    category: "react-shell",
    reason: "Plain TSX drafted-email preview. No OpenUI Lang markup, no <Renderer/>.",
  },
  {
    path: "src/components/react-shell/LoopStatusDisplay.tsx",
    category: "react-shell",
    reason: "Plain TSX loop-status display. No OpenUI Lang markup, no <Renderer/>.",
  },
] as const;

/**
 * Glob scope for the Impeccable polish pass.
 *
 * `include` = the ONLY paths Impeccable may touch (the React_Shell).
 * `exclude` = paths Impeccable must NEVER touch (the OpenUI-generated surfaces).
 *
 * The `impeccable` npm script runs `impeccable detect` against `include`, and
 * `.impeccableignore` lists `exclude`, so a polish pass changes only plain
 * React components (Req 15.3) and leaves OpenUI-generated components unchanged
 * (Req 15.4 / 15.5).
 */
export const IMPECCABLE_SCOPE = {
  include: ["src/components/react-shell/**"],
  exclude: ["src/components/openui/**"],
} as const;

/** Directory whose contents are the only Impeccable polish targets. */
export const REACT_SHELL_DIR = "src/components/react-shell";
/** Directory Impeccable must never touch. */
export const OPENUI_DIR = "src/components/openui";

/** All component paths classified as plain React (Impeccable-polishable). */
export function reactShellComponents(): string[] {
  return COMPONENT_MANIFEST.filter((c) => c.category === "react-shell").map((c) => c.path);
}

/** All component paths classified as OpenUI-generated (never polished). */
export function openUiComponents(): string[] {
  return COMPONENT_MANIFEST.filter((c) => c.category === "openui-generated").map((c) => c.path);
}

/** Resolve a path's category from the manifest, falling back to its directory. */
export function classify(path: string): ComponentCategory | "unknown" {
  const normalized = path.replace(/\\/g, "/");
  const entry = COMPONENT_MANIFEST.find((c) => normalized.endsWith(c.path));
  if (entry) return entry.category;
  if (normalized.includes(`${OPENUI_DIR}/`)) return "openui-generated";
  if (normalized.includes(`${REACT_SHELL_DIR}/`)) return "react-shell";
  return "unknown";
}

/** True when Impeccable is permitted to polish the component at `path`. */
export function isImpeccablePolishable(path: string): boolean {
  return classify(path) === "react-shell";
}

/**
 * Assert the classification is unambiguous (Req 15.2): every component is
 * listed exactly once and assigned exactly one of the two valid categories.
 * Throws if the manifest is internally inconsistent.
 */
export function assertUnambiguous(): void {
  const valid: ComponentCategory[] = ["openui-generated", "react-shell"];
  const seen = new Set<string>();
  for (const entry of COMPONENT_MANIFEST) {
    if (!valid.includes(entry.category)) {
      throw new Error(`Component ${entry.path} has invalid category "${entry.category}".`);
    }
    if (seen.has(entry.path)) {
      throw new Error(`Component ${entry.path} is classified more than once (ambiguous).`);
    }
    seen.add(entry.path);
  }
}

// Fail fast at module load if the partition is ever broken.
assertUnambiguous();
