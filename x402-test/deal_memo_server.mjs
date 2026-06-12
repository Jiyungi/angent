// x402 SELLER (production-ish) — paywalls the REAL Deal_Memo fetch endpoint.
//
// This is the x402 Payment_Gate (Requirement 21 / 18.12 / 18.14). It reuses the
// proven wiring from server.mjs (express + paymentMiddleware + ExactEvmScheme +
// HTTPFacilitatorClient, configured from the X402_* env vars) but serves the
// REAL Deal_Memo produced by the Python Publisher instead of a hardcoded demo.
//
// Deal_Memo source: the Python Publisher (../angent/publisher.py) writes the
// markdown Deal_Memo to ../deal_memos/<run_id>.md (its Senso local fallback,
// task 20.2). This seller serves the MOST RECENT *.md file from that directory
// as the protected content. If none exists yet, it returns a clear placeholder.
//
// Payment behaviour (Requirements 21.1–21.4):
//   * GET /deal-memo WITHOUT a valid x402 payment  -> HTTP 402 Payment Required.
//   * WITH a valid payment -> the Facilitator settles, content is returned, and
//     the settled fetch is recorded to the ClickHouse `fetches` table.
//   * IF the Facilitator is unreachable -> the middleware denies access (502 /
//     402) and NEVER serves the content unpaid. There is NO code path that
//     serves /deal-memo content without a settled payment (the handler is only
//     reached after the middleware verifies payment, and settlement runs after
//     the handler, before the buffered body is flushed).
//
// Run: node deal_memo_server.mjs   (reads config from ../.env)
import express from "express";
import dotenv from "dotenv";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";
import { paymentMiddleware, x402ResourceServer } from "@x402/express";
import { ExactEvmScheme } from "@x402/evm/exact/server";
import { HTTPFacilitatorClient } from "@x402/core/server";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Load the project .env (one level up) — same as server.mjs / buyer.mjs.
dotenv.config({ path: path.join(__dirname, "..", ".env") });

// --- x402 Payment_Gate configuration (Requirement 21.3) --------------------
const FACILITATOR_URL = process.env.X402_FACILITATOR_URL || "https://x402.org/facilitator";
const NETWORK = process.env.X402_NETWORK || "eip155:84532";
const PAY_TO = process.env.X402_PAY_TO_ADDRESS;
const PRICE = process.env.X402_PRICE || "$0.001";
const PORT = process.env.X402_PORT || 4021;

// Where the Python Publisher writes Deal_Memo markdown (its local fallback).
const DEAL_MEMOS_DIR = path.join(__dirname, "..", "deal_memos");

// --- ClickHouse config (for the settled-fetch ledger) ----------------------
const CH_HOST = process.env.CLICKHOUSE_HOST;
const CH_PORT = process.env.CLICKHOUSE_PORT || "8443";
const CH_USER = process.env.CLICKHOUSE_USER || "default";
const CH_PASSWORD = process.env.CLICKHOUSE_PASSWORD || "";
const CH_DATABASE = process.env.CLICKHOUSE_DATABASE || "default";

if (!PAY_TO) {
  console.error("Missing X402_PAY_TO_ADDRESS in .env");
  process.exit(1);
}

// --- Deal_Memo loader -------------------------------------------------------

/**
 * Return the most recent Deal_Memo markdown written by the Publisher, plus the
 * run_id derived from its filename (`<run_id>.md`). Returns null when none yet.
 */
function loadLatestDealMemo() {
  let entries;
  try {
    entries = fs
      .readdirSync(DEAL_MEMOS_DIR, { withFileTypes: true })
      .filter((e) => e.isFile() && e.name.toLowerCase().endsWith(".md"))
      .map((e) => {
        const full = path.join(DEAL_MEMOS_DIR, e.name);
        return { name: e.name, full, mtime: fs.statSync(full).mtimeMs };
      })
      .sort((a, b) => b.mtime - a.mtime);
  } catch {
    return null; // directory does not exist yet
  }
  if (entries.length === 0) return null;
  const latest = entries[0];
  const markdown = fs.readFileSync(latest.full, "utf-8");
  const runId = path.basename(latest.name, ".md");
  return { markdown, runId, title: deriveTitle(markdown) };
}

