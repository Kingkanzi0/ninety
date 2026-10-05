# Ninety

Soccer prediction markets settled by GenLayer validators reading the match pages.

Every fixture opens four pari-mutuel markets:

| Market | Outcomes | Settles on |
| --- | --- | --- |
| Match result | Home / Draw / Away | 90 minutes + stoppage |
| Total goals | Over / Under 2.5 | regular-time goals |
| Both teams score | Yes / No | regular-time goals |
| Correct score | any score, e.g. `2-1` (open pool) | exact regular-time score |

Stakes go into shared pots; winners split the pot of their market in proportion to their stake.

## Why it needs GenLayer

A normal smart contract cannot read a football website, and a single oracle operator would have to be trusted to report the score honestly. In Ninety, the contract itself makes two judgments under GenLayer consensus, and nothing else:

1. **Listing check** (`create_fixture`). Validators open the cited match page and agree whether it really shows *this* fixture: same teams, same home/away order, same date. Fake or mismatched fixtures are stored as `REJECTED` and can never take stakes.
2. **Fact extraction** (`settle`). Validators each read up to two independent match pages (for example BBC and ESPN), extract the regular-time score and match status from each, and must agree on one verdict: `FINAL h-a`, `PENDING`, `CONFLICT` or `VOID`.

All four markets are then settled **deterministically** from the agreed score by plain Python. There is no LLM involved in payouts, and there is no admin function that can set an outcome.

## Consensus design

| Step | Evidence | Pattern | Compared by validators | Not compared |
| --- | --- | --- | --- | --- |
| Listing | `gl.nondet.web.render(source_a, mode="text")` | `gl.vm.run_nondet_unsafe(leader_fn, validator_fn)` | `valid` (bool) | `note` |
| Settlement | `gl.nondet.web.render(url, mode="text")` for each source | `gl.vm.run_nondet_unsafe(leader_fn, validator_fn)` | `verdict`, plus `home` and `away` when `verdict == FINAL` | per-source evidence text |

Each validator re-runs the full task independently (fetch + extract + merge) and compares only the structured decision fields. It never trusts the leader's output, and free-text reasoning never decides consensus.

How two sources are merged (`merge_sources`, deterministic):

| Source reports | Verdict | Effect |
| --- | --- | --- |
| all `FINISHED`, same score | `FINAL` | markets settle |
| all `FINISHED`, different scores | `CONFLICT` | retry; 3 conflicts → `VOID` (refund) |
| any `NOT_FINISHED` / `NOT_FOUND` / unreachable | `PENDING` | retry later, no penalty |
| all `POSTPONED` / `ABANDONED` | `VOID` | full refund |
| mixed, or score after extra time not shown | `CONFLICT` | retry |

Failure handling:
- Errors are classified with prefixes. `[TRANSIENT]` errors on both sides agree. `[LLM_ERROR]`, such as malformed model output, always disagrees, which forces leader rotation.
- `expire()` is permissionless. It voids any fixture still unsettled 7 days after kickoff, so funds can never be stuck.
- If nobody backed the winning outcome of a market, that market is refunded instead of locking the pot.

Prompt-injection hardening: page text is passed between explicit markers and the model is told to treat it as data only. Outputs are normalized against fixed enums, so arbitrary text cannot become an outcome.

## Storage design

Storage uses flat `TreeMap`s only. There are no dataclass storage objects.

- Fixture fields have one map each, keyed by fixture id: `f_home`, `f_away`, `f_kickoff`, `f_status`, …
- Market accounting uses composite string keys:
  - `market_total["fid:MKT"]`
  - `pools["fid:MKT:OUTCOME"]`
  - `stakes["fid:MKT:OUTCOME:addr"]`
  - `user_total["fid:MKT:addr"]`
  - `market_winner["fid:MKT"]`
  - `claimed["fid:addr"]`

## Contract API — `contracts/ninety.py`

