// Trigger an Angent control-loop run from the UI (no terminal needed).
// Spawns `python -m angent.main --demo` against the project root, with Pioneer
// disabled so scoring is instant for a live demo. Returns immediately; the UI
// polls /api/dashboard to show rows populating.

import { NextResponse } from "next/server";
import { spawn } from "node:child_process";
import path from "node:path";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

let running = false;

export async function POST() {
  if (running) return NextResponse.json({ ok: true, already: true, running: true });
  const cwd = path.join(process.cwd(), "..");
  const env = { ...process.env, PIONEER_API_KEY: "" };
  const candidates = ["python", "py", "python3"];
  for (const bin of candidates) {
    try {
      const child = spawn(bin, ["-m", "angent.main", "--demo"], {
        cwd,
        env,
        detached: true,
        stdio: "ignore",
      });
      running = true;
      child.on("error", () => {
        running = false;
      });
      child.on("exit", () => {
        running = false;
      });
      child.unref();
      return NextResponse.json({ ok: true, started: true, bin });
    } catch {
      // try next interpreter name
    }
  }
  return NextResponse.json({ ok: false, error: "python interpreter not found" });
}

export async function GET() {
  return NextResponse.json({ running });
}
