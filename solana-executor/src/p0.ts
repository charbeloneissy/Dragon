import { Connection, PublicKey, Transaction, TransactionInstruction, VersionedTransaction } from "@solana/web3.js";
import { Project0Client, deriveMarginfiAccount, getConfig, MarginfiAccountWrapper, makePriorityFeeMicroIx } from "@0dotxyz/p0-ts-sdk";
import { config } from "./config.js";

export type UnsignedTx = Transaction | VersionedTransaction;

export interface P0AccountInfo {
  authority:string;
  account:string;
  accountIndex:number;
  thirdPartyId?:number;
  programId:string;
  group:string;
  exists:boolean;
}

export interface FlashLoanBuildInput {
  accountAddress:string;
  bankAddress:string;
  amountUi:string;
  middleInstructions?:TransactionInstruction[];
  priorityFeeMicroLamports?:number;
}

export async function createMainnetClient(connection:Connection):Promise<Project0Client>{
  if (!connection.rpcEndpoint.includes("mainnet")) throw new Error("Dragon P0 executor is Mainnet-only");
  return Project0Client.initialize(connection, getConfig("production"));
}

export function deriveP0Account(client:Project0Client, authority:PublicKey, accountIndex=config.accountIndex, thirdPartyId=config.thirdPartyId):PublicKey{
  const [address]=deriveMarginfiAccount(client.program.programId, client.group.address, authority, accountIndex, thirdPartyId);
  return address;
}

export async function getP0AccountInfo(client:Project0Client, authority:PublicKey):Promise<P0AccountInfo>{
  const account=deriveP0Account(client,authority);
  const info=await client.connection.getAccountInfo(account,config.commitment);
  return {authority:authority.toBase58(),account:account.toBase58(),accountIndex:config.accountIndex,thirdPartyId:config.thirdPartyId,programId:client.program.programId.toBase58(),group:client.group.address.toBase58(),exists:info!==null};
}

export async function buildP0AccountInitTx(client:Project0Client, authority:PublicKey):Promise<Transaction>{
  const tx=await client.createMarginfiAccountTx(authority,config.accountIndex,config.thirdPartyId);
  const {blockhash}=await client.connection.getLatestBlockhash(config.commitment);
  tx.feePayer=authority;
  tx.recentBlockhash=blockhash;
  return tx;
}

export async function buildP0FlashLoanTx(client:Project0Client, authority:PublicKey, input:FlashLoanBuildInput):Promise<VersionedTransaction>{
  if (!input.accountAddress) throw new Error("accountAddress is required");
  if (!input.bankAddress) throw new Error("bankAddress is required");
  if (!/^[0-9]+(\.[0-9]+)?$/.test(input.amountUi) || Number(input.amountUi)<=0) throw new Error("amountUi must be a positive decimal string");
  if (config.liveSigningEnabled) throw new Error("Live signing is disabled; this component only builds unsigned transactions");

  const accountAddress=new PublicKey(input.accountAddress);
  if (!accountAddress.equals(deriveP0Account(client,authority))) throw new Error("accountAddress does not match the Dragon executor authority/account PDA");

  const account=await client.fetchAccount(accountAddress);
  const wrapped=new MarginfiAccountWrapper(account,client);
  const bank=client.getBank(new PublicKey(input.bankAddress));
  if (!bank) throw new Error("P0 bank not found in production configuration");

  const borrow=await wrapped.makeBorrowIx(bank.address,input.amountUi);
  const middle=[...borrow.instructions,...(input.middleInstructions||[])];
  if (middle.length===0) throw new Error("flash-loan body cannot be empty");

  if (input.priorityFeeMicroLamports!==undefined) {
    if (!Number.isInteger(input.priorityFeeMicroLamports) || input.priorityFeeMicroLamports<0) throw new Error("priorityFeeMicroLamports must be a non-negative integer");
    middle.unshift(makePriorityFeeMicroIx(input.priorityFeeMicroLamports));
  }

  const {blockhash}=await client.connection.getLatestBlockhash(config.commitment);
  const tx=await wrapped.makeFlashLoanTx({ixs:middle,bankMap:client.bankMap,blockhash,addressLookupTableAccounts:client.addressLookupTables,signers:borrow.keys});
  if (!(tx instanceof VersionedTransaction)) throw new Error("P0 flash-loan builder returned an unexpected transaction type");
  return tx;
}

export function serializeUnsigned(tx:UnsignedTx):string{
  if (tx instanceof Transaction) return Buffer.from(tx.serialize({requireAllSignatures:false,verifySignatures:false})).toString("base64");
  return Buffer.from(tx.serialize()).toString("base64");
}

export function deserializeInstruction(value:{programId:string;keys:Array<{pubkey:string;isSigner:boolean;isWritable:boolean}>;dataBase64:string}):TransactionInstruction{
  return new TransactionInstruction({
    programId:new PublicKey(value.programId),
    keys:value.keys.map(key=>({pubkey:new PublicKey(key.pubkey),isSigner:Boolean(key.isSigner),isWritable:Boolean(key.isWritable)})),
    data:Buffer.from(value.dataBase64,"base64")
  });
}
