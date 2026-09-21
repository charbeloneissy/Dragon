# Dragon Solana Executor

Mainnet-only transaction-construction layer for Dragon + Project 0.

Security:
- Private key is never committed.
- Builder never signs or broadcasts.
- HTTP service returns only public key, P0 account PDA and unsigned transaction bytes.
- Live signing is deliberately rejected.
- Configure secrets through Render secret environment variables or a Render secret file.
- Never paste the private key into ChatGPT, GitHub, logs or PR comments.

Mainnet:
- Solana Mainnet
- P0 environment: production
- Project 0 program: MFv2hWf31Z9kbCa1snEPYctwafyhdvnV7FZnsebVacA

Secrets:
- DRAGON_SOLANA_PRIVATE_KEY_JSON: JSON array of 64 bytes
- DRAGON_SOLANA_PRIVATE_KEY_B58: base58-encoded 64-byte secret key
- DRAGON_SOLANA_KEYPAIR_FILE: mounted secret-file path
- SOLANA_MAINNET_RPC_URL: preferred Mainnet RPC (Helius or Alchemy)
- DRAGON_EXECUTOR_API_TOKEN: recommended internal API authentication

Generate locally:
cd solana-executor
npm install
npx tsx scripts/generate-wallet.ts

The command prints only the public key and saves the secret locally under ~/.dragon/.

P0 account:
Account 0 is a deterministic PDA derived from the production program, production group, executor authority, account index and optional third-party ID. /v1/p0/init-account builds the unsigned initialization transaction.

Flash loan:
The builder creates optional compute-budget instruction(s), then P0 beginFlashLoan, borrow, Dragon swap instructions, and P0 endFlashLoan. The P0 SDK computes projected active banks for the final health check.

Current status:
Build/simulate-only. No signing or sending. Helius Sender remains a later stage after exact simulation and the $0.005 net-profit gate.
