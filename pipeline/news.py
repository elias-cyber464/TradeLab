"""News: read RSS feeds, find which players each story is about, and score
how much it moves their rest-of-season value.

Two scorers:
  * AI (free): any OpenAI-compatible API. Default is Groq's free tier
    (set the GROQ_API_KEY repository secret). It understands indirect effects
    (a starter's injury helps the backup).
  * Rules (always available): keyword patterns. Used if the AI is off or fails.

Old news fades: effect halves every 7 days (the market catches up), and
stories older than 21 days are dropped.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from html import unescape

import requests

from . import sources

HALF_LIFE_DAYS = 7
KEEP_DAYS = 21

# (regex, pct, status, weeksOut, label)  — first match wins, ordered by severity
RULES = [
    (r"season[- ]ending|out for the (season|year)|torn (acl|achilles)|ruptured achilles|achilles (tear|injury)", 0, "IR", 99, "Season-ending injury"),
    (r"(placed|lands|landed|moved|going) on (injured reserve|ir)\b|to injured reserve", 0, "IR", 4, "Placed on IR"),
    (r"\bsuspend(ed|s)\b|suspension", -15, "OUT", None, "Suspension"),
    (r"\b(released|waived|cut by)\b", -30, None, None, "Released"),
    (r"ruled out|(will not|won't|not expected to) play|\binactive\b|\bdoubtful\b", 0, "OUT", 1, "Ruled out"),
    (r"\bbenched\b|demoted|relegated|loses? (his |the )?(starting )?(job|role)|backup role|healthy scratch|lost (his|the) job", -15, None, None, "Lost role"),
    (r"named (the )?starter|will start|takes over|lead (back|role)|promoted|bigger role|expanded role|workhorse|starting job", 12, None, None, "Bigger role"),
    (r"\bquestionable\b|game-time decision|limited (in )?practice|\bday-to-day\b", 0, "Q", 0, "Questionable"),
    (r"activated|designated to return|returns? to practice|cleared|full participant|back at practice", 4, "H", 0, "Returning"),
    (r"traded to|acquired|has been traded", 0, None, None, "Traded"),
]
RULES = [(re.compile(p, re.I), *rest) for p, *rest in RULES]
NUM_GAMES = re.compile(r"(\w+)[- ]games?", re.I)
WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "eight": 8, "ten": 10}


def _clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", unescape(s or ""))
    return re.sub(r"\s+", " ", s).strip()


ABBR = re.compile(r"\b(Jr|Sr|St|Mr|Dr|vs|No|II|III)\.", re.I)


def _clauses(text: str) -> list[str]:
    """Split into sentences, then clauses, without breaking on 'Jr.' etc."""
    t = ABBR.sub(lambda m: m.group(1) + "<DOT>", text)
    out = []
    for sent in re.split(r"(?<=[.!?])\s+", t):
        for c in re.split(r"[;,]\s+|\s+(?:while|but|with|as|after|and)\s+(?=[A-Z])", sent):
            c = c.replace("<DOT>", ".").strip()
            if c:
                out.append(c)
    return out


def build_name_index(players: list[dict]):
    """Full-name regexes for every player in the universe."""
    idx = []
    for p in players:
        n = p["name"]
        base = re.sub(r"\s+(jr\.?|sr\.?|ii|iii|iv)$", "", n, flags=re.I)
        pat = re.compile(r"\b" + re.escape(base).replace(r"\.", r"\.?") + r"\b", re.I)
        last = base.split()[-1]
        idx.append((p, pat, re.compile(r"\b" + re.escape(last) + r"\b", re.I)))
    return idx


def fetch_entries(feeds: list[dict], now: datetime, status: dict) -> list[dict]:
    out = []
    for f in feeds:
        try:
            parsed = sources.rss(f["url"])
            n = 0
            for e in parsed.entries[:80]:
                t = e.get("published_parsed") or e.get("updated_parsed")
                dt = datetime(*t[:6], tzinfo=timezone.utc) if t else now
                if now - dt > timedelta(days=KEEP_DAYS):
                    continue
                out.append({
                    "title": _clean(e.get("title", "")),
                    "summary": _clean(e.get("summary", "") or e.get("description", ""))[:900],
                    "link": e.get("link", ""),
                    "source": f["name"],
                    "date": dt.isoformat(),
                })
                n += 1
            status[f["name"]] = f"ok ({n} stories)"
        except Exception as e:  # noqa: BLE001
            status[f["name"]] = f"failed: {str(e)[:80]}"
    return out


def match_players(entry: dict, idx) -> list[dict]:
    text = entry["title"] + ". " + entry["summary"]
    return [p for p, full, _ in idx if full.search(text)]


def score_rules(entry: dict, player: dict, idx_by_id, others: list[dict]) -> dict | None:
    """Assign each clause to the player(s) it mentions; a clause naming nobody
    belongs to whoever was mentioned last. Then match rules on this player's clauses."""
    text = entry["title"] + ". " + entry["summary"]
    owners_prev: set[str] = set()
    mine = []
    for c in _clauses(text):
        owners = {o["id"] for o in others if idx_by_id[o["id"]][1].search(c) or idx_by_id[o["id"]][2].search(c)}
        if not owners:
            owners = owners_prev
        if player["id"] in owners:
            mine.append(c)
        owners_prev = owners
    for rx, pct, st, w, label in RULES:
        for s in mine:
            if rx.search(s):
                if label == "Suspension":
                    m = NUM_GAMES.search(s)
                    g = None
                    if m:
                        g = WORDNUM.get(m.group(1).lower()) or (int(m.group(1)) if m.group(1).isdigit() else None)
                    w = g or 1
                return {"pct": pct, "status": st, "weeksOut": w, "note": label, "method": "rules"}
    return None