function deriveTitle(markdown) {
  const firstHeading = markdown.split("\n").find((l) => l.startsWith("# "));
  return firstHeading ? firstHeading.replace(/^#\s+/, "").trim() : "Angent Deal Memo";
}

// --- settled-fetch ledger (ClickHouse `fetches` table) ---------------------

/**
 * Decode an x402 base64-JSON header (`x-payment` request header or
 * `x-payment-response` settlement header) into an object, or {} on failure.
 */
function decodeX402Header(value) {
  if (!value) return {};
  try {
    const raw = Buffer.from(String(value), "base64").toString("utf-8");
    return JSON.parse(raw);
  } catch {
    return {};
  }
}

/**
 * Insert a settled-fetch row into the ClickHouse `fetches` table over the
 * Cloud HTTPS interface. Best-effort: never throws, logs on failure. Columns:
 * fetch_id, run_id, paid, amount, network, payer, tx_reference, settled_at.
 */
async function recordFetch(row) {
  if (!CH_HOST) {
    console.warn("[fetches] CLICKHOUSE_HOST not set; skipping ledger insert. Row:", row);
    return;
  }
  const url = `https://${CH_HOST}:${CH_PORT}/?database=${encodeURIComponent(CH_DATABASE)}`;
  const auth = "Basic " + Buffer.from(`${CH_USER}:${CH_PASSWORD}`).toString("base64");
  // settled_at as 'YYYY-MM-DD HH:MM:SS' (ClickHouse DateTime).
  const settledAt = new Date().toISOString().slice(0, 19).replace("T", " ");
  const jsonRow = JSON.stringify({
    fetch_id: row.fetch_id,
    run_id: row.run_id,
    paid: row.paid,
    amount: row.amount,
    network: row.network,
    payer: row.payer,
    tx_reference: row.tx_reference, // Nullable(String)
    settled_at: settledAt,
  });
  const body = "INSERT INTO fetches FORMAT JSONEachRow\n" + jsonRow;
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: { Authorization: auth, "Content-Type": "text/plain" },
      body,
    });
    if (!resp.ok) {
      const text = await resp.text().catch(() => "");
      console.warn(`[fetches] insert failed (${resp.status}): ${text}`);
    } else {
      console.log(`[fetches] recorded settled fetch ${row.fetch_id} payer=${row.payer}`);
    }
  } catch (err) {
    console.warn(`[fetches] insert error: ${err?.message || err}`);
  }
}

// --- express app + x402 paywall (mirrors server.mjs wiring) ----------------

const app = express();
const facilitatorClient = new HTTPFacilitatorClient({ url: FACILITATOR_URL });

app.use(
  paymentMiddleware(
    {
      "GET /deal-memo": {
        accepts: [
          {
            scheme: "exact", // exact USDC scheme (Requirement 21.3)
            price: PRICE,
            network: NETWORK,
            payTo: PAY_TO,
          },
        ],
        description: "Angent verified startup deal memo",
        mimeType: "text/markdown",
      },
    },
    new x402ResourceServer(facilitatorClient).register(NETWORK, new ExactEvmScheme()),
  ),
);

// The protected content. This handler is ONLY reached after the middleware has
// VERIFIED the payment; the Facilitator SETTLES after the handler runs (the
// middleware buffers the body and flushes it only once settlement succeeds).
// So there is no path that serves the Deal_Memo unpaid (Requirements 21.2, 21.4,
// 18.14). We register the settled fetch once the response has been flushed with
// a 2xx status, reading the payer / settlement reference from the x402 headers.
app.get("/deal-memo", (req, res) => {
  const memo = loadLatestDealMemo();

  // Derive payer up-front from the verified payment header (authorization.from).
  const paymentPayload = decodeX402Header(
    req.header("payment-signature") || req.header("x-payment"),
  );
  const payerFromPayment =
    paymentPayload?.payload?.authorization?.from || paymentPayload?.payload?.from || "";
  const runId = memo?.runId || "";

  // Record the settled fetch once the response is fully flushed (after the
  // middleware has settled via the Facilitator). statusCode 2xx => settled.
  res.on("finish", () => {
    if (res.statusCode < 200 || res.statusCode >= 300) return; // not settled/served
    const settlement = decodeX402Header(res.getHeader("x-payment-response"));
    const payer = settlement?.payer || payerFromPayment || "unknown";
    const txReference = settlement?.transaction || settlement?.txHash || null;
    void recordFetch({
      fetch_id: randomUUID(),
      run_id: runId,
      paid: 1,
      amount: PRICE,
      network: NETWORK,
      payer,
      tx_reference: txReference,
    });
  });

  if (!memo) {
    // No Deal_Memo has been published/written yet. Still gated behind payment,
    // but make the placeholder explicit so the buyer knows why it's empty.
    res
      .status(200)
      .type("text/markdown")
      .send(
        "# Angent Deal Memo (placeholder)\n\n" +
          "_No Deal_Memo has been generated yet. The Publisher writes the latest " +
          "Deal_Memo markdown to `deal_memos/<run_id>.md`; once a run publishes, " +
          "this endpoint will serve that content._\n",
      );
    return;
  }

  res.status(200).type("text/markdown").send(memo.markdown);
});

app.listen(PORT, () => {
  console.log(`x402 deal-memo seller listening on http://localhost:${PORT}`);
  console.log(`  network=${NETWORK}  payTo=${PAY_TO}  price=${PRICE}`);
  console.log(`  facilitator=${FACILITATOR_URL}`);
  console.log(`  deal_memos dir=${DEAL_MEMOS_DIR}`);
  console.log(`  fetches ledger -> ClickHouse ${CH_HOST ? CH_HOST + ":" + CH_PORT : "(not configured)"}`);
});
