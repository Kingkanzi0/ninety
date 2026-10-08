"""Fetch upcoming fixtures for Ninety's "Suggested matches".

Runs on GitHub Actions (server side, so no browser CORS limits) and writes
frontend/fixtures.json, which the app reads. Source: ESPN's public scoreboard feed.
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


def parse(feed, comp, now):
    out = []
    for ev in feed.get("events", []):
        comps = ev.get("competitions") or []
        if not comps:
            continue
        teams = comps[0].get("competitors") or []
        home = next((t for t in teams if t.get("homeAway") == "home"), None)
        away = next((t for t in teams if t.get("homeAway") == "away"), None)
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
                    "away": away["team"]["displayName"], "comp": comp, "kickoff": ko})
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
        return parse(get(f"{BASE.format(code=code)}?dates={days[0]}-{days[-1]}"), name, now), "range"
    except Exception as first:
        found, ok = {}, 0
        for d in days:
            try:
                for m in parse(get(f"{BASE.format(code=code)}?dates={d}"), name, now):
                    found[m["id"]] = m
                ok += 1
            except Exception:
                pass
        if not ok:
            try:  # last resort: the default "this week" view
                for m in parse(get(BASE.format(code=code)), name, now):
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
    print(f"wrote {len(matches)} matches to {path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "frontend/fixtures.json")
