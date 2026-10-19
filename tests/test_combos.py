"""
Offline tests for contracts/combos.py (accumulators), wired to a real
contracts/ninety.py instance through the mock's cross-contract view calls.

Run:  python3 tests/test_combos.py
"""

import importlib.util
import json
import pathlib
import sys
import traceback

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_ninety as tn  # noqa: E402  (installs the mock SDK)
import mock_genlayer as mg  # noqa: E402

COMBOS = HERE.parent / "contracts" / "combos.py"
GEN = tn.GEN
CORE_ADDR = "0xd4807a5ebebc9fb8ad071364db452dd809a09129"
OWNER = mg.Address("0x" + "d" * 40)


def load_combos():
    spec = importlib.util.spec_from_file_location("combos", COMBOS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.datetime = mg._FrozenDatetime
    return mod


def world_with_games(n=3):
    """Ninety with n listed games, every RESULT/GOALS outcome seeded so odds exist."""
    mod, core = tn.fresh()
    mg.world.contracts[CORE_ADDR] = core
    games = []
    for i in range(n):
        fid, kickoff = tn.make_fixture(mod, core, ev=str(500001 + i))
        tn.as_(OWNER, 2 * GEN); core.stake(fid, "RESULT", "HOME")   # HOME 2, DRAW 1, AWAY 1 -> 2.00 / 4.00 / 4.00
        tn.as_(OWNER, GEN); core.stake(fid, "RESULT", "DRAW")
        tn.as_(OWNER, GEN); core.stake(fid, "RESULT", "AWAY")
        tn.as_(OWNER, GEN); core.stake(fid, "GOALS", "OVER")       # 2.00 / 2.00
        tn.as_(OWNER, GEN); core.stake(fid, "GOALS", "UNDER")
        games.append((fid, kickoff))
    cm = load_combos()
    tn.as_(OWNER)
    c = cm.NinetyCombos()
    tn.as_(OWNER, 10 * GEN); c.fund()
    return mod, core, cm, c, games


def settle(core, fid, kickoff, h, a, ev):
    mg.world.now = max(mg.world.now, kickoff + 7200)
    tn.set_feed(tn.final(kickoff, h, a), ev=ev)
    return core.settle(fid)


def house(c):
    return json.loads(c.get_house())


def test_runner_header_and_size():
    lines = COMBOS.read_text().splitlines()
    assert lines[0].startswith('# { "Depends": "py-genlayer:')
    json.loads(lines[0][1:])
    assert not lines[1].startswith("#")
    assert len(COMBOS.read_bytes()) < 12_000


def test_place_prices_legs_at_live_odds_and_reserves_payout():
    mod, core, cm, c, g = world_with_games(2)
    tn.as_(tn.BOB, GEN)
    tid = c.place(f"{g[0][0]}:RESULT:HOME,{g[1][0]}:GOALS:OVER")
    t = json.loads(c.get_ticket(tid))
    assert [l[3] for l in t["legs"]] == [200, 200] and t["payout"] == str(4 * GEN)
    h = house(c)
    assert h["free"] == str(7 * GEN) and h["locked"] == str(4 * GEN)
    assert json.loads(c.get_tickets_of(tn.BOB.as_hex))[0]["id"] == tid


def test_place_rules():
    mod, core, cm, c, g = world_with_games(3)
    a, b = g[0][0], g[1][0]
    tn.as_(tn.BOB, 0)
    tn.expect_error(lambda: c.place(f"{a}:RESULT:HOME,{b}:RESULT:HOME"), "send GEN")
    tn.as_(tn.BOB, GEN)
    tn.expect_error(lambda: c.place(f"{a}:RESULT:HOME"), "2 to 6 legs")
    tn.expect_error(lambda: c.place(f"{a}:RESULT:HOME,{a}:GOALS:OVER"), "one leg per match")
    tn.expect_error(lambda: c.place(f"{a}:CORNERS:OVER,{b}:RESULT:HOME"), "market must be")
    tn.expect_error(lambda: c.place(f"{a}:RESULT:WIN,{b}:RESULT:HOME"), "not an outcome")
    tn.expect_error(lambda: c.place(f"{a}:BTTS:YES,{b}:RESULT:HOME"), "no odds yet")
    tn.expect_error(lambda: c.place(f"x:RESULT:HOME,{b}:RESULT:HOME"), "look like")
    tn.as_(tn.BOB, 4 * GEN)      # 4 x 2.00 x 2.00 = 16 -> risk 12 > house 10
    tn.expect_error(lambda: c.place(f"{a}:RESULT:HOME,{b}:RESULT:HOME"), "cannot cover")
    mg.world.now = g[0][1]
    tn.as_(tn.BOB, GEN)
    tn.expect_error(lambda: c.place(f"{a}:RESULT:HOME,{b}:RESULT:HOME"), "closed")


def test_combo_wins_and_pays():
    mod, core, cm, c, g = world_with_games(2)
    tn.as_(tn.BOB, GEN)
    tid = c.place(f"{g[0][0]}:RESULT:HOME,{g[1][0]}:GOALS:OVER")
    tn.expect_error(lambda: c.settle_ticket(tid), "still being played")
    settle(core, g[0][0], g[0][1], 2, 0, "500001")
    tn.expect_error(lambda: c.settle_ticket(tid), "still being played")
    settle(core, g[1][0], g[1][1], 2, 2, "500002")
    mg.world.transfers.clear()
    assert c.settle_ticket(tid) == "WON"
    assert mg.world.transfers == [(tn.BOB.as_hex.lower(), 4 * GEN)]
    h = house(c)
    assert h["free"] == str(7 * GEN) and h["locked"] == "0"
    tn.expect_error(lambda: c.settle_ticket(tid), "already settled")


def test_one_losing_leg_settles_lost_early():
    mod, core, cm, c, g = world_with_games(2)
    tn.as_(tn.BOB, GEN)
    tid = c.place(f"{g[0][0]}:RESULT:HOME,{g[1][0]}:GOALS:OVER")
    settle(core, g[0][0], g[0][1], 0, 1, "500001")        # away win: leg lost
    mg.world.transfers.clear()
    assert c.settle_ticket(tid) == "LOST"                  # second match not played yet
    assert mg.world.transfers == []
    assert house(c)["free"] == str(11 * GEN)                # stake joins the bankroll


def test_void_leg_counts_as_odds_one():
    mod, core, cm, c, g = world_with_games(3)
    tn.as_(tn.BOB, GEN)
    tid = c.place(f"{g[0][0]}:RESULT:HOME,{g[1][0]}:RESULT:HOME,{g[2][0]}:GOALS:UNDER")   # 2 x 2 x 2 = 8
    settle(core, g[0][0], g[0][1], 1, 0, "500001")
    mg.world.now = g[1][1] + 7200
    tn.set_feed(tn.feed(g[1][1], name="STATUS_POSTPONED", state="post"), ev="500002")
    assert core.settle(g[1][0]) == "VOID"
    settle(core, g[2][0], g[2][1], 0, 0, "500003")
    mg.world.transfers.clear()
    assert c.settle_ticket(tid) == "WON"
    assert mg.world.transfers == [(tn.BOB.as_hex.lower(), 4 * GEN)]   # 2 x 1 x 2
    assert house(c)["free"] == str(7 * GEN)


def test_all_legs_void_refunds_stake():
    mod, core, cm, c, g = world_with_games(2)
    tn.as_(tn.BOB, GEN)
    tid = c.place(f"{g[0][0]}:RESULT:HOME,{g[1][0]}:RESULT:AWAY")
    for fid, ko in g:
        mg.world.now = ko + 7 * 86400
        core.expire(fid)
    mg.world.transfers.clear()
    assert c.settle_ticket(tid) == "REFUNDED"
    assert mg.world.transfers == [(tn.BOB.as_hex.lower(), GEN)]
    assert house(c)["free"] == str(10 * GEN) and house(c)["locked"] == "0"


def test_leg_odds_are_capped():
    mod, core, cm, c, g = world_with_games(2)
    tn.as_(tn.CAROL, 100 * GEN); core.stake(g[0][0], "RESULT", "HOME")   # DRAW now 104/1 -> capped at 15.00
    tn.as_(OWNER, 100 * GEN); c.fund()
    tn.as_(tn.BOB, GEN)
    tid = c.place(f"{g[0][0]}:RESULT:DRAW,{g[1][0]}:RESULT:HOME")
    t = json.loads(c.get_ticket(tid))
    assert t["legs"][0][3] == 1500 and t["payout"] == str(20 * GEN)   # 1 x 15 x 2 = 30 GEN, capped at 20


def test_owner_withdraw_only_free_bankroll():
    mod, core, cm, c, g = world_with_games(2)
    tn.as_(tn.BOB, GEN)
    c.place(f"{g[0][0]}:RESULT:HOME,{g[1][0]}:GOALS:OVER")       # locks 3 of the 10
    tn.as_(tn.BOB)
    tn.expect_error(lambda: c.withdraw(GEN), "only the owner")
    tn.as_(OWNER)
    tn.expect_error(lambda: c.withdraw(8 * GEN), "more than the free bankroll")
    mg.world.transfers.clear()
    c.withdraw(7 * GEN)
    assert mg.world.transfers == [(OWNER.as_hex.lower(), 7 * GEN)] and house(c)["free"] == "0"


def test_open_tickets_view():
    mod, core, cm, c, g = world_with_games(2)
    tn.as_(tn.BOB, GEN)
    t0 = c.place(f"{g[0][0]}:RESULT:HOME,{g[1][0]}:GOALS:OVER")
    tn.as_(tn.CAROL, GEN)
    t1 = c.place(f"{g[0][0]}:RESULT:AWAY,{g[1][0]}:GOALS:UNDER")
    settle(core, g[0][0], g[0][1], 0, 1, "500001")
    c.settle_ticket(t0)
    assert [t["id"] for t in json.loads(c.get_open_tickets(0, 10))] == [t1]


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception:
            failed += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
