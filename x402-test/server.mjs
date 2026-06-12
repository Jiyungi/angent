// x402 SELLER — a paywalled "deal memo" endpoint on Base Sepolia (testnet).
// Run: node server.mjs   (reads config from ../.env)
import express from "express";
import dotenv from "dotenv";
import { paymentMiddleware, x402ResourceServer } from "@x402/express";
import { ExactEvmScheme } from "@x402/evm/exact/server";
import { HTTPFacilitatorClient } from "@x402/core/server";

// Load the project .env (one level up)
dotenv.config({ path: "../.env" });

const FACILITATOR_URL = process.env.X402_FACILITATOR_URL || "https://x402.org/facilitator";
const NETWORK = process.env.X402_NETWORK || "eip155:84532";
const PAY_TO = process.env.X402_PAY_TO_ADDRESS;
const PRICE = process.env.X402_PRICE || "$0.001";
const PORT = process.env.X402_PORT || 4021;

if (!PAY_TO) {
  console.error("Missing X402_PAY_TO_ADDRESS in .env");
  process.exit(1);
}

const app = express();
const facilitatorClient = new HTTPFacilitatorClient({ url: FACILITATOR_URL });

app.use(
  paymentMiddleware(
    {
      "GET /deal-memo": {
        accepts: [
          {
            scheme: "exact",
            price: PRICE,
            network: NETWORK,
            payTo: PAY_TO,
          },
        ],
        description: "Angent verified startup deal memo",
        mimeType: "application/json",
      },
    },
    new x402ResourceServer(facilitatorClient).register(NETWORK, new ExactEvmScheme()),
  ),
);

// The actual protected content (this is what an agent pays $0.001 to fetch)
app.get("/deal-memo", (req, res) => {
  res.json({
    title: "Emerging AI-Infra Startups — demo memo",
    generated_at: new Date().toISOString(),
    companies: [
      {
        name: "acme-vectordb",
        source: "github",
        url: "https://github.com/acme/vectordb",
        fit_score: 92,
        why: "High-velocity vector DB with strong early traction; matches the AI-infra thesis.",
      },
    ],
    note: "If you can read this JSON, the x402 payment settled successfully.",
  });
});

app.listen(PORT, () => {
  console.log(`x402 seller listening on http://localhost:${PORT}`);
  console.log(`  network=${NETWORK}  payTo=${PAY_TO}  price=${PRICE}`);
  console.log(`  facilitator=${FACILITATOR_URL}`);
});
