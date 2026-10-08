import { createHash, createHmac } from "node:crypto";
import { readFileSync } from "node:fs";
import {
  ComputeBudgetProgram, Connection, Keypair, LAMPORTS_PER_SOL, PublicKey, SystemProgram,
  TransactionMessage, VersionedTransaction,
} from "@solana/web3.js";
import { OnlinePumpSdk, PUMP_PROGRAM_ID, PUMP_SDK } from "@pump-fun/pump-sdk";

const CFG = JSON.parse(readFileSync(new URL("./coins.json", import.meta.url)));
const conn = new Connection(process.env.RPC_URL || CFG.rpc, "confirmed");
const [task, arg = "", confirm = ""] = process.argv.slice(2);

const sol = (l) => (Number(l) / LAMPORTS_PER_SOL).toFixed(6);
const env = (name) => {
  if (!process.env[name]) throw new Error(`missing secret ${name}`);
  return process.env[name];
};
const keypair = (name) => Keypair.fromSecretKey(Uint8Array.from(JSON.parse(env(name))));
const mintFor = (id) => Keypair.fromSeed(createHmac("sha256", Buffer.from(env("MINT_SEED"), "hex")).update(id).digest());
const code = (obj, back = 0) =>
  createHash("sha256").update(JSON.stringify({ ...obj, bucket: Math.floor(Date.now() / 900_000) - back })).digest("hex").slice(0, 10);

function coin(id) {
  const c = CFG.coins.find((x) => x.id === id);
  if (!c) throw new Error(`unknown coin ${id}`);
  if (c.name.length > 32 || c.symbol.length > 13 || c.uri.length > 200) throw new Error("name, symbol or uri too long");
  return c;
}

async function build(payer, ixs, signers = []) {
  const { blockhash, lastValidBlockHeight } = await conn.getLatestBlockhash("confirmed");
  const alt = (await conn.getAddressLookupTable(new PublicKey(CFG.lookup_table))).value;
  const msg = new TransactionMessage({
    payerKey: payer,
    recentBlockhash: blockhash,
    instructions: [
      ComputeBudgetProgram.setComputeUnitLimit({ units: CFG.cu_limit }),
      ComputeBudgetProgram.setComputeUnitPrice({ microLamports: CFG.cu_price }),
      ...ixs,
    ],
  }).compileToV0Message(alt ? [alt] : []);
  const tx = new VersionedTransaction(msg);
  if (signers.length) tx.sign(signers);
  return { tx, blockhash, lastValidBlockHeight };
}

async function simulate(tx, payer) {
  const pre = await conn.getBalance(payer);
  const r = await conn.simulateTransaction(tx, {
    sigVerify: false, replaceRecentBlockhash: true, accounts: { encoding: "base64", addresses: [payer.toBase58()] },
  });
  if (r.value.err) throw new Error(`simulation failed: ${JSON.stringify(r.value.err)}\n${(r.value.logs || []).slice(-12).join("\n")}`);
  const fee = 5000 * tx.message.header.numRequiredSignatures + Math.ceil((CFG.cu_limit * CFG.cu_price) / 1e6);
  return { pre, cu: r.value.unitsConsumed, cost: pre - r.value.accounts[0].lamports + fee, size: tx.serialize().length };
}

async function createIxs(c, payer, mint) {
  const ix = await PUMP_SDK.createV2Instruction({
    mint, name: c.name, symbol: c.symbol, uri: c.uri, creator: payer, user: payer, mayhemMode: false,
  });
  if (!ix.programId.equals(PUMP_PROGRAM_ID)) throw new Error("unexpected program");
  if (!ix.data.includes(payer.toBuffer())) throw new Error("creator is not the payer");
  if (!ix.keys.some((k) => k.isSigner && k.pubkey.equals(mint))) throw new Error("mint is not a signer");
  return [ix];
}

async function spent(payer, extra) {
  const used = CFG.start_balance_sol * LAMPORTS_PER_SOL - (await conn.getBalance(payer)) + extra;
  if (CFG.start_balance_sol && used > CFG.max_spend_sol * LAMPORTS_PER_SOL) throw new Error(`spend cap: ${sol(used)} > ${CFG.max_spend_sol}`);
  return used;
}

async function send(tx, signers, blockhash, lastValidBlockHeight) {
  tx.sign(signers);
  const sig = await conn.sendRawTransaction(tx.serialize(), { maxRetries: 5 });
  await conn.confirmTransaction({ signature: sig, blockhash, lastValidBlockHeight }, "confirmed");
  return sig;
}

