import fs from "node:fs";
import { Keypair } from "@solana/web3.js";
import bs58 from "bs58";

function fromSecretBytes(bytes: Uint8Array): Keypair {
  if (bytes.length !== 64) throw new Error("Dragon Solana keypair must contain exactly 64 secret-key bytes");
  return Keypair.fromSecretKey(bytes);
}

export function loadExecutorKeypair(): Keypair {
  const json = process.env.DRAGON_SOLANA_PRIVATE_KEY_JSON?.trim();
  if (json) {
    const parsed = JSON.parse(json);
    if (!Array.isArray(parsed)) throw new Error("DRAGON_SOLANA_PRIVATE_KEY_JSON must be a JSON byte array");
    return fromSecretBytes(Uint8Array.from(parsed));
  }
  const base58 = process.env.DRAGON_SOLANA_PRIVATE_KEY_B58?.trim();
  if (base58) return fromSecretBytes(bs58.decode(base58));
  const file = process.env.DRAGON_SOLANA_KEYPAIR_FILE?.trim();
  if (file) {
    const parsed = JSON.parse(fs.readFileSync(file, "utf8"));
    if (!Array.isArray(parsed)) throw new Error("DRAGON_SOLANA_KEYPAIR_FILE must contain a JSON byte array");
    return fromSecretBytes(Uint8Array.from(parsed));
  }
  throw new Error("No Dragon Solana executor key configured");
}

export function executorPublicKey(): string {
  return loadExecutorKeypair().publicKey.toBase58();
}
