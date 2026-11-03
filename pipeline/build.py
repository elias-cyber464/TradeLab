"""Build site/data.json from free sources.

    python -m pipeline.build

Each source fails independently: if one is down, the site still updates with
the rest and the status panel shows what happened.
"""
from __future__ import annotations

import json
import math
import traceback
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import polars as pl

from . import news, sources

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "site" / "data.json"
NEWS_FILE = ROOT / "data" / "news_history.json"
FEEDS_FILE = ROOT / "data" / "feeds.json"

POS = ["QB", "RB", "WR", "TE"]
FORMATS = {"ppr_1": (1, 1), "half_1": (0.5, 1), "std_1": (0, 1),
           "ppr_2": (1, 2), "half_2": (0.5, 2), "std_2": (0, 2)}
TEAM_FIX = {"LA": "LAR", "RAM": "LAR", "STL": "LAR", "WSH": "WAS", "JAC": "JAX", "OAK": "LV", "LVR": "LV",
            "SD": "LAC", "SDC": "LAC", "GBP": "GB", "KCC": "KC", "NEP": "NE", "NOS": "NO", "SFO": "SF",
            "TBB": "TB", "ARZ": "ARI", "BLT": "BAL", "CLV": "CLE", "HST": "HOU"}
FANTASY_LAST_WEEK = 17
PLAYOFF_WEEKS = (15, 16, 17)


def team(t):
    if t is None:
        return "FA"
    t = str(t).upper().strip()
    return TEAM_FIX.get(t, t)


def log(msg):
    print(msg, flush=True)


def safe(status, key, fn, default):
    try:
        v = fn()
        status.setdefault(key, "ok")
        return v
    except Exception as e:  # noqa: BLE001
        status[key] = f"failed: {str(e)[:120]}"
        log(f"  ! {key}: {e}")
        traceback.print_exc()
        return default


