# Ninety


## Milestone 2: sportsbook interface and auto-updating fixtures (Oct 2026)

Ninety's first version was accepted on Oct 7, 2026. This milestone adds the following, all live at https://kingkanzi0.github.io/ninety/frontend/

| Area | Before (accepted version) | After (this milestone) |
| --- | --- | --- |
| Interface | Single fixture list and detail page | Sportsbook layout: competitions rail, match list with 1/X/2 odds, match page with all four markets |
| Betting | One stake at a time inside a market card | Bet slip with several picks, pool-based odds and possible return before staking |
| History | Position shown per match only | "My bets": every stake across all matches marked won, lost or refunded, with one-click Collect |
| Consensus visibility | Generic "sending" message | Live tracker showing each GenLayer stage: network queue, leader validator, validators re-checking, consensus reached |
| Finding matches | Every match typed in by hand | Suggested matches: a GitHub Action (`.github/workflows/fixtures.yml`, `scripts/fetch_fixtures.py`) fetches 10 days of fixtures from 11 leagues every 6 hours (236 on the first run). One click opens the listing form pre-filled |
| Wallets | One injected wallet | Wallet chooser (OKX Wallet, MetaMask), remembered between visits |
| Mobile | Basic | Phone layout with slide-up bet slip |

The Intelligent Contract is unchanged (`0xF94F652d77249feE167d888F2a7AE14858FeD2eb`). Validators still decide only the facts, and payouts stay plain code. A second match, French Guiana v Belize, has settled on Bradbury through validator consensus.


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

- [x] `contracts/ninety.py` is on `main` and is the exact file deployed to Bradbury
- [x] Bradbury contract address + Explorer link in this README
- [x] Live transactions on Bradbury: deploy, `create_fixture` (accepted), `stake`, `settle` (FINAL), `claim`
- [x] Live frontend URL
- [x] `python3 tests/test_ninety.py` passes

## Deployment record

### ✅ Current live deployment — Testnet Bradbury (chain 4221)

| Item | Value |
| --- | --- |
| Contract | [`0xF94F652d77249feE167d888F2a7AE14858FeD2eb`](https://explorer-bradbury.genlayer.com/address/0xF94F652d77249feE167d888F2a7AE14858FeD2eb) |
| Deploy tx | [`0x5a8c6497…656f4f`](https://explorer-bradbury.genlayer.com/tx/0x5a8c64979a6d65d4c46e100254c33c1c7c648bc7fc002f94fc527e680b656f4f) — `ACCEPTED`, `FINISHED_WITH_RETURN` |
| Live app | <https://kingkanzi0.github.io/ninety/frontend/> |
| All transactions | listed on the [contract page](https://explorer-bradbury.genlayer.com/address/0xF94F652d77249feE167d888F2a7AE14858FeD2eb) |

Full lifecycle completed on Bradbury, through the live app:

1. **Listing check:** Arsenal v Leeds United (fixture #0, Premier League) and Martinique v El Salvador (fixture #1, CONCACAF Nations League). Validators opened each Flashscore page and agreed it showed that fixture, so both were stored as `SCHEDULED`.
2. **Stakes:** on fixture #1, 0.2 GEN on RESULT/AWAY and 1 GEN on GOALS/UNDER ([tx](https://explorer-bradbury.genlayer.com/tx/0xe7170f3c6c0de09a5dd8b2ca7082be6a41b38dcf5d2f1ff59f325d7b5fb19f23), FINALIZED).
3. **Settlement:** after full time, validators each read the match page independently and agreed on **FINAL 1–1**. GOALS settled to UNDER. RESULT settled to DRAW, which nobody had backed, so that market was refunded automatically instead of locking funds.
4. **Claim:** winnings plus the refund were paid to the staker's wallet via `emit_transfer`.

### Superseded Bradbury contract

`0x9756e7cDF6A59cd0F57A1298e5cA23d265D0950c` (deploy tx `0x535813200cd4f689b1612bb7957184e26ce9de97019b892128364de3265cec8a`). Deployed fine, but its listings timed out on Bradbury (`VALIDATORS_TIMEOUT`, then `LEADER_TIMEOUT`). The fix in the current contract:

- page text given to the LLM cut from 10,000 to 4,000 characters
- render wait cut from 4s to 3s
- `source_b = "none"` allowed for single-source fixtures

### GenLayer Studio (studionet) — live run, 3 Oct 2026

| Item | Value |
| --- | --- |
| Listing accepted (Arsenal v Leeds United, Flashscore source) | `0x5b8b559d257e7366b4a17c8398e39f181dcaf6042e7006bf6d522cdb4a49b778` |
| Stake 1 GEN on RESULT / HOME | `0xe3efb54e8e4971255d654fe494a3f80410a6c232405452bfe0bcd42aa9810acd` |
| Listing refused: source page failed to load (validators agreed, no fixture created) | `0xebfbb7ab6e1d582c6c9fe1429d320904460126bfcbd68ee250e7ac6f4abf1a0b` |

What the Studio run showed:

- **Listing consensus.** The leader read the Flashscore match page and judged the fixture valid. Validators running different models (GPT-5.4, DeepSeek, GPT-5) each re-read the page independently and **agreed**, comparing only the `valid` decision.
- **Failure consensus.** When a source page could not be loaded (ESPN, BBC), every validator independently hit `WEBPAGE_LOAD_FAILED`. They agreed on the `[TRANSIENT]` error and no fixture was created. Nothing was guessed.
- **JavaScript pages.** Pages are rendered with a short `wait_after_loaded`. Without it, Flashscore's score has not loaded yet when the page is read.

### Lessons from deploying to Bradbury (covered by tests)

- Source must stay under about 20 KB because of the 16.7M per-transaction gas ceiling. A 28 KB revision was rejected with `gas limit too high`.
- GenVM joins **all** leading `#` lines into the JSON runner header. Comment lines directly under the `Depends` line made a deploy fail with `invalid_contract: trailing characters at line 1 column 84` (failed deploy tx `0x1dd82ac978ff6034dc38a15002785ce7b429430b9ece215ece6d71ee13700ea6`). Line 2 of the contract is therefore blank.
