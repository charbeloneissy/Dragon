import "dotenv/config";

const required = (name: string): string => {
  const value = process.env[name]?.trim();
  if (!value) throw new Error("Missing required environment variable: " + name);
  return value;
};

export const config = {
  rpcUrl: process.env.SOLANA_MAINNET_RPC_URL?.trim() || process.env.HELIUS_RPC_URL?.trim() || required("SOLANA_RPC_URL"),
  commitment: (process.env.SOLANA_COMMITMENT?.trim() || "confirmed") as "processed" | "confirmed" | "finalized",
  accountIndex: Number(process.env.DRAGON_P0_ACCOUNT_INDEX || "0"),
  thirdPartyId: process.env.DRAGON_P0_THIRD_PARTY_ID ? Number(process.env.DRAGON_P0_THIRD_PARTY_ID) : undefined,
  liveSigningEnabled: (process.env.SOLANA_LIVE_EXECUTION_ENABLED || "false").toLowerCase() === "true",
};