# ---------------------------------------------------------------- GitHub Models (free)
AI_URL = os.environ.get("AI_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/") + "/chat/completions"
AI_MODELS = [m for m in [os.environ.get("AI_MODEL"), "openai/gpt-oss-120b", "llama-3.3-70b-versatile"] if m]
AI_NAME = os.environ.get("AI_NAME", "Groq")
_ai_dead = False


def _parse_json(txt: str):
    """Accept plain JSON, ```json fenced JSON, or JSON buried in prose."""
    if not txt:
        raise ValueError("empty reply")
    t = txt.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:  # noqa: BLE001
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def score_ai(batch: list[tuple[dict, list[dict]]], roster: list[dict]) -> dict | None:
    """roster = players named in these stories plus their teammates."""
    global _ai_dead
    token = os.environ.get("AI_API_KEY") or os.environ.get("GROQ_API_KEY")
    if _ai_dead or not token or os.environ.get("TRADELAB_AI", "on") == "off":
        return None
    stories = [f"[{i}] {e['title']} | {e['summary'][:400]} (named: {', '.join(p['name'] for p in ps)})"
               for i, (e, ps) in enumerate(batch)]
    names = "; ".join(f"{p['name']} ({p['pos']}, {p['team']})" for p in roster)
    prompt = (
        "You are a fantasy football analyst scoring news for rest-of-season REDRAFT value (PPR).\n"
        "For each story, list every affected player from the PLAYER LIST, including indirect effects "
        "(a starter's injury helps his backup; a QB injury hurts his receivers).\n"
        "pct = integer -50..50 change in value from role, opportunity, suspension, or team change. "
        "Do NOT put missed games into pct; use status/weeksOut for injuries.\n"
        "status = H, Q, OUT, IR or null. weeksOut = games missed starting this week (99 = season) or null.\n"
        "Skip stories with no real fantasy effect. Use exact names from the list.\n"
        'Reply with JSON only, no other text: {"items":[{"story":0,"player":"","pct":0,"status":null,"weeksOut":null,"note":"short reason"}]}\n\n'
        f"PLAYER LIST: {names}\n\nSTORIES:\n" + "\n".join(stories)
    )
    last = ""
    for model in AI_MODELS:
        try:
            r = requests.post(
                AI_URL,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                         "Accept": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.1},
                timeout=60,
            )
            body = r.text
            if r.status_code in (401, 403):
                last = f"{model}: HTTP {r.status_code} {body[:200]}"
                _ai_dead = True  # permissions problem, no point retrying this run
                break
            if r.status_code == 429:
                last = f"{model}: rate limited"
                _ai_dead = True
                break
            if r.status_code >= 400:
                last = f"{model}: HTTP {r.status_code} {body[:200]}"
                continue
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            out = _parse_json(content)
            print(f"  AI scoring ok via {model}")
            return out
        except Exception as e:  # noqa: BLE001
            last = f"{model}: {str(e)[:120]} | reply starts: {(locals().get('body') or '')[:200]!r}"
            continue
    print(f"  ! AI scoring unavailable ({last}); using rules")
    return None


