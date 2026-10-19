// Offline tests for scripts/keeper.mjs planning logic. Run: node tests/test_keeper.mjs
import { plan, planMore, seedAmounts, GEN } from "../scripts/keeper.mjs";

{
const now = 1_800_000_000;
const m = (id, h) => ({ id, league: "eng.1", home: "A" + id, away: "B", kickoff: now + h * 3600 });
const matches = [m("1", 0.5), m("2", 2), m("3", 30), m("4", 200), m("5", 5), { id: "6", kickoff: now + 7200 }];
const listed = { "eng.1/5": 3, "eng.1/2": -1 };
const fx = (id, st, ko, feed = "eng.1/9") => ({ id, status: st, feed, kickoff: ko, settle_opens: ko + 7200, expires: ko + 7 * 86400, home: "x", away: "y" });
const fixtures = [fx(0, "SCHEDULED", now - 3 * 3600), fx(1, "SCHEDULED", now - 3600), fx(2, "FINAL", now - 9000), fx(3, "SCHEDULED", now - 8 * 86400)];
const p = plan({ now, matches, listed, fixtures });
console.log(p.toList.map(x => x.id), p.toSettle.map(x => x.id), p.toExpire.map(x => x.id));
if (JSON.stringify([p.toList.map(x => x.id), p.toSettle.map(x => x.id), p.toExpire.map(x => x.id)]) !== JSON.stringify([["2","3"],[0],[3]])) { console.log("FAIL"); process.exit(1); }
console.log("plan ok");
}
{
const assert = (c, m) => { if (!c) { console.log("FAIL", m); process.exit(1); } };
const now = 1_800_000_000;
const s = seedAmounts({ HOME: 0.5, DRAW: 0.25, AWAY: 0.25 });
assert(s.HOME === 15n * GEN / 100n && s.DRAW === 75n * GEN / 1000n, "seed amounts " + JSON.stringify(s, (k, v) => String(v)));
const fx = (id, st, ko, extra = {}) => ({ id, status: st, feed: "eng.1/" + id, kickoff: ko, markets: {}, ...extra });
const fixtures = [fx(0, "SCHEDULED", now + 7200), fx(1, "SCHEDULED", now + 9000), fx(2, "SCHEDULED", now + 99999), fx(3, "SCHEDULED", now + 600),
  fx(4, "FINAL", now - 9000, { markets: { RESULT: { winner: "HOME" }, GOALS: { winner: "UNDER" } } }), fx(5, "VOID", now - 9000)];
const positions = { 0: { stakes: { RESULT: { HOME: "1" } } }, 4: { claimable: "5", claimed: false }, 5: { claimable: "0", claimed: false } };
const probsByKey = { "eng.1/1": { RESULT: { HOME: 0.6, DRAW: 0.2, AWAY: 0.2 }, GOALS: { OVER: 0.5, UNDER: 0.5 }, BTTS: { YES: 0.5, NO: 0.5 } } };
const tickets = [
  { id: 0, status: "OPEN", legs: [[4, "RESULT", "AWAY", 300], [0, "RESULT", "HOME", 200]] },   // leg lost -> ready
  { id: 1, status: "OPEN", legs: [[4, "RESULT", "HOME", 300], [0, "RESULT", "HOME", 200]] },   // pending
  { id: 2, status: "OPEN", legs: [[4, "GOALS", "UNDER", 200], [5, "RESULT", "HOME", 200]] },  // decided
];
const q = planMore({ now, fixtures, positions, probsByKey, tickets, house: { free: String(GEN) } });
const seededGames = [...new Set(q.seeds.map((x) => x.fid))];
assert(JSON.stringify(seededGames) === "[0,1]", "seed games " + seededGames);
assert(q.seeds.filter((x) => x.fid === 0).length === 6, "fixture 0 skips HOME already staked");
assert(q.seeds.find((x) => x.fid === 1 && x.outcome === "HOME").value === 18n * GEN / 100n, "uses feed probs");
assert(JSON.stringify(q.claims) === "[4]", "claims " + q.claims);
assert(JSON.stringify(q.comboSettles) === "[0,2]", "combos " + q.comboSettles);
assert(q.topUp === 3n * GEN, "top up");
assert(planMore({ now, fixtures, positions, probsByKey, tickets: [], house: { free: String(5n * GEN) } }).topUp === 0n, "no top up");
console.log("planMore ok");
}