# ====================================================================== main
def main():
    now = sources.now_utc()
    status: dict[str, str] = {}
    log(f"TradeLab build @ {now.isoformat()}")

    # ---- calendar
    state = safe(status, "sleeper_state", sources.sleeper_state, {})
    season = int(state.get("league_season") or state.get("season") or (now.year if now.month >= 3 else now.year - 1))
    sched = safe(status, "schedules", lambda: sources.schedules(season), None)
    week = current_week(sched, state)
    log(f"  season {season}, week {week}")

    # ---- ids
    ids = safe(status, "player_ids", lambda: sources.dp_csv("db_playerids.csv"), None)
    fp_to = {}
    sl_to_gsis = {}
    if ids is not None:
        for r in ids.select("fantasypros_id", "sleeper_id", "gsis_id").iter_rows():
            if r[0]:
                fp_to[r[0]] = (r[1], r[2])
            if r[1] and r[2]:
                sl_to_gsis[r[1]] = r[2]

    # ---- market: FantasyCalc (real trades), one pull per format
    trade = {}
    for key, (ppr, qbs) in FORMATS.items():
        rows = safe(status, f"fantasycalc_{key}", lambda p=ppr, q=qbs: sources.fantasycalc(p, q), [])
        trade[key] = rows
    status["fantasycalc"] = "ok" if any(trade.values()) else "failed"

    # ---- experts: FantasyPros rest-of-season consensus via DynastyProcess
    ecr = safe(status, "expert_rankings", lambda: sources.dp_csv("db_fpecr_latest.csv"), None)

    # ---- sleeper
    sl = safe(status, "sleeper_players", sources.sleeper_players, {})
    adds = {r["player_id"]: r["count"] for r in safe(status, "sleeper_trending_add", lambda: sources.sleeper_trending("add"), [])}
    drops = {r["player_id"]: r["count"] for r in safe(status, "sleeper_trending_drop", lambda: sources.sleeper_trending("drop"), [])}

    # ---- assemble player universe
    players: dict[str, dict] = {}

    def get(pid, name, pos, tm):
        p = players.get(pid)
        if not p:
            p = players[pid] = {"id": pid, "name": name, "pos": pos, "team": team(tm), "trade": {}, "expert": None}
        return p

    for key, rows in trade.items():
        for it in rows or []:
            pl_ = it.get("player", {})
            pos = pl_.get("position")
            if pos not in POS:
                continue
            sid = str(pl_.get("sleeperId") or "") or f"fc{pl_.get('id')}"
            p = get(sid, pl_.get("name"), pos, pl_.get("maybeTeam"))
            p["trade"][key] = round(float(it.get("value") or 0), 1)
            if pl_.get("maybeAge"):
                p["age"] = round(float(pl_["maybeAge"]), 1)

    if ecr is not None:
        for pos in POS:
            sub = ecr.filter(pl.col("page_type") == f"redraft-{pos.lower()}")
            for r in sub.iter_rows(named=True):
                sid, _ = fp_to.get(r["id"], (None, None))
                pid = sid or f"fp{r['id']}"
                p = get(pid, r["player"], pos, r["tm"] or r["team"])
                p["expert"] = {"posRank": float(r["ecr"]), "sd": float(r["sd"] or 0)}
        status["expert_rankings"] = f"ok (scraped {ecr['scrape_date'][0]})" if ecr.height else "empty"

    # Sleeper: authoritative team + injury
    for pid, p in players.items():
        s = sl.get(pid)
        if not s:
            continue
        if s.get("team"):
            p["team"] = team(s["team"])
        if not p.get("age") and s.get("age"):
            p["age"] = s["age"]
        p["inj"] = sleeper_injury(s)
        if pid in adds or pid in drops:
            p["trend"] = {"adds": adds.get(pid, 0), "drops": drops.get(pid, 0)}

    # drop deep-bench noise: keep players with any market or expert signal
    players = {k: v for k, v in players.items() if v["trade"] or v["expert"]}

    # ---- stats: production, durability, defense vs position
    stats = safe(status, "nflverse_stats", lambda: sources.player_stats([season - 1, season]), None)
    teams = {}
    if stats is not None:
        stats = normalize_stats(stats)
        add_production(players, stats, season, sl_to_gsis, status)
    if sched is not None:
        teams = team_table(sched, stats, season, week)

    # ---- team factor needs QB health (from Sleeper + news later)
    # ---- news
    feeds = json.loads(FEEDS_FILE.read_text())
    history = json.loads(NEWS_FILE.read_text()) if NEWS_FILE.exists() else []
    plist = sorted(players.values(), key=lambda p: -max(p["trade"].values() or [0]))[:450]
    hist = safe(status, "news", lambda: news.update_news(plist, feeds, history, now, status), history)
    NEWS_FILE.parent.mkdir(exist_ok=True)
    NEWS_FILE.write_text(json.dumps([{k: v for k, v in h.items() if k != "eff"} for h in hist], indent=1))
    by_pid = defaultdict(list)
    for h in hist:
        by_pid[h["pid"]].append(h)
    for pid, items in by_pid.items():
        p = players.get(pid)
        if not p:
            continue
        p["news"] = [{k: h[k] for k in ("date", "headline", "link", "source", "pct", "eff", "status", "weeksOut", "note", "method")} for h in items[:8]]
        # recent injury news can sharpen Sleeper's status
        for h in items:
            age_days = (now - datetime.fromisoformat(h["date"])).days
            if h.get("weeksOut") == 99 and age_days <= 21:
                p["inj"] = {"s": "IR", "w": 99, "note": h["note"], "src": "news"}
                break
            if h.get("status") in ("OUT", "IR") and age_days <= 2 and (p.get("inj") or {}).get("s", "H") == "H":
                p["inj"] = {"s": h["status"], "w": h.get("weeksOut") or 1, "note": h["note"], "src": "news"}
                break

    # ---- QB health → team factors
    apply_qb_health(players, teams)

    out = {
        "meta": {"updated": now.isoformat(timespec="minutes"), "season": season, "week": week,
                 "fantasyLastWeek": FANTASY_LAST_WEEK, "playoffWeeks": list(PLAYOFF_WEEKS), "status": status},
        "teams": teams,
        "players": sorted(players.values(), key=lambda p: -max(p["trade"].values() or [0])),
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":")))
    log(f"  wrote {OUT} — {len(out['players'])} players, {len(teams)} teams")
    for k, v in status.items():
        log(f"    {k}: {v}")


