# Ninety

Soccer prediction markets settled by GenLayer validators. Visitors pick a game and predict: matches open automatically from a public match feed, results are read by validators after full time, with an AI page check as backup.

Every fixture opens four pari-mutuel markets:

| Market | Outcomes | Settles on |
| --- | --- | --- |
| Match result | Home / Draw / Away | 90 minutes + stoppage |
| Total goals | Over / Under 2.5 | regular-time goals |
| Both teams score | Yes / No | regular-time goals |
| Correct score | any score, e.g. `2-1` (open pool) | exact regular-time score |

Stakes go into shared pots; winners split the pot of their market in proportion to their stake.

## What changed in v3

v2 asked whoever listed a match to find and paste a Flashscore link, and every validator then had to load that heavy page, wait for its scripts and read it with an LLM. That made listing awkward and results slow on Bradbury. v3 removes both problems:

- **No links.** Matches come from ESPN's public match feed (`site.api.espn.com`, a small JSON document, not the espn.com website). A keeper bot opens the next four days of top-league games every hour, and anyone can open another listed game in one click.
- **Fast results.** After full time, validators read the score from the same feed with a plain HTTP GET and parse it with plain Python: no browser, no page scripts, no LLM, so settlement takes one quick round.

The feed is a JSON API, so it is read with `gl.nondet.web.get` and browser-like headers, as the GenLayer docs recommend for APIs. A first v3 deploy rendered the feed in the headless browser instead, and ESPN answered `HTTP 403` to every validator (`[TRANSIENT] match feed unavailable (HTTP 403)`); the contract now also falls back to ESPN's second API host. Match pages for the AI backup are still rendered.
- **AI backup.** If the feed stays silent for 6 hours (or only shows a score after extra time), anyone can submit a match page and validators read it with an LLM, as in v2.

## Why it needs GenLayer

A normal smart contract cannot read the internet, and a single oracle operator would have to be trusted to report the score. In Ninety the contract makes the judgments itself under GenLayer consensus:

1. **Listing** (`list_match`). Every validator fetches the match from the feed and must agree on the teams, the kickoff and that the match has not started. The caller only gives a league code and an event id, so nobody can list a fake match or a wrong kickoff.
2. **Settlement** (`settle`). Every validator reads the feed and must agree on one verdict: `FINAL h-a`, `PENDING` or `VOID`.
3. **AI backup** (`settle_with_page`). Validators each read a match page with an LLM, extract the regular-time score and must agree on it.

All four markets are then settled **deterministically** from the agreed score. There is no admin function that can set an outcome.

## Consensus design

| Step | Evidence | Compared by validators | Not compared |
| --- | --- | --- | --- |
| Listing | `gl.nondet.web.get(feed_url, headers=...)`, parsed as JSON | `home`, `away`, `kickoff`, `state` | nothing else |
| Settlement | same feed | `verdict`, plus `home` and `away` when `FINAL` | the note |
| AI backup | `gl.nondet.web.render(page, mode="text", wait_after_loaded="3s")` + `gl.nondet.exec_prompt` | `verdict`, plus `home` and `away` when `FINAL` | evidence text |

All three use `gl.vm.run_nondet_unsafe(leader_fn, validator_fn)`: each validator re-runs the task independently and never trusts the leader's output.

How the feed maps to a verdict (`feed_verdict`, deterministic):

| Feed shows | Verdict |
| --- | --- |
| completed, `STATUS_FULL_TIME` / `STATUS_FINAL`, both scores | `FINAL` |
| completed after extra time or penalties (`AET`, `PEN`) | `PENDING` (the 90-minute score is not in the feed; use the AI backup) |
| `POSTPONED`, `CANCELED`, `ABANDONED`, `FORFEIT` | `VOID` (full refund) |
| not started, in play, or feed unreachable | `PENDING` (retry later, no penalty) |

Failure handling:
- `[TRANSIENT]` errors on both sides agree; `[LLM_ERROR]` (malformed model output) always disagrees, forcing leader rotation.
- In the AI backup, an unclear page counts as a conflict; after 3 conflicts the match is voided and refunded.
- `expire()` is permissionless and voids any match still unsettled 7 days after kickoff, so funds can never be stuck.
- If nobody backed the winning outcome of a market, that market is refunded.

Prompt-injection hardening (AI backup): page text is passed between explicit markers, the model is told to treat it as data only, and outputs are normalized against fixed enums.

## Keeper bot

`scripts/keeper.mjs` runs every hour from `.github/workflows/keeper.yml`. It lists the next four days of games from `frontend/fixtures.json` (up to 6 per run) and calls `settle` on finished matches (up to 6 per run). The keeper has no special rights: `list_match`, `settle` and `expire` are open to everyone, and the contract reads teams, kickoff and score from the feed itself.