# ---------------------------------------------------------------- main entry
def update_news(players: list[dict], feeds: list[dict], history: list[dict], now: datetime, status: dict) -> list[dict]:
    """Returns the new history list (with decayed effects) and fills status."""
    idx = build_name_index(players)
    idx_by_id = {p["id"]: (p, full, last) for p, full, last in idx}
    by_name = {p["name"]: p for p in players}
    seen = {h["id"] for h in history}

    entries = fetch_entries(feeds, now, status)
    fresh = []
    for e in entries:
        ps = match_players(e, idx)
        if not ps:
            continue
        key = hashlib.sha1((e["link"] or e["title"]).encode()).hexdigest()[:12]
        if any(f"{key}:{p['id']}" in seen for p in ps):
            continue
        fresh.append((key, e, ps))

    new_items = []
    # AI pass in batches of 15 stories
    ai_used = 0
    by_team = {}
    for p in players:
        by_team.setdefault(p["team"], []).append(p)
    for i in range(0, len(fresh), 10):
        chunk = fresh[i:i + 10]
        named = {p["id"]: p for _, _, ps in chunk for p in ps}
        roster = {**named}
        for p in list(named.values()):
            for t in sorted(by_team.get(p["team"], []), key=lambda x: -max(x["trade"].values() or [0]))[:12]:
                roster[t["id"]] = t
        res = score_ai([(e, ps) for _, e, ps in chunk], list(roster.values()))
        handled = set()
        if res and isinstance(res.get("items"), list):
            ai_used += 1
            for it in res["items"]:
                try:
                    key, e, _ = chunk[int(it["story"])]
                except Exception:  # noqa: BLE001
                    continue
                p = by_name.get(it.get("player"))
                if not p:
                    continue
                handled.add(key)
                new_items.append(_item(key, e, p, int(max(-50, min(50, it.get("pct") or 0))),
                                       it.get("status"), it.get("weeksOut"), it.get("note") or e["title"], "ai"))
        for key, e, ps in chunk:
            if key in handled:
                continue
            for p in ps:
                s = score_rules(e, p, idx_by_id, ps)
                if s:
                    new_items.append(_item(key, e, p, s["pct"], s["status"], s["weeksOut"], s["note"], "rules"))

    if ai_used:
        status["news_scoring"] = f"AI ({AI_NAME}) on {ai_used} batch(es)"
    elif not (os.environ.get("AI_API_KEY") or os.environ.get("GROQ_API_KEY")):
        status["news_scoring"] = "keyword rules (add a GROQ_API_KEY secret for AI scoring)"
    elif not fresh:
        status["news_scoring"] = "no new stories this run"
    else:
        status["news_scoring"] = "keyword rules (AI request failed, see build log)"
    hist = [h for h in history + new_items if now - datetime.fromisoformat(h["date"]) <= timedelta(days=KEEP_DAYS)]
    # dedupe
    uniq = {}
    for h in hist:
        uniq[h["id"]] = h
    hist = sorted(uniq.values(), key=lambda h: h["date"], reverse=True)
    for h in hist:
        age = (now - datetime.fromisoformat(h["date"])).total_seconds() / 86400
        h["eff"] = round(h["pct"] * 0.5 ** (max(0, age) / HALF_LIFE_DAYS), 1)
    return hist


def _item(key, e, p, pct, st, w, note, method):
    return {"id": f"{key}:{p['id']}", "pid": p["id"], "name": p["name"], "date": e["date"],
            "headline": e["title"][:200], "link": e["link"], "source": e["source"],
            "pct": pct, "status": st if st in ("H", "Q", "OUT", "IR") else None,
            "weeksOut": w if isinstance(w, int) else None, "note": str(note)[:160], "method": method}
