// Checks Base Sepolia USDC balances for buyer + seller to confirm settlement.
import dotenv from "dotenv";
import { createPublicClient, http, getAddress } from "viem";
import { baseSepolia } from "viem/chains";

dotenv.config({ path: "../.env" });

// Base Sepolia USDC
const USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e";
const BUYER = "0x8DeF9cF6Ee1ea6791E19865d80D007c355881DDB";
const SELLER = process.env.X402_PAY_TO_ADDRESS;

const abi = [
  { name: "balanceOf", type: "function", stateMutability: "view",
    inputs: [{ name: "a", type: "address" }], outputs: [{ name: "", type: "uint256" }] },
];

const client = createPublicClient({ chain: baseSepolia, transport: http() });

async function bal(addr) {
  const v = await client.readContract({ address: USDC, abi, functionName: "balanceOf", args: [getAddress(addr)] });
  return (Number(v) / 1e6).toFixed(6);
}

console.log("Buyer  ", BUYER, "USDC:", await bal(BUYER));
console.log("Seller ", SELLER, "USDC:", await bal(SELLER));
