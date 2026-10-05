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

### GenLayer Studio (studionet) — live run, 3 Oct 2026

| Item | Value |
| --- | --- |
| Listing accepted (Arsenal v Leeds United, fixture #0, Flashscore source) | `0x5b8b559d257e7366b4a17c8398e39f181dcaf6042e7006bf6d522cdb4a49b778` |
| Stake 1 GEN on RESULT / HOME | `0xe3efb54e8e4971255d654fe494a3f80410a6c232405452bfe0bcd42aa9810acd` |
| Listing refused: source page failed to load (validators agreed, no fixture created) | `0xebfbb7ab6e1d582c6c9fe1429d320904460126bfcbd68ee250e7ac6f4abf1a0b` |

What the live run showed:

- **Listing consensus.** The leader (gpt-oss) read the Flashscore match page and judged the fixture valid. Validators running GPT-5.4, DeepSeek and GPT-5 each re-read the page independently and **agreed**, comparing only the `valid` decision. Stored listing note: *"Page shows Arsenal (home) vs Leeds United on 10 Oct 2026 in the Premier League."*
- **Failure consensus.** When a source page could not be loaded (ESPN, BBC), every validator independently hit `WEBPAGE_LOAD_FAILED`. They agreed on the `[TRANSIENT]` error, and no fixture was created. Nothing was guessed.
- **JavaScript pages.** Pages are rendered with `wait_after_loaded="4s"`. Without it, Flashscore's scores have not loaded yet when the page is read.
- **Staking.** `get_fixtures` shows `RESULT.pools.HOME = 1000000000000000000` (1 GEN).

The Studio run used an earlier, longer revision of the contract with identical logic. The compact file in this repo is the one deployed to Bradbury.

### Testnet Bradbury

| Item | Value |
| --- | --- |
| Network | Testnet Bradbury (chain 4221) |
| Contract | [`0x9756e7cDF6A59cd0F57A1298e5cA23d265D0950c`](https://explorer-bradbury.genlayer.com/address/0x9756e7cDF6A59cd0F57A1298e5cA23d265D0950c) |
| Deploy tx | [`0x535813200cd4f689b1612bb7957184e26ce9de97019b892128364de3265cec8a`](https://explorer-bradbury.genlayer.com/tx/0x535813200cd4f689b1612bb7957184e26ce9de97019b892128364de3265cec8a) (`FINISHED_WITH_RETURN`) |
| Listing accepted tx | _fill in_ |
| Stake tx | _fill in_ |
| Settle (FINAL) tx | _after kickoff + 2 h_ |
| Claim tx | _fill in_ |
| Frontend | _fill in_ |

Two lessons from deploying to Bradbury:

- Source must stay under about 20 KB because of the 16.7M per-transaction gas ceiling (about 730 gas per byte of source). A 28 KB revision was rejected with `gas limit too high`, so `contracts/ninety.py` is written compactly (about 17.5 KB, tab indentation).
- GenVM joins **all** leading `#` lines into the JSON runner header. Comment lines placed directly under the `Depends` line made the deploy fail with `invalid_contract: trailing characters at line 1 column 84` (failed deploy tx `0x1dd82ac978ff6034dc38a15002785ce7b429430b9ece215ece6d71ee13700ea6`). Line 2 of the contract is therefore blank.
