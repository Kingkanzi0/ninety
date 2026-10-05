"""
Offline lifecycle tests for contracts/ninety.py.

Run:  python3 tests/test_ninety.py

Uses tests/mock_genlayer.py (a minimal SDK stand-in), so these tests prove the
deterministic logic and the validator comparison rules. Live behaviour must
also be checked on Studio / Bradbury - see README.
"""

import importlib.util
import json
import pathlib
import sys
import traceback

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mock_genlayer as mg  # noqa: E402

mg.install()

CONTRACT = HERE.parent / "contracts" / "ninety.py"
GEN = 10**18
A = "https://www.bbc.com/sport/football/live/match-123"
B = "https://www.espn.com/soccer/match/_/gameId/456"
ALICE = mg.Address("0x" + "a" * 40)
BOB = mg.Address("0x" + "b" * 40)
CAROL = mg.Address("0x" + "c" * 40)


def load():
    spec = importlib.util.spec_from_file_location("ninety", CONTRACT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.datetime = mg._FrozenDatetime  # pin the transaction clock
    return mod


def fresh():
    mg.world = mg.World()
    # rebind module-level reference used by the mock functions
    globals_ = vars(mg)
    globals_["world"] = mg.world
    mod = load()
    c = mod.Ninety()
    c.__init__()
    return mod, c


def as_(addr, value=0):
    mg.world.sender = addr
    mg.world.value = value


def llm_listing(valid=True):
    return lambda prompt: {"valid": valid, "note": "page shows the fixture"}


def llm_extract(status="FINISHED", h=2, a=1, note="FT 2-1"):
    def fn(prompt):
        if "valid" in prompt and "fixture listings" in prompt:
            return {"valid": True, "note": "ok"}
        return {"status": status, "home_goals": h, "away_goals": a, "evidence": note}
    return fn


def make_fixture(mod, c, source_b=B):
    mg.world.set_web(A, "Arsenal v Chelsea, Premier League, Saturday")
    if source_b:
        mg.world.set_web(source_b, "Arsenal vs Chelsea match page")
    mg.world.set_llm(llm_listing(True))
    as_(ALICE)
    kickoff = mg.world.now + 3600
    fid = c.create_fixture("Arsenal", "Chelsea", "Premier League", kickoff, A, source_b)
    return fid, kickoff


def expect_error(fn, contains):
    try:
        fn()
    except (mg.UserError, mg.ConsensusFailure) as e:
        msg = getattr(e, "message", str(e))
        assert contains in msg, f"expected {contains!r} in {msg!r}"
        return
    raise AssertionError(f"expected error containing {contains!r}")


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

def test_no_forbidden_patterns_in_source():
    src = CONTRACT.read_text()
    assert "web.get" not in src, "must use gl.nondet.web.render"
    assert "strict_eq" not in src, "no strict_eq over LLM payloads"
    assert "@dataclass" not in src and "allow_storage" not in src, "no dataclass storage"
    assert "web.render" in src and "run_nondet_unsafe" in src


def test_listing_accepts_and_rejects():
    mod, c = fresh()
    fid, _ = make_fixture(mod, c)
    f = json.loads(c.get_fixture(fid))
    assert f["status"] == "SCHEDULED" and f["home"] == "Arsenal"
    assert set(f["markets"]) == {"RESULT", "GOALS", "BTTS", "SCORE"}

    mg.world.set_llm(llm_listing(False))
    fid2 = c.create_fixture("Arsenal", "Spurs", "", mg.world.now + 3600, A, "")
    assert json.loads(c.get_fixture(fid2))["status"] == "REJECTED"
    as_(BOB, GEN)
    expect_error(lambda: c.stake(fid2, "RESULT", "HOME"), "not open")


def test_listing_input_validation():
    mod, c = fresh()
    mg.world.set_web(A, "x")
    mg.world.set_llm(llm_listing(True))
    now = mg.world.now
    expect_error(lambda: c.create_fixture("Arsenal", "arsenal", "", now + 3600, A, ""), "teams must differ")
    expect_error(lambda: c.create_fixture("Arsenal", "Chelsea", "", now + 60, A, ""), "kickoff")
    expect_error(lambda: c.create_fixture("Arsenal", "Chelsea", "", now + 3600, "http://x.com", ""), "https")
    expect_error(lambda: c.create_fixture("Arsenal", "Chelsea", "", now + 3600, A, A), "must differ")


def test_listing_validator_ignores_note_but_not_decision():
    mod, c = fresh()
    mg.world.set_web(A, "page")
    mg.world.set_llm(lambda p: {"valid": True, "note": "leader wording"}, role="leader")
    mg.world.set_llm(lambda p: {"valid": True, "note": "completely different wording"}, role="validator")
    c.create_fixture("Arsenal", "Chelsea", "", mg.world.now + 3600, A, "")  # agrees

    mg.world.set_llm(lambda p: {"valid": False, "note": "x"}, role="validator")
    expect_error(lambda: c.create_fixture("Arsenal", "Chelsea", "", mg.world.now + 3600, A, ""), "validators agreed")


def test_staking_rules():
    mod, c = fresh()
    fid, kickoff = make_fixture(mod, c)
    as_(BOB, 0)
    expect_error(lambda: c.stake(fid, "RESULT", "HOME"), "send GEN")
    as_(BOB, GEN)
    expect_error(lambda: c.stake(fid, "RESULT", "WIN"), "must be one of")
    expect_error(lambda: c.stake(fid, "CORNERS", "OVER"), "market must be")
    expect_error(lambda: c.stake(fid, "SCORE", "two-one"), "look like")
    c.stake(fid, "SCORE", " 02 : 1 ")
    pos = json.loads(c.get_position(fid, BOB.as_hex))
    assert pos["stakes"]["SCORE"] == {"2-1": str(GEN)}
    mg.world.now = kickoff
    expect_error(lambda: c.stake(fid, "RESULT", "HOME"), "closed at kickoff")


def full_book(c, fid):
    # RESULT: Alice HOME 3, Bob AWAY 1, Carol DRAW 1
    as_(ALICE, 3 * GEN); c.stake(fid, "RESULT", "HOME")
    as_(BOB, 1 * GEN); c.stake(fid, "RESULT", "AWAY")
    as_(CAROL, 1 * GEN); c.stake(fid, "RESULT", "DRAW")
    # GOALS: Alice OVER 1, Bob UNDER 1
    as_(ALICE, GEN); c.stake(fid, "GOALS", "OVER")
    as_(BOB, GEN); c.stake(fid, "GOALS", "UNDER")
    # BTTS: only NO backed (Bob)
    as_(BOB, 2 * GEN); c.stake(fid, "BTTS", "NO")
    # SCORE: Carol 2-1 (1), Bob 1-0 (1)
    as_(CAROL, GEN); c.stake(fid, "SCORE", "2-1")
    as_(BOB, GEN); c.stake(fid, "SCORE", "1-0")


def test_full_lifecycle_final_and_payouts():
    mod, c = fresh()
    fid, kickoff = make_fixture(mod, c)
    full_book(c, fid)

    mg.world.now = kickoff + 3600
    expect_error(lambda: c.settle(fid), "too early")

    mg.world.now = kickoff + 2 * 3600
    mg.world.set_llm(llm_extract("FINISHED", 2, 1))
    assert c.settle(fid) == "FINAL 2-1"
    f = json.loads(c.get_fixture(fid))
    assert f["status"] == "FINAL" and (f["home_goals"], f["away_goals"]) == (2, 1)
    assert f["markets"]["RESULT"]["winner"] == "HOME"
    assert f["markets"]["GOALS"]["winner"] == "OVER"
    assert f["markets"]["BTTS"]["winner"] == "VOID"        # nobody backed YES -> refund
    assert f["markets"]["SCORE"]["winner"] == "2-1"
    assert "source A: FINISHED 2-1" in f["settle_note"] and "source B" in f["settle_note"]

    # Alice: RESULT 5 GEN (whole pot), GOALS 2 GEN (whole pot)
    as_(ALICE); assert c.claim(fid) == 7 * GEN
    # Bob: BTTS refund 2 GEN, everything else lost
    as_(BOB); assert c.claim(fid) == 2 * GEN
    # Carol: SCORE pot 2 GEN
    as_(CAROL); assert c.claim(fid) == 2 * GEN
    expect_error(lambda: c.claim(fid), "already claimed")

    paid = sum(v for _, v in mg.world.transfers)
    staked = 3 + 1 + 1 + 1 + 1 + 2 + 1 + 1
    assert paid == staked * GEN, (paid, staked)
    assert mg.world.transfers[0][0] == ALICE.as_hex.lower()


def test_proportional_split_rounds_down():
    mod, c = fresh()
    fid, kickoff = make_fixture(mod, c)
    as_(ALICE, 1); c.stake(fid, "RESULT", "HOME")
    as_(BOB, 2); c.stake(fid, "RESULT", "HOME")
    as_(CAROL, 4); c.stake(fid, "RESULT", "AWAY")
    mg.world.now = kickoff + 7200
    mg.world.set_llm(llm_extract("FINISHED", 1, 0))
    c.settle(fid)
    as_(ALICE); a = c.claim(fid)
    as_(BOB); b = c.claim(fid)
    assert (a, b) == (7 // 3, 14 // 3)
    assert a + b <= 7


def test_sources_disagree_conflict_then_void():
    mod, c = fresh()
    fid, kickoff = make_fixture(mod, c)
    as_(BOB, GEN); c.stake(fid, "RESULT", "AWAY")
    mg.world.now = kickoff + 7200

    # Source A says 2-1, source B says 2-2 (different pages -> model reads differently)
    def by_page(prompt):
        if "Arsenal v Chelsea, Premier League" in prompt:
            return {"status": "FINISHED", "home_goals": 2, "away_goals": 1, "evidence": "a"}
        return {"status": "FINISHED", "home_goals": 2, "away_goals": 2, "evidence": "b"}
    mg.world.set_llm(by_page)

    assert c.settle(fid) == "CONFLICT"
    assert c.settle(fid) == "CONFLICT"
    assert c.settle(fid) == "VOID"
    assert json.loads(c.get_fixture(fid))["status"] == "VOID"
    as_(BOB); assert c.claim(fid) == GEN


def test_pending_does_not_burn_attempts():
    mod, c = fresh()
    fid, kickoff = make_fixture(mod, c)
    mg.world.now = kickoff + 7200
    mg.world.set_llm(llm_extract("NOT_FINISHED", None, None))
    for _ in range(5):
        assert c.settle(fid) == "PENDING"
    f = json.loads(c.get_fixture(fid))
    assert f["status"] == "SCHEDULED" and f["conflicts"] == 0
    mg.world.set_llm(llm_extract("FINISHED", 0, 0))
    assert c.settle(fid) == "FINAL 0-0"


def test_postponed_voids_and_refunds():
    mod, c = fresh()
    fid, kickoff = make_fixture(mod, c)
    as_(ALICE, 3 * GEN); c.stake(fid, "SCORE", "1-1")
    as_(ALICE, GEN); c.stake(fid, "BTTS", "YES")
    mg.world.now = kickoff + 7200
    mg.world.set_llm(llm_extract("POSTPONED", None, None))
    assert c.settle(fid) == "VOID"
    as_(ALICE); assert c.claim(fid) == 4 * GEN


def test_unreachable_source_is_pending_not_final():
    mod, c = fresh()
    fid, kickoff = make_fixture(mod, c)
    mg.world.now = kickoff + 7200
    mg.world.set_web(B, RuntimeError("timeout"))
    mg.world.set_llm(llm_extract("FINISHED", 3, 0))
    assert c.settle(fid) == "PENDING"


def test_single_source_fixture_settles():
    mod, c = fresh()
    fid, kickoff = make_fixture(mod, c, source_b="")
    as_(BOB, GEN); c.stake(fid, "GOALS", "UNDER")
    mg.world.now = kickoff + 7200
    mg.world.set_llm(llm_extract("FINISHED", 1, 1))
    assert c.settle(fid) == "FINAL 1-1"
    as_(BOB); assert c.claim(fid) == GEN


def test_settlement_validator_compares_score_not_evidence():
    mod, c = fresh()
    fid, kickoff = make_fixture(mod, c)
    mg.world.now = kickoff + 7200
    # Same score, different evidence text -> agree
    mg.world.set_llm(llm_extract("FINISHED", 2, 0, note="leader saw FT"), role="leader")
    mg.world.set_llm(llm_extract("FINISHED", 2, 0, note="totally different words"), role="validator")
    assert c.settle(fid) == "FINAL 2-0"

    mod, c = fresh()
    fid, kickoff = make_fixture(mod, c)
    mg.world.now = kickoff + 7200
    # Leader lies about the score -> validators reject
    mg.world.set_llm(llm_extract("FINISHED", 5, 0), role="leader")
    mg.world.set_llm(llm_extract("FINISHED", 2, 0), role="validator")
    expect_error(lambda: c.settle(fid), "validators agreed")
    assert json.loads(c.get_fixture(fid))["status"] == "SCHEDULED"


def test_malformed_llm_output_forces_rotation():
    mod, c = fresh()
    fid, kickoff = make_fixture(mod, c)
    mg.world.now = kickoff + 7200
    mg.world.set_llm(lambda p: {"status": "MAYBE"}, role="leader")
    mg.world.set_llm(llm_extract("FINISHED", 1, 0), role="validator")
    expect_error(lambda: c.settle(fid), "validators agreed")


def test_expire_safety_valve():
    mod, c = fresh()
    fid, kickoff = make_fixture(mod, c)
    as_(CAROL, GEN); c.stake(fid, "RESULT", "DRAW")
    mg.world.now = kickoff + 6 * 24 * 3600
    expect_error(lambda: c.expire(fid), "7 days")
    mg.world.now = kickoff + 7 * 24 * 3600
    c.expire(fid)
    as_(CAROL); assert c.claim(fid) == GEN
    expect_error(lambda: c.settle(fid), "not awaiting")


def test_merge_sources_table():
    mod, _ = fresh()
    F = lambda h, a: {"status": "FINISHED", "home": h, "away": a}
    S = lambda s: {"status": s, "home": None, "away": None}
    m = mod.merge_sources
    assert m([F(1, 0), F(1, 0)])["verdict"] == "FINAL"
    assert m([F(1, 0), F(0, 1)])["verdict"] == "CONFLICT"
    assert m([F(1, 0), S("NOT_FINISHED")])["verdict"] == "PENDING"
    assert m([F(1, 0), S("UNREACHABLE")])["verdict"] == "PENDING"
    assert m([S("POSTPONED"), S("ABANDONED")])["verdict"] == "VOID"
    assert m([F(1, 0), S("POSTPONED")])["verdict"] == "CONFLICT"
    assert m([S("UNCLEAR"), F(1, 1)])["verdict"] == "CONFLICT"


def test_outcomes_for_score_table():
    mod, _ = fresh()
    o = mod.outcomes_for_score
    assert o(2, 1) == {"RESULT": "HOME", "GOALS": "OVER", "BTTS": "YES", "SCORE": "2-1"}
    assert o(0, 0) == {"RESULT": "DRAW", "GOALS": "UNDER", "BTTS": "NO", "SCORE": "0-0"}
    assert o(0, 2)["RESULT"] == "AWAY" and o(0, 2)["GOALS"] == "UNDER"
    assert o(1, 2)["GOALS"] == "OVER"


def test_views_paginate_newest_first():
    mod, c = fresh()
    for _ in range(3):
        make_fixture(mod, c)
    page = json.loads(c.get_fixtures(0, 2))
    assert page["total"] == 3 and [f["id"] for f in page["fixtures"]] == [2, 1]
    page = json.loads(c.get_fixtures(2, 10))
    assert [f["id"] for f in page["fixtures"]] == [0]


def test_web_render_is_used_in_text_mode():
    mod, c = fresh()
    fid, kickoff = make_fixture(mod, c)
    mg.world.now = kickoff + 7200
    mg.world.set_llm(llm_extract("FINISHED", 1, 0))
    c.settle(fid)
    renders = [x for x in mg.world.nondet_calls if x[0] == "render"]
    assert renders and all(x[3] == "text" for x in renders)
    prompts = [x for x in mg.world.nondet_calls if x[0] == "prompt"]
    assert all(x[3] == "json" for x in prompts)


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
