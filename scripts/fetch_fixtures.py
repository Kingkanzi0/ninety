"""Fetch upcoming fixtures for Ninety.

Runs on GitHub Actions (server side, so no browser CORS limits) and writes
frontend/fixtures.json (matches + starting probabilities used by the keeper to
seed odds) and frontend/logos.json (team name -> badge URL, merged over time).
Source: ESPN's public scoreboard feed.
"""
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

LEAGUES = [
    ("eng.1", "Premier League"), ("esp.1", "LaLiga"), ("ita.1", "Serie A"),
    ("ger.1", "Bundesliga"), ("fra.1", "Ligue 1"), ("uefa.champions", "Champions League"),
    ("uefa.europa", "Europa League"), ("usa.1", "MLS"), ("ned.1", "Eredivisie"),
    ("por.1", "Primeira Liga"), ("eng.2", "Championship"),
]
DAYS_AHEAD = 10
LOGOS = {}
# Used when the feed has no bookmaker odds for a match.
DEFAULT_PROBS = {"RESULT": {"HOME": 0.45, "DRAW": 0.27, "AWAY": 0.28},
                 "GOALS": {"OVER": 0.52, "UNDER": 0.48}, "BTTS": {"YES": 0.52, "NO": 0.48}}


def implied(price):
    """American odds (-150, +240, 'EVEN') -> probability, or None."""
    try:
        s = str(price).strip().upper()
        v = 100.0 if s == "EVEN" else float(s)
    except (TypeError, ValueError):
        return None
    if v == 0:
        return None
    return 100 / (v + 100) if v > 0 else -v / (-v + 100)


def _pick(o, *paths):
    for path in paths:
        cur = o
        for k in path:
            cur = cur.get(k) if isinstance(cur, dict) else None
        p = implied(cur)
        if p:
            return p
    return None


def _norm(d):
    t = sum(d.values())
    return {k: round(min(max(v / t, 0.05), 0.9), 3) for k, v in d.items()}


def probs_from(comp):
    """Starting probabilities from the feed's bookmaker odds, else defaults."""
    out = {k: dict(v) for k, v in DEFAULT_PROBS.items()}
    src = "default"
    odds = (comp.get("odds") or [None])[0]
    if isinstance(odds, dict):
        h = _pick(odds, ("homeTeamOdds", "moneyLine"), ("moneyline", "home", "close", "odds"), ("moneyline", "home", "open", "odds"))
        a = _pick(odds, ("awayTeamOdds", "moneyLine"), ("moneyline", "away", "close", "odds"), ("moneyline", "away", "open", "odds"))
        d = _pick(odds, ("drawOdds", "moneyLine"), ("moneyline", "draw", "close", "odds"), ("moneyline", "draw", "open", "odds"))
        if h and a and d:
            out["RESULT"], src = _norm({"HOME": h, "DRAW": d, "AWAY": a}), "feed"
        try:
            line = float(odds.get("overUnder"))
        except (TypeError, ValueError):
            line = None
        if line == 2.5:
            ov = _pick(odds, ("overOdds",), ("total", "over", "close", "odds"), ("total", "over", "open", "odds"))
            un = _pick(odds, ("underOdds",), ("total", "under", "close", "odds"), ("total", "under", "open", "odds"))
            if ov and un:
                out["GOALS"] = _norm({"OVER": ov, "UNDER": un})
    out["source"] = src
    return out


def league_logo(feed, name):
    """Remember the competition badge the feed ships with (leagues[0].logos[0].href)."""
    try:
        href = feed["leagues"][0]["logos"][0]["href"]
        if str(href).startswith("https://"):
            LOGOS[f"league:{name}"] = href
    except (KeyError, IndexError, TypeError):
        pass


def parse(feed, comp, now, code=""):
    out = []
    for ev in feed.get("events", []):
        comps = ev.get("competitions") or []
        if not comps:
            continue
        teams = comps[0].get("competitors") or []
        home = next((t for t in teams if t.get("homeAway") == "home"), None)
        away = next((t for t in teams if t.get("homeAway") == "away"), None)
        for t in (home, away):
            team = (t or {}).get("team") or {}
            if team.get("displayName") and str(team.get("logo", "")).startswith("https://"):
                LOGOS[team["displayName"]] = team["logo"]
        state = ((ev.get("status") or {}).get("type") or {}).get("state")
        if not home or not away or state != "pre":
            continue
        try:
            ko = int(datetime.strptime(ev["date"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc).timestamp())
        except (KeyError, ValueError):
            continue
        if ko < now + 1800:
            continue
        out.append({"id": str(ev.get("id")), "home": home["team"]["displayName"],
                    "away": away["team"]["displayName"], "comp": comp, "league": code, "kickoff": ko,
                    "probs": probs_from(comps[0])})
    return out


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-GB,en;q=0.9",
}
BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer/{code}/scoreboard"


def get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def fetch_league(code, name, now):
    """Try a date range first; if ESPN refuses it, ask one day at a time."""
    start = datetime.now(timezone.utc)
    days = [(start + timedelta(days=i)).strftime("%Y%m%d") for i in range(DAYS_AHEAD + 1)]
    try:
        feed = get(f"{BASE.format(code=code)}?dates={days[0]}-{days[-1]}")
        league_logo(feed, name)
        return parse(feed, name, now, code), "range"
    except Exception as first:
        found, ok = {}, 0
        for d in days:
            try:
                feed = get(f"{BASE.format(code=code)}?dates={d}")
                league_logo(feed, name)
                for m in parse(feed, name, now, code):
                    found[m["id"]] = m
                ok += 1
            except Exception:
                pass
        if not ok:
            try:  # last resort: the default "this week" view
                for m in parse(get(BASE.format(code=code)), name, now, code):
                    found[m["id"]] = m
                ok = 1
            except Exception:
                raise first
        return list(found.values()), "per-day"


def main(path):
    now = int(time.time())
    matches, seen = [], set()
    for code, name in LEAGUES:
        try:
            got, how = fetch_league(code, name, now)
            for m in got:
                if m["id"] not in seen:
                    seen.add(m["id"]); matches.append(m)
            print(f"{name}: {len(got)} matches ({how})")
        except Exception as e:  # one league failing must not stop the rest
            print(f"{name}: skipped ({e})")
    matches.sort(key=lambda m: m["kickoff"])
    with open(path, "w") as f:
        json.dump({"updated": now, "matches": matches}, f, indent=1)
    print(f"wrote {len(matches)} matches to {path} ({sum(m['probs']['source'] == 'feed' for m in matches)} with bookmaker odds)")
    logo_path = path.replace("fixtures.json", "logos.json")
    try:
        with open(logo_path) as f:
            known = json.load(f)
    except (OSError, ValueError):
        known = {}
    known.update(LOGOS)
    with open(logo_path, "w") as f:
        json.dump(dict(sorted(known.items())), f, indent=0)
    print(f"wrote {len(known)} team logos to {logo_path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "frontend/fixtures.json")
