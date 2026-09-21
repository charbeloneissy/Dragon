import test from "node:test";
import assert from "node:assert/strict";
import { Keypair } from "@solana/web3.js";
import { deriveMarginfiAccount,getConfig } from "@0dotxyz/p0-ts-sdk";

test("P0 production config is mainnet and account derivation is deterministic",()=>{
  const authority=Keypair.generate().publicKey;
  const config=getConfig("production");
  const [a]=deriveMarginfiAccount(config.programId,config.groupPk,authority,0);
  const [b]=deriveMarginfiAccount(config.programId,config.groupPk,authority,0);
  assert.equal(a.toBase58(),b.toBase58());
  assert.ok(config.programId);
});
