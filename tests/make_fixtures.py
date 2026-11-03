"""Generate offline fixtures in each source's real format so the pipeline can be
tested without network access. Player IDs and expert rankings are real
(DynastyProcess files); trade values, stats and schedule are synthetic."""
import json, random, shutil, sys
from pathlib import Path
import polars as pl

DP = Path(sys.argv[1])           # path to a dynastyprocess/data checkout's files/
FX = Path(__file__).parent / "fixtures"
FX.mkdir(exist_ok=True)
random.seed(7)
for f in ("db_playerids.csv", "db_fpecr_latest.csv"):
    shutil.copy(DP / f, FX / f)

ids = pl.read_csv(DP / "db_playerids.csv", infer_schema_length=0, null_values=["NA"])
ecr = pl.read_csv(DP / "db_fpecr_latest.csv", infer_schema_length=0, null_values=["NA"])
fp = {r["fantasypros_id"]: r for r in ids.iter_rows(named=True) if r["fantasypros_id"]}

# universe from expert positional ranks
uni = []
for pos in ("QB", "RB", "WR", "TE"):
    for r in ecr.filter(pl.col("page_type") == f"redraft-{pos.lower()}").iter_rows(named=True):
        m = fp.get(r["id"])
        if m and m["sleeper_id"] and m["gsis_id"]:
            uni.append({"name": r["player"], "pos": pos, "team": r["tm"] or r["team"], "rank": float(r["ecr"]),
                        "sid": m["sleeper_id"], "gsis": m["gsis_id"]})

# FantasyCalc-format values: decay by positional rank with noise
base = {"QB": 5200, "RB": 9800, "WR": 9600, "TE": 7200}
for ppr in (1, 0.5, 0):
    for qbs in (1, 2):
        rows = []
        for u in uni:
            v = base[u["pos"]] * (0.92 ** (u["rank"] - 1)) * random.uniform(0.85, 1.15)
            if u["pos"] == "QB" and qbs == 2: v *= 1.9
            if ppr < 1 and u["pos"] in ("WR", "TE"): v *= 0.92 if ppr else 0.85
            rows.append({"player": {"id": int(u["sid"]) % 99991, "name": u["name"], "sleeperId": u["sid"], "position": u["pos"],
                                    "maybeTeam": u["team"], "maybeAge": round(random.uniform(22, 32), 1)},
                         "value": round(v), "overallRank": 0, "positionRank": 0, "trend30Day": 0})
        (FX / f"fantasycalc_{ppr}_{qbs}.json").write_text(json.dumps(rows))

# Sleeper players
inj = {"Lamar Jackson": ("IR", "Thumb"), "Jalen Hurts": ("IR", "Knee"), "Jeremiyah Love": ("Out", "Ankle"),
       "Kyler Murray": ("Questionable", "Hamstring"), "Cam Skattebo": ("Questionable", "Hamstring")}
sl = {}
for u in uni:
    st = inj.get(u["name"])
    sl[u["sid"]] = {"player_id": u["sid"], "full_name": u["name"], "position": u["pos"], "team": u["team"],
                    "injury_status": st[0] if st else None, "injury_body_part": st[1] if st else None, "age": 26}
(FX / "sleeper_players.json").write_text(json.dumps(sl))
(FX / "sleeper_state.json").write_text(json.dumps({"season": "2026", "league_season": "2026", "week": 9, "season_type": "regular"}))
top = [u["sid"] for u in uni[:400]]
(FX / "sleeper_trending_add.json").write_text(json.dumps([{"player_id": s, "count": random.randint(500, 90000)} for s in random.sample(top, 25)]))
(FX / "sleeper_trending_drop.json").write_text(json.dumps([{"player_id": s, "count": random.randint(500, 40000)} for s in random.sample(top, 25)]))

# Schedule: 32 teams, 18 weeks, one bye each (weeks 5-14), weeks 1-8 played
teams = sorted({"ARI","ATL","BAL","BUF","CAR","CHI","CIN","CLE","DAL","DEN","DET","GB","HOU","IND","JAX","KC","LV","LAC","LA","MIA","MIN","NE","NO","NYG","NYJ","PHI","PIT","SF","SEA","TB","TEN","WAS"})
byes = {t: 5 + i % 10 for i, t in enumerate(teams)}
games = []
for w in range(1, 19):
    pool = [t for t in teams if byes[t] != w]
    random.shuffle(pool)
    for a, b in zip(pool[::2], pool[1::2]):
        played = w <= 8
        games.append({"game_id": f"2026_{w:02d}_{a}_{b}", "season": 2026, "game_type": "REG", "week": w,
                      "home_team": a, "away_team": b,
                      "home_score": random.randint(10, 38) if played else None,
                      "away_score": random.randint(10, 38) if played else None})
pl.DataFrame(games).write_csv(FX / "schedules.csv")

# Weekly stats for played weeks (current season) + prior season counts
opp = {}
for g in games:
    opp[(g["week"], g["home_team"])] = g["away_team"]
    opp[(g["week"], g["away_team"])] = g["home_team"]
tfix = {"LAR": "LA"}
rows = []
ppg_base = {"QB": 19, "RB": 15, "WR": 14, "TE": 10}
for u in uni:
    t = tfix.get(u["team"], u["team"])
    lvl = ppg_base[u["pos"]] * (0.97 ** u["rank"]) + 3
    for season, weeks in ((2025, range(1, 18)), (2026, range(1, 9))):
        for w in weeks:
            if random.random() < 0.08: continue
            if season == 2026 and (w, t) not in opp: continue
            pts = max(0, random.gauss(lvl, lvl * 0.35))
            rec = 0 if u["pos"] == "QB" else max(0, round(random.gauss(pts / 4, 1)))
            rows.append({"player_id": u["gsis"], "player_display_name": u["name"], "position": u["pos"], "team": t,
                         "opponent_team": opp.get((w, t), "BUF"), "season": season, "week": w, "season_type": "REG",
                         "receptions": rec, "fantasy_points": round(pts - rec, 2), "fantasy_points_ppr": round(pts, 2)})
pl.DataFrame(rows).write_csv(FX / "player_stats.csv")

# One RSS feed with realistic stories
items = [
  ("Ravens place Lamar Jackson on injured reserve", "Lamar Jackson suffered a thumb injury on his throwing hand and was placed on injured reserve. The injury could be season-ending."),
  ("Raiders RB Mike Washington Jr. takes over lead role", "Mike Washington Jr. appears to have taken over as the lead back, with Ashton Jeanty relegated to a change-of-pace role."),
  ("Cardinals' Jeremiyah Love ruled out", "Jeremiyah Love reinjured his ankle and has been ruled out for Week 9."),
  ("Commanders rookie earns expanded role", "Unrelated story with no player names."),
]
xml = '<?xml version="1.0"?><rss version="2.0"><channel><title>Test</title>' + "".join(
    f"<item><title>{t}</title><description>{d}</description><link>https://example.com/{i}</link><pubDate>Mon, 02 Nov 2026 1{i}:00:00 GMT</pubDate></item>"
    for i, (t, d) in enumerate(items)) + "</channel></rss>"
url = "https://www.rotowire.com/rss/news.php?sport=NFL"
name = "rss_" + "".join(c if c.isalnum() else "_" for c in url)[-60:] + ".xml"
(FX / name).write_text(xml)
print(f"fixtures: {len(uni)} players, {len(games)} games, {len(rows)} stat rows")
