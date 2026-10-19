// Project: Ninety. Keeper bot, run hourly by .github/workflows/keeper.yml.
// 1. Lists upcoming top-league matches from frontend/fixtures.json (ESPN feed),
//    so visitors always find games ready to predict on.
// 2. Calls settle() on finished matches, so results land without anyone clicking.
// The keeper has no special power: list_match and settle are open to everyone,
// and the contract reads teams, kickoff and score from the feed itself.
//
// Secrets / env:
//   NINETY_KEEPER_KEY  private key of the keeper wallet (0x...), GitHub secret
//   NINETY_CONTRACT    optional, overrides DEFAULT_CONTRACT
//   DRY_RUN=1          print the plan, send nothing

import { readFileSync } from "node:fs";

export const DEFAULT_CONTRACT = "0xD4807A5EBeBc9fb8aD071364Db452dd809a09129"; // set after deploy
export const LIST_AHEAD_DAYS = 4;    // keep the next 4 days of games open
export const MIN_LEAD_SEC = 45 * 60; // contract needs 10 min; leave room for consensus
export const MAX_LIST = 6;           // per run, keeps each run short
export const MAX_SETTLE = 6;
const SETTLE_RETRY_SEC = 50 * 60;    // a PENDING match is retried next run

// Pure: decide what to do this run. Exported for tests.
export function plan({ now, matches, listed, fixtures }) {
  const toList = matches
    .filter((m) => m.league && m.id && !(listed[`${m.league}/${m.id}`] >= 0))
    .filter((m) => m.kickoff >= now + MIN_LEAD_SEC && m.kickoff <= now + LIST_AHEAD_DAYS * 86400)
    .sort((a, b) => a.kickoff - b.kickoff)
    .slice(0, MAX_LIST);
  const open = fixtures.filter((f) => f.status === "SCHEDULED" && f.feed);
  const toSettle = open
    .filter((f) => now >= f.settle_opens && now < f.expires)
    .sort((a, b) => a.kickoff - b.kickoff)
    .slice(0, MAX_SETTLE);
  const toExpire = open.filter((f) => now >= f.expires).slice(0, 3);
  return { toList, toSettle, toExpire, retryAfter: SETTLE_RETRY_SEC };
}

async function main() {
  const dry = process.env.DRY_RUN === "1";
  const address = process.env.NINETY_CONTRACT || DEFAULT_CONTRACT;
  if (/^0x0{40}$/.test(address)) throw new Error("set DEFAULT_CONTRACT or NINETY_CONTRACT");
  const { createClient, createAccount } = await import("genlayer-js");
  const { testnetBradbury } = await import("genlayer-js/chains");

  const key = process.env.NINETY_KEEPER_KEY;
  const account = key ? createAccount(key.trim()) : null;
  if (!account && !dry) throw new Error("NINETY_KEEPER_KEY secret is missing");
  const client = createClient({ chain: testnetBradbury, ...(account ? { account } : {}) });
  if (account) console.log(`keeper address: ${account.address}  (fund this address with a little GEN)`);

  const read = (functionName, args = []) => client.readContract({ address, functionName, args });
  const now = Math.floor(Date.now() / 1000);
  const feed = JSON.parse(readFileSync("frontend/fixtures.json", "utf8"));
  const matches = feed.matches || [];
  const keys = matches.filter((m) => m.league).map((m) => `${m.league}/${m.id}`);
  const listed = {};
  for (let i = 0; i < keys.length; i += 90) Object.assign(listed, JSON.parse(await read("get_listed", [keys.slice(i, i + 90).join(",")])));
  const fixtures = [];
  for (let off = 0; ; off += 50) {
    const page = JSON.parse(await read("get_fixtures", [off, 50]));
    fixtures.push(...page.fixtures);
    if (off + 50 >= page.total) break;
  }
  const p = plan({ now, matches, listed, fixtures });
  console.log(`feed matches: ${matches.length}, on Ninety: ${fixtures.length}`);
  console.log(`plan: list ${p.toList.length}, settle ${p.toSettle.length}, expire ${p.toExpire.length}`);
  if (dry) { console.log(JSON.stringify(p, null, 1)); return; }

  async function send(functionName, args, label) {
    try {
      const hash = await client.writeContract({ address, functionName, args, value: 0n });
      console.log(`${label}: sent ${hash}`);
      const r = await client.waitForTransactionReceipt({ hash, waitUntil: "decided", interval: 5000, retries: 72 });
      const result = r?.consensus_data?.leader_receipt?.[0]?.result ?? r?.data?.result ?? "";
      console.log(`${label}: ${r?.statusName || r?.status} ${r?.txExecutionResultName || ""} ${typeof result === "string" ? result.slice(0, 80) : ""}`);
    } catch (e) {
      console.log(`${label}: failed (${String(e?.shortMessage || e?.message || e).slice(0, 160)}), will retry next run`);
    }
  }
  for (const m of p.toList) await send("list_match", [m.league, Number(m.id)], `list ${m.home} v ${m.away}`);
  for (const f of p.toSettle) await send("settle", [f.id], `settle #${f.id} ${f.home} v ${f.away}`);
  for (const f of p.toExpire) await send("expire", [f.id], `expire #${f.id}`);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((e) => { console.error(e?.message || e); process.exit(1); });
}
