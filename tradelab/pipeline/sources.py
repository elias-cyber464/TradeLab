"""Data fetchers. Every source is free and needs no API key.

Set TRADELAB_FIXTURES=/path/to/dir to read local copies instead of the network
(used by the offline tests).
"""
from __future__ import annotations

import io
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import requests

UA = "TradeLab/1.0 (open-source fantasy trade calculator; github actions)"
FIX = os.environ.get("TRADELAB_FIXTURES")
CACHE = Path(os.environ.get("TRADELAB_CACHE", ".cache"))
CACHE.mkdir(exist_ok=True)

DP_RAW = "https://raw.githubusercontent.com/dynastyprocess/data/master/files/"

_session = requests.Session()
_session.headers["User-Agent"] = UA


def _get(url: str, *, as_json=True, tries=3, timeout=30):
    last = None
    for i in range(tries):
        try:
            r = _session.get(url, timeout=timeout)
            r.raise_for_status()
            return r.json() if as_json else r.content
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


def _fixture(name: str):
    p = Path(FIX) / name
    if not p.exists():
        raise FileNotFoundError(p)
    return p


# ---------------------------------------------------------------- FantasyCalc
def fantasycalc(ppr: float, qbs: int, teams: int = 12) -> list[dict]:
    """Redraft trade values computed from real fantasy trades."""
    if FIX:
        return json.loads(_fixture(f"fantasycalc_{ppr}_{qbs}.json").read_text())
    url = (
        "https://api.fantasycalc.com/values/current"
        f"?isDynasty=false&numQbs={qbs}&numTeams={teams}&ppr={ppr}"
    )
    return _get(url)


# ---------------------------------------------------------------- Sleeper
def sleeper_players() -> dict:
    """All NFL players with injury status. Sleeper asks that this endpoint be
    called at most once a day, so it is cached for 20 hours."""
    if FIX:
        return json.loads(_fixture("sleeper_players.json").read_text())
    f = CACHE / "sleeper_players.json"
    if f.exists():
        try:
            c = json.loads(f.read_text())
            if time.time() - c["fetched"] < 20 * 3600:
                return c["data"]
        except Exception:  # noqa: BLE001
            pass
    data = _get("https://api.sleeper.app/v1/players/nfl", timeout=90)
    slim = {k: {x: v.get(x) for x in ("full_name", "position", "team", "injury_status", "injury_body_part", "injury_notes", "age")}
            for k, v in data.items() if v.get("position") in ("QB", "RB", "WR", "TE")}
    f.write_text(json.dumps({"fetched": time.time(), "data": slim}))
    return slim


def sleeper_trending(kind: str, hours: int = 24, limit: int = 200) -> list[dict]:
    if FIX:
        p = Path(FIX) / f"sleeper_trending_{kind}.json"
        return json.loads(p.read_text()) if p.exists() else []
    url = f"https://api.sleeper.app/v1/players/nfl/trending/{kind}?lookback_hours={hours}&limit={limit}"
    return _get(url)


def sleeper_state() -> dict:
    if FIX:
        p = Path(FIX) / "sleeper_state.json"
        return json.loads(p.read_text()) if p.exists() else {}
    return _get("https://api.sleeper.app/v1/state/nfl")


# ---------------------------------------------------------------- DynastyProcess (FantasyPros consensus + ID map)
def dp_csv(name: str) -> pl.DataFrame:
    raw = _fixture(name).read_bytes() if FIX else _get(DP_RAW + name, as_json=False, timeout=90)
    return pl.read_csv(io.BytesIO(raw), infer_schema_length=0, null_values=["NA", ""])


# ---------------------------------------------------------------- nflverse
def schedules(season: int) -> pl.DataFrame:
    if FIX:
        return pl.read_csv(_fixture("schedules.csv"), null_values=["NA", ""])
    import nflreadpy as nfl

    return nfl.load_schedules([season])


def player_stats(seasons: list[int]) -> pl.DataFrame:
    if FIX:
        return pl.read_csv(_fixture("player_stats.csv"), null_values=["NA", ""])
    import nflreadpy as nfl

    frames = []
    for s in seasons:
        try:
            frames.append(nfl.load_player_stats([s], summary_level="week"))
        except Exception as e:  # noqa: BLE001
            print(f"  ! player stats {s} unavailable: {e}")
    if not frames:
        raise RuntimeError("no player stats")
    return pl.concat(frames, how="diagonal_relaxed")


# ---------------------------------------------------------------- RSS news
def rss(url: str):
    import feedparser

    if FIX:
        name = "rss_" + "".join(c if c.isalnum() else "_" for c in url)[-60:] + ".xml"
        p = Path(FIX) / name
        return feedparser.parse(p.read_bytes()) if p.exists() else feedparser.parse(b"")
    content = _get(url, as_json=False, timeout=20, tries=2)
    return feedparser.parse(content)


def now_utc() -> datetime:
    if FIX and os.environ.get("TRADELAB_NOW"):
        return datetime.fromisoformat(os.environ["TRADELAB_NOW"])
    return datetime.now(timezone.utc)
