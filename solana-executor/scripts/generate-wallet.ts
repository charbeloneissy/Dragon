import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { Keypair } from "@solana/web3.js";

const dir=path.join(os.homedir(),".dragon");
const file=path.join(dir,"solana-executor-keypair.json");
if(fs.existsSync(file))throw new Error("Refusing to overwrite existing wallet: "+file);
fs.mkdirSync(dir,{recursive:true,mode:0o700});
const keypair=Keypair.generate();
fs.writeFileSync(file,JSON.stringify(Array.from(keypair.secretKey)),{mode:0o600});
console.log("Dragon executor public key: "+keypair.publicKey.toBase58());
console.log("Keypair saved locally at: "+file);
console.log("The private key was not printed. Do not commit or upload this file to GitHub.");
