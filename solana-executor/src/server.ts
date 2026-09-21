import http from "node:http";
import { Connection } from "@solana/web3.js";
import { config } from "./config.js";
import { loadExecutorKeypair } from "./wallet.js";
import { buildP0AccountInitTx,buildP0FlashLoanTx,createMainnetClient,deriveP0Account,getP0AccountInfo,serializeUnsigned,deserializeInstruction } from "./p0.js";

const connection=new Connection(config.rpcUrl,config.commitment);
const PORT=Number(process.env.PORT||"10000");
const API_TOKEN=process.env.DRAGON_EXECUTOR_API_TOKEN?.trim();

function authorize(req:http.IncomingMessage):void{
  if(!API_TOKEN)return;
  if((req.headers.authorization||"")!=="Bearer "+API_TOKEN)throw new Error("unauthorized");
}

async function readJson(req:http.IncomingMessage):Promise<any>{
  const chunks:Buffer[]=[];
  for await(const chunk of req)chunks.push(Buffer.from(chunk));
  return JSON.parse(Buffer.concat(chunks).toString("utf8")||"{}");
}

function send(res:http.ServerResponse,status:number,body:unknown):void{
  res.writeHead(status,{"content-type":"application/json","cache-control":"no-store"});
  res.end(JSON.stringify(body));
}

async function main():Promise<void>{
  const client=await createMainnetClient(connection);
  const authority=loadExecutorKeypair().publicKey;
  const account=deriveP0Account(client,authority);

  const server=http.createServer(async(req,res)=>{
    try{
      authorize(req);
      const path=(req.url||"").split("?")[0];
      if(req.method==="GET"&&path==="/health")return send(res,200,{ok:true,network:"solana-mainnet",p0Program:client.program.programId.toBase58()});
      if(req.method==="GET"&&path==="/v1/wallet")return send(res,200,{publicKey:authority.toBase58(),p0Account:account.toBase58()});
      if(req.method==="GET"&&path==="/v1/p0/account")return send(res,200,await getP0AccountInfo(client,authority));
      if(req.method==="POST"&&path==="/v1/p0/init-account"){
        const tx=await buildP0AccountInitTx(client,authority);
        return send(res,200,{unsigned:true,authority:authority.toBase58(),account:account.toBase58(),transactionBase64:serializeUnsigned(tx)});
      }
      if(req.method==="POST"&&path==="/v1/p0/flashloan"){
        const body=await readJson(req);
        const middleInstructions=(body.middleInstructions||[]).map(deserializeInstruction);
        const tx=await buildP0FlashLoanTx(client,authority,{accountAddress:String(body.accountAddress||account.toBase58()),bankAddress:String(body.bankAddress||""),amountUi:String(body.amountUi||""),middleInstructions,priorityFeeMicroLamports:body.priorityFeeMicroLamports===undefined?undefined:Number(body.priorityFeeMicroLamports)});
        return send(res,200,{unsigned:true,transactionBase64:serializeUnsigned(tx),p0Account:account.toBase58()});
      }
      return send(res,404,{error:"not_found"});
    }catch(error){
      const message=error instanceof Error?error.message:"unknown_error";
      return send(res,message==="unauthorized"?401:400,{error:message});
    }
  });

  server.listen(PORT,"0.0.0.0",()=>console.log("Dragon Solana executor listening on :"+PORT+" (public key only)"));
}

main().catch(error=>{console.error("Dragon Solana executor startup failed:",error instanceof Error?error.message:error);process.exit(1);});
