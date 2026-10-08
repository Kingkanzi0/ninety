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
URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/{code}/scoreboard?dates={start}-{end}"


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


def main(path):
    now = int(time.time())
    start = datetime.now(timezone.utc)
    end = start + timedelta(days=DAYS_AHEAD)
    matches = []
    for code, name in LEAGUES:
        url = URL.format(code=code, start=start.strftime("%Y%m%d"), end=end.strftime("%Y%m%d"))
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ninety-fixtures/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                matches += parse(json.load(r), name, now)
            print(f"{name}: ok")
        except Exception as e:  # one league failing must not stop the rest
            print(f"{name}: skipped ({e})")
    matches.sort(key=lambda m: m["kickoff"])
    with open(path, "w") as f:
        json.dump({"updated": now, "matches": matches}, f, indent=1)
    print(f"wrote {len(matches)} matches to {path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "frontend/fixtures.json")