async function launch(id, live) {
  const c = coin(id);
  const payer = keypair("LAUNCH_KEY");
  const mint = mintFor(id);
  if (await conn.getAccountInfo(mint.publicKey)) throw new Error(`coin ${id} already launched: ${mint.publicKey.toBase58()}`);
  const { tx, blockhash, lastValidBlockHeight } = await build(payer.publicKey, await createIxs(c, payer.publicKey, mint.publicKey));
  const s = await simulate(tx, payer.publicKey);
  const facts = { id, name: c.name, symbol: c.symbol, uri: c.uri, mint: mint.publicKey.toBase58(), payer: payer.publicKey.toBase58(), cu_price: CFG.cu_price };
  const used = await spent(payer.publicKey, s.cost);
  console.log(JSON.stringify({ ...facts, balance_sol: sol(s.pre), cost_sol: sol(s.cost), spent_after_sol: sol(used), cu: s.cu, bytes: s.size }, null, 1));
  if (!live) return console.log(`preview code: ${code(facts)} (valid about 15-30 min)`);
  if (confirm !== code(facts) && confirm !== code(facts, 1)) throw new Error("confirm code does not match a fresh preview");
  const sig = await send(tx, [payer, mint], blockhash, lastValidBlockHeight);
  const bc = await new OnlinePumpSdk(conn).fetchBondingCurve(mint.publicKey);
  console.log(JSON.stringify({ sent: sig, mint: facts.mint, creator_ok: bc.creator.equals(payer.publicKey), url: `https://pump.fun/coin/${facts.mint}` }, null, 1));
}

async function claim(live) {
  const payer = keypair("LAUNCH_KEY");
  const online = new OnlinePumpSdk(conn);
  const vault = await online.getCreatorVaultBalanceBothPrograms(payer.publicKey);
  const { tx, blockhash, lastValidBlockHeight } = await build(payer.publicKey, await online.collectCoinCreatorFeeInstructions(payer.publicKey, payer.publicKey));
  const s = await simulate(tx, payer.publicKey);
  const facts = { task: "claim", payer: payer.publicKey.toBase58(), vault: vault.toString() };
  console.log(JSON.stringify({ ...facts, vault_sol: sol(vault), net_sol: sol(-s.cost) }, null, 1));
  if (!live) return console.log(`preview code: ${code(facts)}`);
  if (confirm !== code(facts) && confirm !== code(facts, 1)) throw new Error("confirm code does not match a fresh preview");
  console.log(`sent: ${await send(tx, [payer], blockhash, lastValidBlockHeight)}`);
}

async function sweep(live) {
  const old = keypair("OLD_KEY");
  const dest = new PublicKey(arg);
  const bal = await conn.getBalance(old.publicKey);
  const tokens = await conn.getParsedTokenAccountsByOwner(old.publicKey, { programId: new PublicKey("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA") });
  const amount = bal - 5000 - Math.ceil((CFG.cu_limit * CFG.cu_price) / 1e6);
  const { tx, blockhash, lastValidBlockHeight } = await build(old.publicKey, [SystemProgram.transfer({ fromPubkey: old.publicKey, toPubkey: dest, lamports: amount })]);
  await simulate(tx, old.publicKey);
  const facts = { task: "sweep", from: old.publicKey.toBase58(), to: dest.toBase58(), lamports: amount };
  console.log(JSON.stringify({ ...facts, amount_sol: sol(amount), token_accounts_left: tokens.value.length }, null, 1));
  if (!live) return console.log(`preview code: ${code(facts)}`);
  if (confirm !== code(facts) && confirm !== code(facts, 1)) throw new Error("confirm code does not match a fresh preview");
  console.log(`sent: ${await send(tx, [old], blockhash, lastValidBlockHeight)}`);
}

async function status() {
  const payer = keypair("LAUNCH_KEY").publicKey;
  const online = new OnlinePumpSdk(conn);
  const rows = [];
  for (const c of CFG.coins) {
    const mint = mintFor(c.id).publicKey;
    const live = await conn.getAccountInfo(mint);
    const bc = live ? await online.fetchBondingCurve(mint) : null;
    rows.push({ id: c.id, mint: mint.toBase58(), launched: !!live, complete: bc?.complete ?? null, real_sol: bc ? sol(bc.realQuoteReserves ?? bc.realSolReserves) : null });
  }
  console.log(JSON.stringify({ payer: payer.toBase58(), balance_sol: sol(await conn.getBalance(payer)),
    creator_vault_sol: sol(await online.getCreatorVaultBalanceBothPrograms(payer)), coins: rows }, null, 1));
}

async function dry(id, payerAddr) {
  const c = coin(id);
  const payer = new PublicKey(payerAddr);
  const mint = Keypair.generate().publicKey;
  const { tx } = await build(payer, await createIxs(c, payer, mint));
  console.log(JSON.stringify({ id, payer: payer.toBase58(), test_mint: mint.toBase58(), ...(await simulate(tx, payer)) }, null, 1));
}

const tasks = {
  address: async () => console.log(keypair("LAUNCH_KEY").publicKey.toBase58()),
  dry: () => dry(arg, confirm),
  preview: () => launch(arg, false),
  send: () => launch(arg, true),
  claim_preview: () => claim(false),
  claim: () => claim(true),
  sweep_preview: () => sweep(false),
  sweep: () => sweep(true),
  status,
};
if (!tasks[task]) throw new Error(`task must be one of ${Object.keys(tasks).join(", ")}`);
await tasks[task]();
