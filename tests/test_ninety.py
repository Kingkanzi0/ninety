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
PAGE = "https://www.flashscore.com/match/football/abc123/"
LG, EV = "eng.1", "704512"
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


def feed_url(lg=LG, ev=EV, host="site.api.espn.com"):
    return f"https://{host}/apis/site/v2/sports/soccer/{lg}/summary?event={ev}"


HOST2 = "site.web.api.espn.com"


def feed(kickoff, name="STATUS_SCHEDULED", state="pre", done=False, h="0", a="0",
         home="Arsenal", away="Chelsea"):
    from datetime import datetime, timezone
    date = datetime.fromtimestamp(kickoff, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    doc = {"header": {"id": EV, "competitions": [{
        "date": date,
        "status": {"type": {"name": name, "state": state, "completed": done}},
        "competitors": [
            {"homeAway": "home", "score": h, "team": {"displayName": home}},
            {"homeAway": "away", "score": a, "team": {"displayName": away}},
        ]}]}, "news": {"articles": []}}
    return json.dumps(doc)


def set_feed(text, role=None, lg=LG, ev=EV):
    mg.world.set_web(feed_url(lg, ev), text, role)


def final(kickoff, h, a, name="STATUS_FULL_TIME"):
    return feed(kickoff, name=name, state="post", done=True, h=str(h), a=str(a))


def llm_extract(status="FINISHED", h=2, a=1, note="FT 2-1"):
    return lambda prompt: {"status": status, "home_goals": h, "away_goals": a, "evidence": note}


def make_fixture(mod, c, ev=EV, lead=3600):
    kickoff = mg.world.now + lead
    set_feed(feed(kickoff), ev=ev)
    as_(ALICE)
    return c.list_match(LG, int(ev)), kickoff


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

def test_runner_header_is_alone():
    # GenVM joins ALL leading '#' lines into the JSON runner header.
    lines = CONTRACT.read_text().splitlines()
    assert lines[0].startswith('# { "Depends": "py-genlayer:')
    json.loads(lines[0][1:])
    assert not lines[1].startswith("#")


def test_source_fits_bradbury_gas_ceiling():
    assert len(CONTRACT.read_bytes()) < 19_800, "Bradbury rejects source above ~20 KB"


def test_no_forbidden_patterns_in_source():
    src = CONTRACT.read_text()
    assert "web.get(FEED" in src, "JSON feed is read with an HTTP GET"
    assert "_page(url)" in src and "def _page" in src, "match pages are rendered"
    assert "strict_eq" not in src, "no strict_eq over LLM payloads"
    assert "@dataclass" not in src and "allow_storage" not in src, "no dataclass storage"
    assert "web.render" in src and "run_nondet_unsafe" in src and "exec_prompt" in src


def test_list_match_takes_teams_and_kickoff_from_feed():
    mod, c = fresh()
    fid, kickoff = make_fixture(mod, c)
    f = json.loads(c.get_fixture(fid))
    assert (f["home"], f["away"], f["competition"]) == ("Arsenal", "Chelsea", "Premier League")
    assert f["kickoff"] == kickoff // 60 * 60 and f["status"] == "SCHEDULED"
    assert f["feed"] == f"{LG}/{EV}" and f["source_a"] == ""
    assert set(f["markets"]) == {"RESULT", "GOALS", "BTTS", "SCORE"}
    assert json.loads(c.get_listed(f"{LG}/{EV},{LG}/999"))  == {f"{LG}/{EV}": 0, f"{LG}/999": -1}
    gets = [x for x in mg.world.nondet_calls if x[0] == "get"]
    assert gets and all("Mozilla" in x[3].get("User-Agent", "") for x in gets), "browser User-Agent sent"
    assert not [x for x in mg.world.nondet_calls if x[0] in ("render", "prompt")], "no browser, no LLM"


def test_list_match_input_rules():
    mod, c = fresh()
    make_fixture(mod, c)
    expect_error(lambda: c.list_match(LG, int(EV)), "already listed")
    expect_error(lambda: c.list_match("xyz.9", 123456), "unsupported league")
    expect_error(lambda: c.list_match(LG, -5), "digits")
    # started or too soon / too far
    set_feed(feed(mg.world.now - 600, name="STATUS_FIRST_HALF", state="in"), ev="111111")
    expect_error(lambda: c.list_match(LG, 111111), "already started")
    set_feed(feed(mg.world.now + 120), ev="222222")
    expect_error(lambda: c.list_match(LG, 222222), "10 minutes")
    set_feed(feed(mg.world.now + 90 * 86400), ev="333333")
    expect_error(lambda: c.list_match(LG, 333333), "60 days")


def test_list_match_feed_down_creates_nothing():
    mod, c = fresh()
    mg.world.set_web(feed_url(), 403)
    mg.world.set_web(feed_url(host=HOST2), RuntimeError("{'causes': ['X'], 'ctx': {'status': 503}}"))
    expect_error(lambda: c.list_match(LG, int(EV)), "match feed unavailable (HTTP 503)")
    mg.world.set_web(feed_url(host=HOST2), 403)
    expect_error(lambda: c.list_match(LG, int(EV)), "match feed unavailable (HTTP 403)")
    mg.world.set_web(feed_url(host=HOST2), "<html>not json</html>")
    expect_error(lambda: c.list_match(LG, int(EV)), "match feed unreadable")
    assert c.get_fixture_count() == 0


def test_second_feed_host_is_used_when_first_blocks():
    mod, c = fresh()
    k = mg.world.now + 3600
    mg.world.set_web(feed_url(), 403)
    mg.world.set_web(feed_url(host=HOST2), feed(k))
    fid = c.list_match(LG, int(EV))
    assert json.loads(c.get_fixture(fid))["home"] == "Arsenal"


def test_list_match_lying_leader_rejected():
    mod, c = fresh()
    k = mg.world.now + 3600
    set_feed(feed(k, home="Arsenal"), role="leader")
    set_feed(feed(k, home="Leeds United"), role="validator")
    expect_error(lambda: c.list_match(LG, int(EV)), "validators agreed")
    assert c.get_fixture_count() == 0


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
    as_(ALICE, 3 * GEN); c.stake(fid, "RESULT", "HOME")
    as_(BOB, 1 * GEN); c.stake(fid, "RESULT", "AWAY")
    as_(CAROL, 1 * GEN); c.stake(fid, "RESULT", "DRAW")
    as_(ALICE, GEN); c.stake(fid, "GOALS", "OVER")
    as_(BOB, GEN); c.stake(fid, "GOALS", "UNDER")
    as_(BOB, 2 * GEN); c.stake(fid, "BTTS", "NO")
    as_(CAROL, GEN); c.stake(fid, "SCORE", "2-1")
    as_(BOB, GEN); c.stake(fid, "SCORE", "1-0")


def test_full_lifecycle_feed_final_and_payouts():
    mod, c = fresh()
    fid, kickoff = make_fixture(mod, c)
    full_book(c, fid)
    mg.world.now = kickoff + 3600
    expect_error(lambda: c.settle(fid), "too early")
    mg.world.now = kickoff + 2 * 3600
    set_feed(final(kickoff, 2, 1))
    mg.world.nondet_calls.clear()
    assert c.settle(fid) == "FINAL 2-1"
    assert not [x for x in mg.world.nondet_calls if x[0] == "prompt"], "fast path uses no LLM"
    f = json.loads(c.get_fixture(fid))
    assert f["status"] == "FINAL" and (f["home_goals"], f["away_goals"]) == (2, 1)
    assert f["markets"]["RESULT"]["winner"] == "HOME"
    assert f["markets"]["GOALS"]["winner"] == "OVER"
    assert f["markets"]["BTTS"]["winner"] == "VOID"
    assert f["markets"]["SCORE"]["winner"] == "2-1"
    assert "STATUS_FULL_TIME 2-1" in f["settle_note"]
    as_(ALICE); assert c.claim(fid) == 7 * GEN
    as_(BOB); assert c.claim(fid) == 2 * GEN
    as_(CAROL); assert c.claim(fid) == 2 * GEN
    expect_error(lambda: c.claim(fid), "already claimed")
    assert sum(v for _, v in mg.world.transfers) == 11 * GEN


def test_proportional_split_rounds_down():
    mod, c = fresh()
    fid, kickoff = make_fixture(mod, c)
    as_(ALICE, 1); c.stake(fid, "RESULT", "HOME")
    as_(BOB, 2); c.stake(fid, "RESULT", "HOME")
    as_(CAROL, 4); c.stake(fid, "RESULT", "AWAY")
    mg.world.now = kickoff + 7200
    set_feed(final(kickoff, 1, 0))
    c.settle(fid)
    as_(ALICE); a = c.claim(fid)
    as_(BOB); b = c.claim(fid)
    assert (a, b) == (7 // 3, 14 // 3)


def test_feed_in_play_or_down_is_pending_and_free():
    mod, c = fresh()
    fid, kickoff = make_fixture(mod, c)
    mg.world.now = kickoff + 7200
    set_feed(feed(kickoff, name="STATUS_SECOND_HALF", state="in", h="1", a="0"))
    assert c.settle(fid) == "PENDING"
    set_feed(RuntimeError("timeout"))
    mg.world.set_web(feed_url(host=HOST2), 403)
    assert c.settle(fid) == "PENDING"
    f = json.loads(c.get_fixture(fid))
    assert f["status"] == "SCHEDULED" and f["conflicts"] == 0 and "unavailable" in f["settle_note"]
    set_feed(final(kickoff, 0, 0))
    assert c.settle(fid) == "FINAL 0-0"


def test_extra_time_result_is_not_used():
    # the feed's score after extra time is not the 90-minute score
    mod, c = fresh()
    fid, kickoff = make_fixture(mod, c)
    mg.world.now = kickoff + 7200
    for n in ("STATUS_FINAL_AET", "STATUS_FINAL_PEN"):
        set_feed(final(kickoff, 2, 1, name=n))
        assert c.settle(fid) == "PENDING"


def test_postponed_voids_and_refunds():
    mod, c = fresh()
    fid, kickoff = make_fixture(mod, c)
    as_(ALICE, 3 * GEN); c.stake(fid, "SCORE", "1-1")
    as_(ALICE, GEN); c.stake(fid, "BTTS", "YES")
    mg.world.now = kickoff + 7200
    set_feed(feed(kickoff, name="STATUS_POSTPONED", state="post"))
    assert c.settle(fid) == "VOID"
    as_(ALICE); assert c.claim(fid) == 4 * GEN


def test_feed_lying_leader_rejected():
    mod, c = fresh()
    fid, kickoff = make_fixture(mod, c)
    mg.world.now = kickoff + 7200
    set_feed(final(kickoff, 5, 0), role="leader")
    set_feed(final(kickoff, 2, 0), role="validator")
    expect_error(lambda: c.settle(fid), "validators agreed")
    assert json.loads(c.get_fixture(fid))["status"] == "SCHEDULED"


def test_ai_backup_opens_after_six_hours():
    mod, c = fresh()
    fid, kickoff = make_fixture(mod, c)
    as_(BOB, GEN); c.stake(fid, "GOALS", "UNDER")
    mg.world.now = kickoff + 5 * 3600
    expect_error(lambda: c.settle_with_page(fid, PAGE), "6 hours")
    mg.world.now = kickoff + 6 * 3600
    expect_error(lambda: c.settle_with_page(fid, "http://x.com"), "https")
    mg.world.set_web(PAGE, "Arsenal 1-1 Chelsea FT")
    mg.world.set_llm(llm_extract("FINISHED", 1, 1))
    assert c.settle_with_page(fid, PAGE) == "FINAL 1-1"
    f = json.loads(c.get_fixture(fid))
    assert f["source_a"] == PAGE and f["settle_note"].startswith("page: FINISHED 1-1")
    as_(BOB); assert c.claim(fid) == GEN
    renders = [x for x in mg.world.nondet_calls if x[0] == "render" and x[2] == PAGE]
    assert renders and all(x[3] == "text" and x[4] == "3s" for x in renders)
    prompts = [x for x in mg.world.nondet_calls if x[0] == "prompt"]
    assert prompts and all(x[3] == "json" for x in prompts)


def test_ai_backup_compares_score_not_evidence():
    mod, c = fresh()
    fid, kickoff = make_fixture(mod, c)
    mg.world.now = kickoff + 6 * 3600
    mg.world.set_web(PAGE, "page")
    mg.world.set_llm(llm_extract("FINISHED", 2, 0, note="leader words"), role="leader")
    mg.world.set_llm(llm_extract("FINISHED", 2, 0, note="other words"), role="validator")
    assert c.settle_with_page(fid, PAGE) == "FINAL 2-0"

    mod, c = fresh()
    fid, kickoff = make_fixture(mod, c)
    mg.world.now = kickoff + 6 * 3600
    mg.world.set_web(PAGE, "page")
    mg.world.set_llm(llm_extract("FINISHED", 5, 0), role="leader")
    mg.world.set_llm(llm_extract("FINISHED", 2, 0), role="validator")
    expect_error(lambda: c.settle_with_page(fid, PAGE), "validators agreed")


def test_ai_backup_unclear_page_counts_conflicts_then_voids():
    mod, c = fresh()
    fid, kickoff = make_fixture(mod, c)
    as_(BOB, GEN); c.stake(fid, "RESULT", "AWAY")
    mg.world.now = kickoff + 6 * 3600
    mg.world.set_web(PAGE, "page")
    mg.world.set_llm(llm_extract("UNCLEAR", None, None))
    assert c.settle_with_page(fid, PAGE) == "CONFLICT"
    assert c.settle_with_page(fid, PAGE) == "CONFLICT"
    assert c.settle_with_page(fid, PAGE) == "VOID"
    as_(BOB); assert c.claim(fid) == GEN


def test_ai_backup_malformed_llm_forces_rotation():
    mod, c = fresh()
    fid, kickoff = make_fixture(mod, c)
    mg.world.now = kickoff + 6 * 3600
    mg.world.set_web(PAGE, "page")
    mg.world.set_llm(lambda p: {"status": "MAYBE"}, role="leader")
    mg.world.set_llm(llm_extract("FINISHED", 1, 0), role="validator")
    expect_error(lambda: c.settle_with_page(fid, PAGE), "validators agreed")


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


def test_feed_verdict_table():
    mod, _ = fresh()
    v = lambda **k: mod.feed_verdict({"name": "", "state": "", "done": False, "hg": None, "ag": None, **k})["verdict"]
    assert v(name="STATUS_FULL_TIME", done=True, hg=1, ag=0) == "FINAL"
    assert v(name="STATUS_FINAL", done=True, hg=0, ag=0) == "FINAL"
    assert v(name="STATUS_FINAL_AET", done=True, hg=1, ag=1) == "PENDING"
    assert v(name="STATUS_FULL_TIME", done=True, hg=None, ag=0) == "PENDING"
    assert v(name="STATUS_CANCELED") == "VOID" and v(name="STATUS_ABANDONED") == "VOID"
    assert v(name="STATUS_HALFTIME", state="in") == "PENDING"


def test_merge_sources_table():
    mod, _ = fresh()
    F = lambda h, a: {"status": "FINISHED", "home": h, "away": a}
    S = lambda s: {"status": s, "home": None, "away": None}
    m = mod.merge_sources
    assert m([F(1, 0)])["verdict"] == "FINAL"
    assert m([S("NOT_FINISHED")])["verdict"] == "PENDING"
    assert m([S("UNREACHABLE")])["verdict"] == "PENDING"
    assert m([S("POSTPONED")])["verdict"] == "VOID"
    assert m([S("UNCLEAR")])["verdict"] == "CONFLICT"


def test_outcomes_for_score_table():
    mod, _ = fresh()
    o = mod.outcomes_for_score
    assert o(2, 1) == {"RESULT": "HOME", "GOALS": "OVER", "BTTS": "YES", "SCORE": "2-1"}
    assert o(0, 0) == {"RESULT": "DRAW", "GOALS": "UNDER", "BTTS": "NO", "SCORE": "0-0"}
    assert o(0, 2)["RESULT"] == "AWAY" and o(1, 2)["GOALS"] == "OVER"


def test_views_paginate_newest_first():
    mod, c = fresh()
    for ev in ("100001", "100002", "100003"):
        make_fixture(mod, c, ev=ev)
    page = json.loads(c.get_fixtures(0, 2))
    assert page["total"] == 3 and [f["id"] for f in page["fixtures"]] == [2, 1]
    assert [f["id"] for f in json.loads(c.get_fixtures(2, 10))["fixtures"]] == [0]


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