# ====================================================================== helpers
def current_week(sched, state):
    if state.get("week") and state.get("season_type") == "regular":
        # Sleeper's week flips on Tuesday; it's the week being played next
        w = int(state["week"])
        if sched is not None:
            return max(w, _sched_week(sched) or w)
        return w
    return _sched_week(sched) or 1


def _sched_week(sched):
    if sched is None:
        return None
    s = sched.filter((pl.col("game_type") == "REG") & pl.col("home_score").is_null())
    return int(s["week"].min()) if s.height else 19


def sleeper_injury(s):
    st = (s.get("injury_status") or "").upper()
    note = " ".join(x for x in [s.get("injury_body_part"), s.get("injury_notes")] if x) or ""
    if st in ("IR", "PUP", "NFI"):
        return {"s": "IR", "w": 4, "note": note or st, "src": "sleeper"}
    if st in ("OUT", "DOUBTFUL", "SUS", "SUSP"):
        return {"s": "OUT", "w": 1, "note": note or st.title(), "src": "sleeper"}
    if st == "QUESTIONABLE":
        return {"s": "Q", "w": 0, "note": note or "Questionable", "src": "sleeper"}
    return {"s": "H", "w": 0, "note": "", "src": "sleeper"}


def normalize_stats(df: pl.DataFrame) -> pl.DataFrame:
    cols = set(df.columns)
    tcol = "team" if "team" in cols else "recent_team"
    df = df.with_columns(
        pl.col(tcol).map_elements(team, return_dtype=pl.Utf8).alias("tm"),
        pl.col("opponent_team").map_elements(team, return_dtype=pl.Utf8).alias("opp"),
        pl.col("fantasy_points_ppr").cast(pl.Float64).fill_null(0).alias("ppr"),
        pl.col("fantasy_points").cast(pl.Float64).fill_null(0).alias("std"),
        pl.col("receptions").cast(pl.Float64).fill_null(0).alias("rec"),
    ).with_columns((pl.col("std") + 0.5 * pl.col("rec")).alias("half"))
    if "season_type" in cols:
        df = df.filter(pl.col("season_type") == "REG")
    return df.filter(pl.col("position").is_in(POS))


def add_production(players, stats, season, sl_to_gsis, status):
    cur = stats.filter(pl.col("season") == season)
    prev = stats.filter(pl.col("season") == season - 1)
    if cur.height == 0:
        status["nflverse_stats"] = "no current-season rows"
        return
    last_wk = int(cur["week"].max())
    g = (cur.sort("week").group_by("player_id").agg(
        pl.len().alias("gp"), pl.col("ppr").mean(), pl.col("half").mean(), pl.col("std").mean(),
        pl.col("ppr").tail(4).mean().alias("r_ppr"), pl.col("half").tail(4).mean().alias("r_half"),
        pl.col("std").tail(4).mean().alias("r_std"), pl.col("week").max().alias("lastwk")))
    gm = {r["player_id"]: r for r in g.iter_rows(named=True)}
    gp_prev = {r[0]: r[1] for r in prev.group_by("player_id").agg(pl.len()).iter_rows()}
    n = 0
    for pid, p in players.items():
        gs = sl_to_gsis.get(pid)
        r = gm.get(gs) if gs else None
        if not r:
            continue
        n += 1
        p["gp"] = r["gp"]
        p["ppg"] = {k: round(r[k], 2) for k in ("ppr", "half", "std")}
        p["recent"] = {k: round(r["r_" + k], 2) for k in ("ppr", "half", "std")}
        # durability: share of available games played over the last two seasons
        avail = (last_wk - 1) + (16 if gs in gp_prev else 0)  # minus a bye each season
        played = r["gp"] + gp_prev.get(gs, 0)
        ratio = played / max(1, avail)
        if avail < 6:
            d = "A"
        elif ratio >= 0.94:
            d = "L"
        elif ratio >= 0.85:
            d = "A"
        elif ratio >= 0.72:
            d = "A+"
        else:
            d = "H"
        p["dur"] = d
        p["durNote"] = f"Played {played} of ~{avail} games over the last two seasons"
    status["nflverse_stats"] = f"ok (through week {last_wk}, {n} players matched)"


