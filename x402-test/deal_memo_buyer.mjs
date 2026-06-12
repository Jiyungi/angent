// x402 BUYER (Deal_Memo) — demonstrates an agent paying ~0.001 testnet USDC to
// fetch the REAL Deal_Memo served by deal_memo_server.mjs (Requirement 21.5 /
// 19.12). This mirrors the proven wiring in buyer.mjs (privateKeyToAccount +
// x402Client + ExactEvmScheme + wrapFetchWithPayment) but is robust to the
// deal-memo seller returning `text/markdown` instead of JSON.
//
// Flow it shows:
//   1. GET /deal-memo with no payment -> seller replies HTTP 402 Payment Required.
//   2. @x402/fetch automatically signs + pays ~$0.001 USDC via EVM_PRIVATE_KEY,
//      the Facilitator settles, and the seller returns the Deal_Memo markdown.
//   3. We print the returned Deal_Memo content + the payment/settlement status.
//
// Run: node deal_memo_buyer.mjs   (reads EVM_PRIVATE_KEY + X402_PORT from ../.env)
import dotenv from "dotenv";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { wrapFetchWithPayment } from "@x402/fetch";
import { x402Client } from "@x402/core/client";
import { ExactEvmScheme } from "@x402/evm/exact/client";
import { privateKeyToAccount } from "viem/accounts";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
dotenv.config({ path: path.join(__dirname, "..", ".env") });

const PK = process.env.EVM_PRIVATE_KEY;
const PORT = process.env.X402_PORT || 4021;
const URL = `http://localhost:${PORT}/deal-memo`;

if (!PK) {
  console.error("Missing EVM_PRIVATE_KEY in .env");
  process.exit(1);
}

// Decode an x402 base64-JSON settlement header into an object, or {} on failure.
function decodeX402Header(value) {
  if (!value) return {};
  try {
    return JSON.parse(Buffer.from(String(value), "base64").toString("utf-8"));
  } catch {
    return {};
  }
}

const signer = privateKeyToAccount(PK);
console.log("Buyer wallet:", signer.address);

const client = new x402Client();
client.register("eip155:*", new ExactEvmScheme(signer));

const fetchWithPayment = wrapFetchWithPayment(fetch, client);

console.log(`\nRequesting ${URL} (x402 payment handled automatically)...`);

// wrapFetchWithPayment transparently performs the 402 -> pay -> retry handshake
// and resolves with the FINAL (paid) response. The deal-memo seller returns
// `text/markdown`, so read the body as text rather than assuming JSON.
const response = await fetchWithPayment(URL, { method: "GET" });

const contentType = response.headers.get("content-type") || "";
const rawBody = await response.text();

console.log("\n--- Deal_Memo content ---");
if (contentType.includes("application/json")) {
  // Be tolerant: pretty-print if the seller happened to return JSON.
  try {
    console.log(JSON.stringify(JSON.parse(rawBody), null, 2));
  } catch {
    console.log(rawBody);
  }
} else {
  // Default path: markdown / plain text Deal_Memo.
  console.log(rawBody);
}

console.log("\n--- Payment ---");
console.log("http status:", response.status, response.statusText);
console.log("content-type:", contentType || "(none)");

// The settlement details are returned by the Payment_Gate in the
// `x-payment-response` header (base64-encoded JSON) once the Facilitator settles.
const settlementHeader = response.headers.get("x-payment-response");
if (settlementHeader) {
  const settlement = decodeX402Header(settlementHeader);
  console.log("settled:", settlement?.success ?? true);
  if (settlement?.payer) console.log("payer:", settlement.payer);
  if (settlement?.transaction || settlement?.txHash) {
    console.log("tx:", settlement.transaction || settlement.txHash);
  }
  console.log("settlement:", JSON.stringify(settlement));
} else if (response.status >= 200 && response.status < 300) {
  console.log("status: paid (content returned; no settlement header exposed)");
} else {
  console.log("status: payment not completed");
}
