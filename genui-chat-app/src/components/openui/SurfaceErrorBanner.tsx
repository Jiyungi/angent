"use client";

// Error indication for the OpenUI_Surface (Req 14.2, 14.4).
//
// NOTE: This banner is NOT part of the agent-generated content. It is the
// surface's failure/timeout indicator, rendered alongside (never replacing) the
// retained last-good generated view. The generated view itself remains mounted
// so its last successfully rendered state is preserved.

import type { GenerationState } from "./openuiSurface";

export interface SurfaceErrorBannerProps {
  state: GenerationState;
  onDismiss?: () => void;
}

export function SurfaceErrorBanner({ state, onDismiss }: SurfaceErrorBannerProps) {
  const isError = state.status === "error" || state.status === "timeout";
  if (!isError) return null;

  return (
    <div
      role="alert"
      style={{
        position: "absolute",
        top: 0,
        left: 0,
        right: 0,
        zIndex: 50,
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: "0.75rem",
        padding: "0.625rem 1rem",
        backgroundColor: "#fef2f2",
        color: "#991b1b",
        borderBottom: "1px solid #fecaca",
        fontSize: "0.875rem",
      }}
    >
      <span>
        {state.error ?? "Generation did not complete. Showing the last successfully rendered view."}
      </span>
      {onDismiss ? (
        <button
          type="button"
          onClick={onDismiss}
          style={{
            flexShrink: 0,
            border: "1px solid #fecaca",
            borderRadius: "0.375rem",
            background: "transparent",
            color: "#991b1b",
            padding: "0.125rem 0.5rem",
            cursor: "pointer",
          }}
        >
          Dismiss
        </button>
      ) : null}
    </div>
  );
}

export default SurfaceErrorBanner;
