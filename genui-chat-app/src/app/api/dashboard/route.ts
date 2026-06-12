// Angent dashboard data: reads the REAL rows the Python core wrote to the
// ClickHouse blackboard (companies / loop_state / emails) over the Cloud HTTPS
// interface, so the UI reflects an actual agent run end-to-end.
//
// ClickHouse credentials live in the project root .env (one level up from the
// Next app). We read them server-side at request time so nothing secret is
// bundled into the client. If ClickHouse is unreachable or empty, the route
// returns empty arrays and the dashboard falls back to representative data.

import { NextResponse } from "next/server";
import fs from "node:fs";
import path from "node:path";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type Env = Record<string, string>;

function loadRootEnv(): Env {
  // Prefer real process env; fall back to parsing ../.env (project root).
  const env: Env = {};
  try {
    const p = path.join(process.cwd(), "..", ".env");
    const text = fs.readFileSync(p, "utf-8");
    for (const line of text.split(/\r?\n/)) {
      const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
      if (!m) continue;
      let v = m[2].trim();
      if (
        (v.startsWith('"') && v.endsWith('"')) ||
        (v.startsWith("'") && v.endsWith("'"))
      ) {
        v = v.slice(1, -1);
      }
      env[m[1]] = v;
    }
  } catch {
    // ignore — fall back to process.env below
  }
  return { ...env, ...process.env } as Env;
}

async function chQuery(env: Env, sql: string): Promise<Record<string, unknown>[]> {
  const host = env.CLICKHOUSE_HOST;
  if (!host) return [];
  const port = env.CLICKHOUSE_PORT || "8443";
  const user = env.CLICKHOUSE_USER || "default";
  const password = env.CLICKHOUSE_PASSWORD || "";
  const database = env.CLICKHOUSE_DATABASE || "default";
  const url = `https://${host}:${port}/?database=${encodeURIComponent(database)}`;
  const auth = "Basic " + Buffer.from(`${user}:${password}`).toString("base64");

  const resp = await fetch(url, {
    method: "POST",
    headers: { Authorization: auth, "Content-Type": "text/plain" },
    body: sql + "\nFORMAT JSONEachRow",
    // ClickHouse Cloud can be slow on a cold connection; bound it generously.
    signal: AbortSignal.timeout(25_000),
  });
  if (!resp.ok) {
    throw new Error(`ClickHouse ${resp.status}: ${(await resp.text()).slice(0, 200)}`);
  }
  const body = (await resp.text()).trim();
  if (!body) return [];
  return body.split(/\r?\n/).map((line) => JSON.parse(line));
}

const COMPANIES_SQL = `
SELECT source, name, url, fs AS fit_score, fe AS fit_explanation, sig AS signals FROM (
  SELECT source, source_unique_id, name, url,
         argMax(fit_score, version) AS fs,
         argMax(fit_explanation, version) AS fe,
         argMax(signals, version) AS sig
  FROM companies
  GROUP BY source, source_unique_id, name, url
) WHERE fs >= 0
ORDER BY fit_score DESC
LIMIT 12`;

const LOOP_STATE_SQL = `
SELECT run_id, ti AS tick_index, es AS emails_sent, bud AS budget,
       rr AS reply_rate, st AS status, sr AS stop_reason FROM (
  SELECT run_id,
         argMax(tick_index, version) AS ti,
         argMax(emails_sent, version) AS es,
         argMax(goal_email_budget, version) AS bud,
         argMax(reply_rate, version) AS rr,
         argMax(status, version) AS st,
         argMax(stop_reason, version) AS sr,
         max(updated_at) AS ua
  FROM loop_state GROUP BY run_id
) ORDER BY run_id DESC LIMIT 1`;

const DRAFT_SQL = `
SELECT sub AS subject, bod AS body, ap AS approved, snt AS sent FROM (
  SELECT email_id,
         argMax(subject, version) AS sub,
         argMax(body, version) AS bod,
         argMax(approved, version) AS ap,
         argMax(sent, version) AS snt,
         max(updated_at) AS ua
  FROM emails GROUP BY email_id
) ORDER BY ua DESC LIMIT 1`;

export async function GET() {
  const env = loadRootEnv();
  try {
    const [companies, loopRows, draftRows] = await Promise.all([
      chQuery(env, COMPANIES_SQL),
      chQuery(env, LOOP_STATE_SQL),
      chQuery(env, DRAFT_SQL),
    ]);
    return NextResponse.json({
      ok: true,
      source: "clickhouse",
      companies,
      loopState: loopRows[0] ?? null,
      draft: draftRows[0] ?? null,
    });
  } catch (err) {
    return NextResponse.json({
      ok: false,
      source: "error",
      error: err instanceof Error ? err.message : String(err),
      companies: [],
      loopState: null,
      draft: null,
    });
  }
}