Setup: create a wallet key for the keeper, store it as the repository secret `NINETY_KEEPER_KEY`, set `DEFAULT_CONTRACT` in the script, and fund the address printed in the first run's log with a little GEN. Run the workflow by hand with `dry_run = 1` to see the plan without sending anything.

## Combos (accumulators) — `contracts/combos.py`

A second, small contract adds accumulators without touching the Ninety contract. Pick 2 to 6 matches (one pick per match); the odds multiply and every pick must win.

- **Priced on Ninety.** When a ticket is placed, each leg is priced at the live Ninety pool odds for that outcome (read with a cross-contract view call, `gl.get_contract_at(ninety).view().get_fixture(id)`). Legs are capped at ×15.00, tickets at ×100.00 and 20 GEN.
- **Settled on Ninety.** `settle_ticket` reads each leg's agreed result from Ninety, so combos use exactly the same validator-agreed scores. A ticket settles as soon as one leg loses, or when every leg is decided. A postponed match or a refunded market counts as ×1.00; if every leg is void the stake is refunded.
- **Always solvent.** Winnings come from a house bankroll (`fund`, open to anyone). A ticket is only accepted if the free bankroll covers its full possible win, which is then locked until the ticket settles. The owner can only withdraw the free part.

| Method | Type | Notes |
| --- | --- | --- |
| `place(legs)` | write, payable | `legs` like `3:RESULT:HOME,5:GOALS:OVER` |
| `settle_ticket(ticket_id)` | write | permissionless; the keeper calls it every hour |
| `fund()` | write, payable | adds to the house bankroll |
| `withdraw(amount)` | write | owner only, free bankroll only |
| `get_ticket`, `get_tickets_of(user)`, `get_open_tickets(offset, limit)`, `get_house()` | view | JSON strings |

Known issue in the deployed version: `get_open_tickets` raises while no ticket has been placed yet (it reads ticket 0 when the count is 0). It works once a ticket exists, and the keeper only calls it when `get_house().tickets > 0`.

## Starting odds and team badges

Ninety is a shared pot, so a market only has odds once someone has backed it. The keeper therefore seeds every new game: a small stake (0.3 GEN per market) spread across all outcomes in proportion to bookmaker probabilities from the ESPN feed (or neutral defaults when the feed has none). The pot then starts at fair odds, real bets move them, and the keeper collects its seed back after the match. Team badges come from the same feed (`frontend/logos.json`); the lettered badge is the fallback.

## Contract size on Bradbury

Bradbury limits each transaction to 16,777,216 gas. Deploying a Python Intelligent Contract costs roughly 730 gas per byte of source, so source above about 20 KB cannot be deployed ("gas limit too high"). `contracts/ninety.py` is therefore written compactly, at about 19 KB with tab indentation and terse comments (a test fails above 19.6 KB). The full design rationale lives in this README instead of in code comments.

The Studio and Bradbury records below the v3 section belong to v2 (link-based listing). They are kept as history.

## Storage design

Storage uses flat `TreeMap`s only. There are no dataclass storage objects.

- Fixture fields have one map each, keyed by fixture id: `f_home`, `f_away`, `f_kickoff`, `f_status`, `f_feed`, …
- `feed_ids["league/event"]` stores fixture id + 1, so each feed match can be listed once.
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
| `list_match(league, event_id)` | write | league code such as `eng.1`; teams and kickoff come from the feed; kickoff 10 min to 60 days ahead; one fixture per event |
| `stake(fixture_id, market, outcome)` | write, payable | market `RESULT`/`GOALS`/`BTTS`/`SCORE`; open until kickoff |
| `settle(fixture_id)` | write | permissionless, from kickoff + 2 h, reads the feed |
| `settle_with_page(fixture_id, page_url)` | write | permissionless AI backup, from kickoff + 6 h |
| `expire(fixture_id)` | write | permissionless, from kickoff + 7 days |
| `claim(fixture_id)` | write | pays winnings and refunds across all four markets |
| `get_fixtures(offset, limit)` | view | JSON string, newest first |
| `get_fixture(fixture_id)` | view | JSON string |
| `get_listed(keys)` | view | comma list of `league/event` → fixture id or `-1` |
| `get_position(fixture_id, user)` | view | JSON string with stakes and claimable amount |
| `get_fixture_count()` | view | int |

Supported leagues: Premier League, Championship, LaLiga, Serie A, Bundesliga, Ligue 1, Eredivisie, Primeira Liga, MLS, Champions League, Europa League.