def team_table(sched, stats, season, week):
    s = sched.filter((pl.col("season") == season) & (pl.col("game_type") == "REG"))
    s = s.with_columns(pl.col("home_team").map_elements(team, return_dtype=pl.Utf8),
                       pl.col("away_team").map_elements(team, return_dtype=pl.Utf8))
    teams = {}
    games = list(s.iter_rows(named=True))
    all_t = sorted({g["home_team"] for g in games} | {g["away_team"] for g in games})
    # scoring offense
    pts = defaultdict(list)
    for gm in games:
        if gm["home_score"] is not None:
            pts[gm["home_team"]].append(gm["home_score"])
            pts[gm["away_team"]].append(gm["away_score"])
    league_ppg = sum(sum(v) for v in pts.values()) / max(1, sum(len(v) for v in pts.values()))

    # fantasy points allowed per game by defense, by position (shrunk toward league mean)
    allowed = {}
    if stats is not None:
        cur = stats.filter(pl.col("season") == season)
        per = cur.group_by(["opp", "week", "position"]).agg(pl.col("ppr").sum())
        agg = per.group_by(["opp", "position"]).agg(pl.col("ppr").mean().alias("m"), pl.len().alias("n"))
        lg = per.group_by("position").agg(pl.col("ppr").mean().alias("lm"))
        lgm = {r[0]: r[1] for r in lg.iter_rows()}
        for r in agg.iter_rows(named=True):
            k = 3.0
            allowed[(r["opp"], r["position"])] = (r["m"] * r["n"] + lgm[r["position"]] * k) / (r["n"] + k)
    else:
        lgm = {}

    for t in all_t:
        tg = [gm for gm in games if t in (gm["home_team"], gm["away_team"])]
        weeks = {gm["week"] for gm in tg}
        bye = next((w for w in range(1, 19) if w not in weeks and w <= 18), None)
        rem = [gm for gm in tg if gm["home_score"] is None and gm["week"] >= week and gm["week"] <= FANTASY_LAST_WEEK]
        sos = {}
        for pos in POS:
            vals = [allowed.get((gm["away_team"] if gm["home_team"] == t else gm["home_team"], pos), lgm.get(pos)) for gm in rem]
            po = [allowed.get((gm["away_team"] if gm["home_team"] == t else gm["home_team"], pos), lgm.get(pos))
                  for gm in rem if gm["week"] in PLAYOFF_WEEKS]
            vals = [v for v in vals if v is not None]
            po = [v for v in po if v is not None]
            sos[pos] = {"ros": round(sum(vals) / len(vals), 2) if vals else None,
                        "po": round(sum(po) / len(po), 2) if po else None, "n": len(vals)}
        ppg = sum(pts[t]) / len(pts[t]) if pts[t] else league_ppg
        off = max(0.92, min(1.08, 1 + 0.5 * (ppg / league_ppg - 1))) if league_ppg else 1.0
        teams[t] = {"bye": bye, "ppg": round(ppg, 1), "off": round(off, 3),
                    "why": f"Scoring {ppg:.1f} pts/game (league {league_ppg:.1f})", "sos": sos,
                    "opps": [{"w": gm["week"], "o": gm["away_team"] if gm["home_team"] == t else "@" + gm["home_team"]} for gm in rem]}
    return teams


def apply_qb_health(players, teams):
    qbs = defaultdict(list)
    for p in players.values():
        if p["pos"] == "QB":
            qbs[p["team"]].append(p)
    for t, info in teams.items():
        cand = sorted(qbs.get(t, []), key=lambda p: -max(p["trade"].values() or [0]))
        info["skill"] = info["off"]
        info["qb"] = info["off"]
        if not cand:
            continue
        starter = cand[0]
        inj = starter.get("inj") or {"s": "H"}
        info["starterQB"] = starter["name"]
        if inj["s"] in ("OUT", "IR"):
            hit = 0.86 if inj.get("w", 1) >= 4 else 0.93
            info["skill"] = round(info["off"] * hit, 3)
            info["why"] += f"; starting QB {starter['name']} {'on IR' if inj['s']=='IR' else 'out'}"


if __name__ == "__main__":
    main()