| Method | Type | Notes |
| --- | --- | --- |
| `create_fixture(home_team, away_team, competition, kickoff_ts, source_a, source_b)` | write | `source_b` may be `""`; kickoff 10 min – 60 days ahead; returns fixture id |
| `stake(fixture_id, market, outcome)` | write, payable | market `RESULT`/`GOALS`/`BTTS`/`SCORE`; open until kickoff |
| `settle(fixture_id)` | write | permissionless, from kickoff + 2 h |
| `expire(fixture_id)` | write | permissionless, from kickoff + 7 days |
| `claim(fixture_id)` | write | pays winnings + refunds across all four markets |
| `get_fixtures(offset, limit)` | view | JSON string, newest first |
| `get_fixture(fixture_id)` | view | JSON string |
| `get_position(fixture_id, user)` | view | JSON string with stakes and claimable amount |
| `get_fixture_count()` | view | int |

Payouts to wallets use `@gl.evm.contract_interface` + `emit_transfer`, the documented route for sending GEN to an EOA.

## Repository layout

```
contracts/ninety.py         the Intelligent Contract
frontend/index.html         the dApp (single static page, genlayer-js from a CDN)
tests/test_ninety.py        offline lifecycle tests
tests/mock_genlayer.py      minimal SDK stand-in used by the tests
```

## Testing

Offline tests cover:
- the listing check
- staking rules
- all settlement verdicts
- validator disagreement, including a leader reporting a false score
- refunds and expiry
- pari-mutuel arithmetic
- a guard that fails if `web.get`, `strict_eq` or dataclass storage ever appear in the contract

```
python3 tests/test_ninety.py
```

The mock is not GenVM. Always confirm behaviour live in Studio and on Bradbury, and record the transaction hashes (see below).

## Deploy

### 1. Studio (quick live check)

1. Open <https://studio.genlayer.com/contracts>, create `ninety.py`, and paste `contracts/ninety.py`.
2. Deploy. The constructor takes no arguments.
3. Call `create_fixture` with a real upcoming match and its match-page URL, and a `kickoff_ts` (unix seconds) in the future.
4. Check `get_fixtures(0, 10)` shows it as `SCHEDULED` with a listing note.

### 2. Bradbury testnet (persistent, for submission)

```
npm install -g genlayer
genlayer network set testnet-bradbury     # if it prompts, pick Testnet Bradbury
genlayer account create                   # or: genlayer account import
# fund the account: https://testnet-faucet.genlayer.foundation/
genlayer deploy --contract contracts/ninety.py
```

Copy the deployed address and the deploy transaction hash. Check both on <https://explorer-bradbury.genlayer.com>.

### 3. Frontend

Put your address into `DEFAULT_CONTRACT` near the top of `frontend/index.html`, or open the page with `?contract=0x…`.

To host on GitHub Pages: Settings → Pages → deploy from branch `main`, folder `/frontend`.

The page uses the `testnetBradbury` chain from genlayer-js. Add `?network=studionet` to point it at Studio instead.

### Choosing good match pages

- Use pages that show the score as text after full time, such as BBC Sport and ESPN match pages, or a league's official match centre.
- Use two different sites, so one wrong page cannot settle a market on its own.
- Avoid pages behind logins, cookie walls or heavy paywalls. Validators will mark them unreachable, and the market will stay `PENDING`.

## Submission checklist

- [ ] `contracts/ninety.py` is on `main` and opens directly in the GitHub browser view
- [ ] Bradbury contract address + Explorer link in this README
- [ ] Live transactions recorded below: deploy, `create_fixture` (accepted), a rejected listing, `stake`, `settle` (FINAL), `claim`
- [ ] Live frontend URL
- [ ] `python3 tests/test_ninety.py` passes

## Deployment record

| Item | Value |
| --- | --- |
| Network | Testnet Bradbury (chain 4221) |
| Contract | _fill in_ |
| Deploy tx | _fill in_ |
| Listing accepted tx | _fill in_ |
| Listing rejected tx | _fill in_ |
| Stake tx | _fill in_ |
| Settle (FINAL) tx | _fill in_ |
| Claim tx | _fill in_ |
| Frontend | _fill in_ |