Payouts to wallets use `@gl.evm.contract_interface` + `emit_transfer`, the documented route for sending GEN to an EOA.

## Repository layout

```
contracts/ninety.py         the Intelligent Contract
contracts/combos.py         accumulators on top of Ninety
scripts/keeper.mjs          hourly keeper: lists, seeds odds, settles, claims, settles combos
scripts/fetch_fixtures.py   builds frontend/fixtures.json from the ESPN feed
frontend/index.html         the dApp (single static page, genlayer-js from a CDN)
tests/test_ninety.py        offline lifecycle tests
tests/mock_genlayer.py      minimal SDK stand-in used by the tests
```

## Testing

Offline tests cover:
- feed listing: teams and kickoff taken from the feed, duplicates, started or out-of-range matches, feed down, a lying leader
- feed settlement: final score, in play, feed down, extra time ignored, postponed, a lying leader
- AI backup: 6-hour gate, score compared but not evidence, unclear pages, malformed LLM output
- refunds, expiry and pari-mutuel arithmetic
- guards: source size under the Bradbury gas ceiling, runner header, feed via HTTP GET with a browser User-Agent, pages via render, no `strict_eq` or dataclass storage

```
python3 tests/test_ninety.py     # Ninety contract
python3 tests/test_combos.py     # combos, wired to a real Ninety instance
node tests/test_keeper.mjs       # keeper planning: listing, seeding, claims, combos
```

The mock is not GenVM. Always confirm behaviour live in Studio and on Bradbury, and record the transaction hashes (see below).

## Deploy

### 1. Studio (quick live check)

1. Open <https://studio.genlayer.com/contracts>, create `ninety.py`, and paste `contracts/ninety.py`.
2. Deploy. The constructor takes no arguments.
3. Call `list_match` with a league code and an ESPN event id of an upcoming game (both are in `frontend/fixtures.json`, for example `eng.1` and `740912`).
4. Check `get_fixtures(0, 10)` shows it as `SCHEDULED` with the teams and kickoff from the feed.

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

To host on GitHub Pages: Settings → Pages → deploy from branch `main`, folder `/ (root)`. The app is then served at `https://<user>.github.io/<repo>/frontend/`.

The page uses the `testnetBradbury` chain from genlayer-js. Add `?network=studionet` to point it at Studio instead.

### Choosing a page for the AI backup

- Use a page that shows the score as text after full time. Flashscore match pages work.
- Avoid pages behind logins, cookie walls or heavy paywalls. Validators will mark them unreachable, and the match will stay `PENDING`.

## Submission checklist

- [ ] `contracts/ninety.py` is on `main` and opens directly in the GitHub browser view
- [ ] Bradbury contract address + Explorer link in this README
- [ ] Live transactions recorded below: deploy, `list_match`, `stake`, `settle` (FINAL from the feed), `claim`, keeper run log
- [ ] Live frontend URL
- [ ] `python3 tests/test_ninety.py` passes

## Deployment record

### v3 on Testnet Bradbury (feed listing, keeper bot, AI backup)

