"""
liga.py - Banana League: streamers ranked by their average banana score over a week (real data only).

Shared by the long-form (kompilace.py, Sunday "Banana League" episode) and the shorts (klipuj.py /
publikuj.py, second agent). Sources:
  long_pool.json            every moment we know (rendered shorts + long-form pool), with views
  publikovano*.json         what went out as a short (id -> due time); its meta.json in out/ gives the
                            channel, views and the banana score ("rating")
Banana score of a moment = meta "rating" if present, else klipuj.banana_rating(views) (1-10, log scale).

  table(days=7, end=None) -> [{channel, n, avg, total, best, rank, rank_prev}], best first
  python liga.py [--days 7] [--end 2026-11-24]
"""

import argparse
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
POOL = Path(os.environ.get("KLIPY_POOL") or HERE / "long_pool.json")
MIN_MOMENTS = 2                   # one lucky clip does not make a league leader

def _load(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def banana_rating(views):
    """Same scale as klipuj.banana_rating (no heavy imports here): 50 -> 1, 5k -> 6, 500k -> 10."""
    return int(round(max(1, min(10, 1 + math.log10(max(views or 1, 1) / 50) * 2.25))))

def _ts(s):
    try:
        return datetime.fromisoformat(s).timestamp()
    except (TypeError, ValueError):
        return 0

def moments():
    """{id: {channel, ts, score}} from the pool and from published shorts (their meta.json)."""
    out = {}
    for r in _load(POOL).values():
        if r.get("channel") and (r.get("created") or r.get("added")):
            out[r["id"]] = {"channel": r["channel"].lower(), "ts": r.get("created") or r.get("added"),
                            "score": r.get("rating") or banana_rating(r.get("views"))}
    metas = {}
    for mf in (HERE / "out").glob("*/*/meta.json"):
        if not mf.parts[-3].startswith("_"):
            m = _load(mf)
            if m.get("id"):
                metas[m["id"]] = m
    for f in HERE.glob("publikovano*.json"):
        if "long" in f.name:
            continue
        for cid, rec in _load(f).items():
            m = metas.get(cid) or {}
            ch = m.get("channel") or (out.get(cid) or {}).get("channel")
            if not ch:
                continue
            out[cid] = {"channel": ch.lower(), "ts": m.get("created") or _ts(rec.get("due")),
                        "score": m.get("rating") or banana_rating(m.get("views") or (out.get(cid) or {}).get("views"))}
    return out

def _rank(rows):
    rows = sorted(rows, key=lambda r: (-r["avg"], -r["n"], -r["best"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows

def _window(ms, t0, t1):
    per = {}
    for m in ms.values():
        if t0 <= (m["ts"] or 0) < t1:
            per.setdefault(m["channel"], []).append(m["score"])
    return _rank([{"channel": ch, "n": len(s), "avg": round(sum(s) / len(s), 2), "total": sum(s), "best": max(s)}
                  for ch, s in per.items() if len(s) >= MIN_MOMENTS])

def table(days=7, end=None):
    """League of the `days` before `end` (date/datetime/ISO string; default now), with last period's rank."""
    if end is None:
        t1 = datetime.now(timezone.utc).timestamp()
    else:
        e = datetime.fromisoformat(str(end)) if not isinstance(end, datetime) else end
        e = e if e.tzinfo else e.replace(tzinfo=timezone.utc)
        t1 = e.timestamp()
    t0 = t1 - days * 86400
    ms = moments()
    now, prev = _window(ms, t0, t1), _window(ms, t0 - days * 86400, t0)
    before = {r["channel"]: r["rank"] for r in prev}
    for r in now:
        r["rank_prev"] = before.get(r["channel"])
    return now

def movement(r):
    """'UP 2', 'DOWN 1', 'SAME', 'NEW' - for cards (only real data)."""
    if r.get("rank_prev") is None:
        return "NEW"
    d = r["rank_prev"] - r["rank"]
    return f"UP {d}" if d > 0 else f"DOWN {-d}" if d < 0 else "SAME"

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Banana League table")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--end")
    a = p.parse_args()
    for r in table(a.days, a.end):
        print(f"{r['rank']:>3}. {r['channel']:<20} avg {r['avg']:>5}  n {r['n']:>3}  best {r['best']:>2}  {movement(r)}")
