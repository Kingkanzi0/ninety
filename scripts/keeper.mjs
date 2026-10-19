// Project: Ninety. Keeper bot, run hourly by .github/workflows/keeper.yml.
// 1. Lists upcoming top-league matches from frontend/fixtures.json (ESPN feed),
//    so visitors always find games ready to predict on.
// 2. Seeds starting odds: a small stake on every outcome, sized from bookmaker
//    probabilities in fixtures.json (or defaults), so odds show from the start.
// 3. Calls settle() on finished matches, then claim() for its own seed money.
// 4. Settles finished combo tickets and keeps the combo house bankroll topped up.
// The keeper has no special power: every method it calls is open to everyone,
// and the contracts read teams, kickoff and score from the feed themselves.
//
// Secrets / env:
//   NINETY_KEEPER_KEY  private key of the keeper wallet (0x...), GitHub secret
//   NINETY_CONTRACT    optional, overrides DEFAULT_CONTRACT
//   COMBOS_CONTRACT    optional, overrides DEFAULT_COMBOS
//   DRY_RUN=1          print the plan, send nothing

import { readFileSync } from "node:fs";

export const DEFAULT_CONTRACT = "0xD4807A5EBeBc9fb8aD071364Db452dd809a09129"; // set after deploy
export const LIST_AHEAD_DAYS = 4;    // keep the next 4 days of games open
export const MIN_LEAD_SEC = 45 * 60; // contract needs 10 min; leave room for consensus
export const MAX_LIST = 6;           // per run, keeps each run short
export const MAX_SETTLE = 6;
export const DEFAULT_COMBOS = "0x2CF0247D078C738acac807025Df1F60e9057C02C"; // set after deploy
export const GEN = 10n ** 18n;
export const SEED_PER_MARKET = 3n * GEN / 10n;   // 0.3 GEN spread across a market's outcomes
export const MAX_SEED_GAMES = 2;                 // per run (7 stakes per game)
export const MAX_CLAIM = 6;
export const MAX_COMBO_SETTLE = 6;
export const HOUSE_MIN = 2n * GEN, HOUSE_TOPUP = 3n * GEN;
export const DEFAULT_PROBS = { RESULT: { HOME: 0.45, DRAW: 0.27, AWAY: 0.28 }, GOALS: { OVER: 0.52, UNDER: 0.48 }, BTTS: { YES: 0.52, NO: 0.48 } };
const SETTLE_RETRY_SEC = 50 * 60;    // a PENDING match is retried next run
const ZERO = /^0x0{40}$/;

// Stakes (wei) to seed one market from probabilities.
export function seedAmounts(probs, total = SEED_PER_MARKET) {
  const out = {};
  for (const [o, pr] of Object.entries(probs)) out[o] = BigInt(Math.max(1, Math.round(pr * 1000))) * total / 1000n;
  return out;
}

// Which seed stakes are still missing for a fixture, given the keeper's position.
export function seedsFor(fixture, probsByKey, position) {
  const probs = probsByKey[fixture.feed] || DEFAULT_PROBS;
  const have = position?.stakes || {};
  const todo = [];
  for (const m of ["RESULT", "GOALS", "BTTS"]) {
    const amounts = seedAmounts(probs[m] || DEFAULT_PROBS[m]);
    for (const [o, v] of Object.entries(amounts)) if (!have[m]?.[o]) todo.push({ fid: fixture.id, market: m, outcome: o, value: v });
  }
  return todo;
}

// Can a combo ticket be settled now? (one leg lost, or every leg decided)
export function ticketReady(ticket, byId) {
  let pending = false;
  for (const [fid, m, o] of ticket.legs) {
    const f = byId[fid];
    if (!f) return false;
    if (f.status === "VOID") continue;
    if (f.status !== "FINAL") { pending = true; continue; }
    const w = f.markets?.[m]?.winner;
    if (w !== "VOID" && w !== o) return true;
  }
  return !pending;
}

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

// Pure: seeding, claims and combo work for this run. Exported for tests.
export function planMore({ now, fixtures, positions, probsByKey, tickets = [], house = null }) {
  const seeds = [];
  let games = 0;
  for (const f of fixtures.filter((x) => x.status === "SCHEDULED" && x.feed && x.kickoff > now + 1800).sort((a, b) => a.kickoff - b.kickoff)) {
    const todo = seedsFor(f, probsByKey, positions[f.id]);
    if (!todo.length) continue;
    seeds.push(...todo);
    if (++games >= MAX_SEED_GAMES) break;
  }
  const claims = fixtures
    .filter((f) => (f.status === "FINAL" || f.status === "VOID") && positions[f.id] && !positions[f.id].claimed && BigInt(positions[f.id].claimable || 0) > 0n)
    .slice(0, MAX_CLAIM).map((f) => f.id);
  const byId = Object.fromEntries(fixtures.map((f) => [f.id, f]));
  const comboSettles = tickets.filter((t) => t.status === "OPEN" && ticketReady(t, byId)).slice(0, MAX_COMBO_SETTLE).map((t) => t.id);
  const topUp = house && BigInt(house.free) < HOUSE_MIN ? HOUSE_TOPUP : 0n;
  return { seeds, claims, comboSettles, topUp };
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
  const probsByKey = Object.fromEntries(matches.filter((m) => m.league && m.probs).map((m) => [`${m.league}/${m.id}`, m.probs]));
  const positions = {};
  if (account) {
    for (const f of fixtures.filter((x) => x.feed && (x.status === "SCHEDULED" || now - (x.settled_ts || 0) < 14 * 86400))) {
      try { positions[f.id] = JSON.parse(await read("get_position", [f.id, account.address])); } catch { /* skip */ }
    }
  }
  const combos = process.env.COMBOS_CONTRACT || DEFAULT_COMBOS;
  let tickets = [], house = null;
  if (!ZERO.test(combos)) {
    try {
      house = JSON.parse(await client.readContract({ address: combos, functionName: "get_house", args: [] }));
      tickets = JSON.parse(await client.readContract({ address: combos, functionName: "get_open_tickets", args: [0, 50] }));
    } catch (e) { console.log(`combos read failed (${String(e?.message || e).slice(0, 120)})`); }
  }
  const q = planMore({ now, fixtures, positions, probsByKey, tickets, house });
  console.log(`feed matches: ${matches.length}, on Ninety: ${fixtures.length}`);
  console.log(`plan: list ${p.toList.length}, settle ${p.toSettle.length}, expire ${p.toExpire.length}, seed ${q.seeds.length}, claim ${q.claims.length}, combos ${q.comboSettles.length}, house top-up ${q.topUp > 0n ? "yes" : "no"}`);
  if (dry) { console.log(JSON.stringify({ ...p, ...q }, (k, v) => (typeof v === "bigint" ? v.toString() : v), 1)); return; }

  async function send(functionName, args, label, value = 0n, to = address) {
    try {
      const hash = await client.writeContract({ address: to, functionName, args, value });
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
  for (const sd of q.seeds) await send("stake", [sd.fid, sd.market, sd.outcome], `seed #${sd.fid} ${sd.market} ${sd.outcome}`, sd.value);
  for (const fid of q.claims) await send("claim", [fid], `claim seed #${fid}`);
  if (!ZERO.test(combos)) {
    for (const tid of q.comboSettles) await send("settle_ticket", [tid], `settle combo #${tid}`, 0n, combos);
    if (q.topUp > 0n) await send("fund", [], "top up combo house", q.topUp, combos);
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((e) => { console.error(e?.message || e); process.exit(1); });
}