| Item | Value |
| --- | --- |
| Contract | [`0xD4807A5EBeBc9fb8aD071364Db452dd809a09129`](https://explorer-bradbury.genlayer.com/address/0xD4807A5EBeBc9fb8aD071364Db452dd809a09129) |
| First v3 deploy (superseded) | `0xa6E51375D0893A469494BdeBA2326A94dE95D455`: `list_match` failed with `HTTP 403` because the feed was rendered in a browser |
| Deploy tx | [`0xfcfbcfbd1a2688e2cc33fa810d9a2c643d1f672ef5ca171cc0409d5463a19741`](https://explorer-bradbury.genlayer.com/tx/0xfcfbcfbd1a2688e2cc33fa810d9a2c643d1f672ef5ca171cc0409d5463a19741) (`ACCEPTED`, `FINISHED_WITH_RETURN`) |
| `list_match` from the feed | Bayern Munich v Arsenal, by CLI (all 5 validators agreed); 6 more by the keeper |
| `stake` | Monza v Cagliari (fixture #1): 0.5 GEN each on Home win, Over 2.5 and Both teams score, placed from the website |
| `settle` (FINAL from the feed) | Monza 4-0 Cagliari, settled automatically by the keeper from the match feed |
| `claim` | [`0xb582fe58…92fb055`](https://explorer-bradbury.genlayer.com/tx/0xb582fe588bcfa0f936cb7e1f6ac86757ccf6db10d706f58c9b8a0a80992fb055): `claim(1)`, 1.27 GEN paid back to the wallet (Home win and Over 2.5 won, Both teams score lost) |
| Combos contract | [`0x2CF0247D078C738acac807025Df1F60e9057C02C`](https://explorer-bradbury.genlayer.com/address/0x2CF0247D078C738acac807025Df1F60e9057C02C), deploy tx [`0x28cb84b41a9dece3e5049943dc149e5704a466e4c0538c511535429435b38ed3`](https://explorer-bradbury.genlayer.com/tx/0x28cb84b41a9dece3e5049943dc149e5704a466e4c0538c511535429435b38ed3) (`FINISHED_WITH_RETURN`). A first deploy with the Ninety address as a constructor argument failed (`FINISHED_WITH_ERROR`); the address is now a constant. |
| Keeper run (GitHub Actions log) | [run #2](https://github.com/Kingkanzi0/ninety/actions/runs/39233043004): 6 games listed in 1 min 40 s, each `FINISHED_WITH_RETURN` |


### GenLayer Studio (studionet) — live run, 3 Oct 2026

| Item | Value |
| --- | --- |
| Contract | `0x3a…6e28` (full address: _fill in from Studio's copy button_) |
| Deploy tx | `0x1102…` (_fill in full hash_) |
| Listing accepted (Arsenal v Leeds United, fixture #0, Flashscore source) | `0x5b8b559d257e7366b4a17c8398e39f181dcaf6042e7006bf6d522cdb4a49b778` |
| Stake 1 GEN on RESULT / HOME | `0xe3efb54e8e4971255d654fe494a3f80410a6c232405452bfe0bcd42aa9810acd` |
| Listing refused: source page failed to load (validators agreed, no fixture created) | `0xebfbb7ab6e1d582c6c9fe1429d320904460126bfcbd68ee250e7ac6f4abf1a0b` |
| Settle (FINAL) | _after kickoff + 2h: Sat 10 Oct 2026, 14:30 London_ |
| Claim | _fill in_ |

What the live run showed:

- **Listing consensus.** The leader (gpt-oss) read the Flashscore match page and judged the fixture valid. Validators running GPT-5.4, DeepSeek and GPT-5 each re-read the page independently and **agreed**, comparing only the `valid` decision. Stored listing note: *"Page shows Arsenal (home) vs Leeds United on 10 Oct 2026 in the Premier League."*
- **Failure consensus.** When a source page could not be loaded (ESPN, BBC), every validator independently hit `WEBPAGE_LOAD_FAILED`. They agreed on the `[TRANSIENT]` error, and no fixture was created. Nothing was guessed.
- **JavaScript pages.** Pages are rendered with a short `wait_after_loaded` (4s on Studio, 3s on Bradbury). Without it, Flashscore's scores have not loaded yet when the page is read.
- **Staking.** `get_fixtures` shows `RESULT.pools.HOME = 1000000000000000000` (1 GEN). `settle_opens = 1791639000` is kickoff + 2 h.

### Testnet Bradbury

| Item | Value |
| --- | --- |
| Network | Testnet Bradbury (chain 4221) |
| Contract | [`0xF94F652d77249feE167d888F2a7AE14858FeD2eb`](https://explorer-bradbury.genlayer.com/address/0xF94F652d77249feE167d888F2a7AE14858FeD2eb) |
| Deploy tx | [`0x5a8c64979a6d65d4c46e100254c33c1c7c648bc7fc002f94fc527e680b656f4f`](https://explorer-bradbury.genlayer.com/tx/0x5a8c64979a6d65d4c46e100254c33c1c7c648bc7fc002f94fc527e680b656f4f) (`ACCEPTED`, `FINISHED_WITH_RETURN`) |
| Superseded contract | `0x9756e7cDF6A59cd0F57A1298e5cA23d265D0950c`: listings timed out on Bradbury (`VALIDATORS_TIMEOUT`, then `LEADER_TIMEOUT`), so page text was cut to 4,000 characters, the render wait to 3s, and `source_b = "none"` was added for single-source fixtures |
| Frontend | _fill in_ |

Two lessons from deploying to Bradbury, both now covered by tests:

- Source must stay under about 20 KB because of the 16.7M per-transaction gas ceiling. A 28 KB revision was rejected with `gas limit too high`.
- GenVM joins **all** leading `#` lines into the JSON runner header. Comment lines placed directly under the `Depends` line made the deploy fail with `invalid_contract: trailing characters at line 1 column 84` (failed deploy tx `0x1dd82ac978ff6034dc38a15002785ce7b429430b9ece215ece6d71ee13700ea6`). Line 2 of the contract is therefore blank.
