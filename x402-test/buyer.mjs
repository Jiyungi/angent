// x402 BUYER — simulates an agent paying $0.001 to fetch the deal memo.
// Run: node buyer.mjs   (reads EVM_PRIVATE_KEY from ../.env)
import dotenv from "dotenv";
import { wrapFetchWithPayment, x402HTTPClient } from "@x402/fetch";
import { x402Client } from "@x402/core/client";
import { ExactEvmScheme } from "@x402/evm/exact/client";
import { privateKeyToAccount } from "viem/accounts";

dotenv.config({ path: "../.env" });

const PK = process.env.EVM_PRIVATE_KEY;
const PORT = process.env.X402_PORT || 4021;
const URL = `http://localhost:${PORT}/deal-memo`;

if (!PK) {
  console.error("Missing EVM_PRIVATE_KEY in .env");
  process.exit(1);
}

const signer = privateKeyToAccount(PK);
console.log("Buyer wallet:", signer.address);

const client = new x402Client();
client.register("eip155:*", new ExactEvmScheme(signer));

const fetchWithPayment = wrapFetchWithPayment(fetch, client);
const httpClient = new x402HTTPClient(client);

console.log(`\nRequesting ${URL} (payment handled automatically)...`);
const response = await fetchWithPayment(URL, { method: "GET" });
const result = await httpClient.processResponse(response);

console.log("\n--- Response body ---");
console.log(JSON.stringify(result.body, null, 2));

console.log("\n--- Payment ---");
console.log("status:", result.paymentStatus);
if (result.header) console.log("settlement:", JSON.stringify(result.header));
