"""
kompilace.py - long-form YouTube compilations (16:9, 8-15 min) from collected stream moments.

  python kompilace.py --znacka bananas --serie funny-day                 # yesterday (Prague)
  python kompilace.py --znacka fanpage --serie wild-day --od 2026-11-19 --do 2026-11-19
  python kompilace.py --znacka bananas --serie top-week                  # last 7 days
  python kompilace.py --znacka fanpage --serie streamer:xqc --od 2026-11-19 --do 2026-11-25
  python kompilace.py ... --zkouska                                      # plan only, no render

Series: funny-day, wild-day, glitches, chases, top-week, streamer:<login> (docs/long-form.md).

1. Candidates: long_pool.json (every rendered short + candidates that missed the shorts quota)
   + the Twitch API listing of the category in the window (if keys). Same game filter as shorts.
2. Pick 20-40 moments for the series (tags), at most 4 per streamer, never a moment the other
   brand used (long_used.json, shared) or a short of the other brand. Missing source/tags are
   filled cheaply (download + Whisper + tags, no 9:16 render; klipuj.collect).
3. Cut every moment the HPC way: start in the action, end ~3 s after the payoff. Original
   16:9 stream layout (game + streamer's own facecam). Small @channel credit, brand watermark
   and the countdown number in the corners. Cold open ~15 s from the best 3, then the countdown
   from the weakest to #1. bananas: small banana score after each moment; fanpage: its own
   neon sweep transition. Our music only, quietly, under the transitions.
4. out/_long/<brand>/<date>_<series>.mp4 + .meta.json (title, description with chapters and
   credits, hashtags, tags, suggested upload time) + <date>_<series>/nahled_vstup (thumbnail
   inputs) and nahled.jpg (AI thumbnail, only with Cloudflare keys).
"""

import argparse
import json
import math
import os
import random
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

import klipuj as K

HERE = Path(__file__).resolve().parent
OUT = HERE / "out" / "_long"
USED = Path(os.environ.get("LONG_USED") or HERE / "long_used.json")
TZ = ZoneInfo("Europe/Prague")
W, H, FPS = 1920, 1080, 30
LAUNCH = date(2026, 11, 19)
FONT_B = HERE / "fonts" / "cap" / "montserrat-900.ttf"
FONT_M = HERE / "fonts" / "montserrat-600.ttf"
COL = {"bananas": "FFE135", "fanpage": "FF4FD8"}
DARK = {"bananas": "111111", "fanpage": "0B0B12"}
SWEEP = 0.7                       # fanpage transition (s)
CARD = 2.6                        # bananas score card (s), the clip keeps running under it

EPISODES = Path(os.environ.get("LONG_EPISODES") or HERE / "long_episodes.json")   # {brand: {series: last #}}
RESERVE = Path(os.environ.get("LONG_RESERVE") or HERE / "long_reserve.json")      # {brand: {series: [...]}}
FUN_MIN = 0.35                    # entertainment floor: quiet walking / shopping / menus never get in
MIN_VERIFIED = 15                 # a themed series needs this many verified moments, else the main series
MIN_VIDEO = 300.0                 # shorter than 5:00: no video
CHAPTER_EXTRA = {"bananas": 1.5, "fanpage": 1.5}           # the tier list animation after every moment

# Every series: categories it needs (None = any moment), tag weights for ranking, the vision question
# (kategorie.QUESTION key), the window in days, title. Titles: no invented numbers, #N = episode.
SERIES = {
    "funny-day": {"cats": None, "w": {"funny": 1.0, "rage": 0.6, "fail": 0.6, "wow": 0.4, "physics": 0.5}, "days": 1,
                  "title": "GTA 6 Funniest Moments #{n} ({who})"},
    "wild-day": {"cats": None, "w": {"crash": 1.0, "chase": 0.9, "stunt": 0.8, "wow": 0.7, "rage": 0.5, "glitch": 0.5},
                 "days": 2, "title": "Wildest GTA 6 Moments #{n} ({who})"},
    "streamer-night": {"cats": None, "w": {"crash": 0.8, "chase": 0.8, "wow": 0.8, "funny": 0.6, "rage": 0.5},
                       "days": 1, "night": True, "title": "{lead}'s Night in Vice City #{n}"},
    "after-dark": {"cats": None, "w": {"crash": 1.0, "chase": 0.9, "stunt": 0.8, "wow": 0.7, "rage": 0.5},
                   "days": 1, "title": "Vice City After Dark #{n} ({who})"},
    "banana-league": {"cats": None, "w": {"funny": 1.0, "rage": 0.6, "fail": 0.6, "wow": 0.5}, "days": 7, "reuse": True,
                      "league": True, "title": "Banana League Week #{n} - GTA 6 Streamer Tier List ({who})"},
    "same-mission": {"cats": None, "w": {"funny": 0.6, "wow": 0.6, "crash": 0.5, "mission": 0.5}, "days": 7,
                     "same_mission": True, "title": "Same Mission, {k} Streamers #{n} ({who})"},
    "first-reactions": {"cats": {"first-reaction", "wow"}, "w": {"first-reaction": 1.0, "wow": 0.7, "funny": 0.5},
                        "days": 0, "title": "Streamers' First Reactions to Vice City #{n} ({who})"},
    "rage": {"cats": {"rage"}, "w": {"rage": 1.0, "fail": 0.4}, "days": 7, "title": "GTA 6 Rage Moments #{n} ({who})"},
    "streamer-week": {"cats": None, "w": {"funny": 0.6, "wow": 0.6, "crash": 0.5}, "days": 7, "streamer": True,
                      "title": "{lead} in GTA 6 #{n} - Best Moments of the Week"},
    "physics": {"cats": {"physics", "glitch", "crash"}, "w": {"physics": 1.0, "glitch": 0.8, "crash": 0.6, "funny": 0.5},
                "verify": "physics", "days": 7, "title": "Streamers vs GTA 6 Physics #{n} ({who})"},
    "fails": {"cats": {"fail"}, "w": {"fail": 1.0, "funny": 0.5, "crash": 0.4}, "verify": "fail", "days": 7,
              "title": "GTA 6 Fails of the Week #{n} ({who})"},
    "missions": {"cats": {"mission"}, "w": {"mission": 1.0, "fail": 0.6, "funny": 0.4}, "verify": "mission", "days": 7,
                 "title": "Streamers vs GTA 6 Missions - Mission Fails #{n} ({who})"},
    "try-not-to-laugh": {"cats": {"funny"}, "w": {"funny": 1.0, "fail": 0.5}, "days": 7, "min_conf": 0.6,
                         "title": "Try Not to Laugh: GTA 6 Streamers #{n} ({who})"},
    "top-week": {"cats": None, "w": None, "days": 7, "reuse": True,
                 "title": {"bananas": "Top {k} Funniest GTA 6 Moments of the Week #{n} ({who})",
                           "fanpage": "Moments That Broke the Internet - Night #{n} ({who})"}},
    "chases": {"cats": {"chase"}, "w": {"chase": 1.0, "crash": 0.5, "escape": 0.5}, "verify": "chase", "days": 7,
               "title": "GTA 6 Craziest Police Chases #{n} ({who})"},
    "crashes-stunts": {"cats": {"crash", "stunt"}, "w": {"crash": 1.0, "stunt": 1.0, "physics": 0.4},
                       "verify": "crash", "days": 7, "title": "GTA 6 Craziest Crashes and Stunts #{n} ({who})"},
    "glitches": {"cats": {"glitch", "physics"}, "w": {"glitch": 1.0, "physics": 0.7}, "verify": "glitch", "days": 7,
                 "title": "GTA 6 Craziest Glitches #{n} ({who})"},
    "escapes": {"cats": {"escape"}, "w": {"escape": 1.0, "chase": 0.6}, "verify": "escape", "days": 7,
                "title": "GTA 6 Clutch Police Escapes #{n} ({who})"},
    "easter-eggs": {"cats": {"easter-egg"}, "w": {"easter-egg": 1.0, "wow": 0.4}, "verify": "easter-egg", "days": 7,
                    "title": "Easter Eggs and Secrets Streamers Found in GTA 6 #{n} ({who})"},
    "heists": {"cats": {"mission"}, "w": {"mission": 1.0, "chase": 0.4, "wow": 0.4}, "verify": "mission", "days": 7,
               "title": "GTA 6 Best Heists and Missions #{n} ({who})"},
    "streamer": {"cats": None, "w": {"funny": 0.5, "wow": 0.5}, "days": 7, "title": "{lead} GTA 6 Best Moments #{n}"},
}
DAILY = {"bananas": "funny-day", "fanpage": "wild-day"}
WD = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

def series_key(serie):
    return "streamer" if serie.startswith("streamer:") else serie

def calendar_series(z, day):
    """Series of the publish day from brands.json long_calendar (None = no long-form that day)."""
    cal = (K.load_json(K.BRANDS, {}).get(z) or {}).get("long_calendar") or {}
    if str(day) in (cal.get("special") or {}):
        return cal["special"][str(day)]
    for ph in cal.get("phases") or []:
        if ph.get("from", "0000") <= str(day) <= ph.get("to", "9999"):
            s = (ph.get("days") or {}).get(WD[day.weekday()])
            if isinstance(s, list):                        # weekly rotation
                s = s[day.isocalendar()[1] % len(s)]
            return s
    return main_series(z)

# ---------------------------------------------------------------------------
# state: episodes, reserve, promotions

def episode_next(z, serie, state=None):
    eps = state if state is not None else K.load_json(EPISODES, {})
    return (eps.get(z) or {}).get(series_key(serie), 0) + 1

def reserve_rows(z, serie, today, state=None):
    res = state if state is not None else K.load_json(RESERVE, {})
    return [r for r in ((res.get(z) or {}).get(series_key(serie)) or []) if r.get("until", "") >= str(today)]

def titles_texts(z, serie, n, d, moments):
    sk = series_key(serie)
    t = SERIES[sk]["title"]
    t = t[z] if isinstance(t, dict) else t
    who = ", ".join(names(moments))
    lead = names(moments, 1)[0] if moments else ""
    out = t.format(n=n, who=who, lead=lead, k=len({m["channel"] for m in moments}))
    while len(out) > 100 and "," in out:
        out = out.rsplit(",", 1)[0] + ")"
    return out[:100]

def window_for(serie, run_day, a):
    """Moments window for a run on run_day: daily series = yesterday, themed / weekly = last 7 days,
    first reactions = since the launch."""
    if a.od or a.do:
        od = date.fromisoformat(a.od or a.do)
        do = date.fromisoformat(a.do or a.od)
    else:
        days = SERIES[series_key(serie)]["days"]
        do = run_day - timedelta(days=1)
        od = LAUNCH if days == 0 else do - timedelta(days=max(days, 1) - 1)
    t0 = datetime(od.year, od.month, od.day, tzinfo=TZ).timestamp()
    t1 = datetime(do.year, do.month, do.day, tzinfo=TZ).timestamp() + 86400
    return od, do, t0, t1

def candidates(a, serie, t0, t1, used):
    rows = {r["id"]: dict(r) for r in K.pool_load().values()}
    game = a.hra or K.load_json(K.SOURCES, {}).get("twitch_game", "Grand Theft Auto VI")
    if all(K.helix_keys()) and a.pages:
        try:
            for c in K.game_clips(game, t0, t1, pages=a.pages):
                rows.setdefault(c["id"], c).update(views=max(c["views"], rows[c["id"]].get("views") or 0))
        except Exception as e:
            log(f"twitch api: {str(e)[-150:]}")
    game_rx = re.escape(a.hra) if a.hra else K.game_re()
    blocked = {b.lower() for b in K.load_json(K.SOURCES, {}).get("blocklist", [])}
    other = "fanpage" if a.znacka == "bananas" else "bananas"
    out = []
    for r in rows.values():
        if not (t0 <= (r.get("created") or 0) < t1) or r.get("channel", "").lower() in blocked:
            continue
        if not re.search(game_rx, r.get("category") or "", re.I):
            continue
        u = used.get(r["id"])
        if u and not (u["znacka"] == a.znacka and (u.get("reserve") and u["serie"] == series_key(serie)
                                                   or SERIES[series_key(serie)].get("reuse") and not u.get("reserve"))):
            continue                                       # the other brand's / another series' / already used
        if other in (r.get("rendered") or []) and a.znacka not in (r.get("rendered") or []):
            continue                                       # the other brand's short: keep the accounts apart
        if serie.startswith("streamer:") and r["channel"] != serie.split(":", 1)[1].lower():
            continue
        out.append(r)
    return out

def with_seg(r):
    """Row ready for the similarity checks: source, words, cut, facecam box."""
    r = ready_quiet(r)
    if r.get("source") and r.get("cut"):
        r.setdefault("seg", list(segment_times(r)))
        moment_words(r)
        r.setdefault("cam", K.load_json(K.STREAMERS, {}).get(r["channel"], {}).get("cam"))
    return r

def mission_groups(rows):
    """Same anchor (shared NPC lines) AND different outcomes, >= 3 streamers; canned scenes out (podobnost.py)."""
    import kategorie
    import podobnost
    rows = [with_seg(r) for r in rows]
    rows = [r for r in rows if r.get("seg")]
    return podobnost.refine_groups(kategorie.same_mission(rows), rows)

def drop_canned(good, why):
    """Entertainment filter: two streamers in the identical game scene (barber, shop, cutscene) = canned, out."""
    import kategorie
    import podobnost
    bad = set()
    for g in kategorie.same_mission(good, min_shared=2, min_streamers=2):
        ms = [r for r in good if r["id"] in g]
        for i, x in enumerate(ms):
            for y in ms[i + 1:]:
                if x["channel"] != y["channel"] and podobnost.canned(with_seg(x), with_seg(y))[0]:
                    bad |= {x["id"], y["id"]}
    if bad:
        why["canned scene"] = len(bad)
    return [r for r in good if r["id"] not in bad]

def ready_quiet(r):
    try:
        return ready(r) or r
    except Exception:
        return r

def classified(r):
    """Ready row with categories (kategorie.classify) and the HPC cut; None when unusable."""
    r = ready(r)
    if r is None:
        return None
    if r.get("cats") is None:
        import kategorie
        words = moment_words(r)
        a0, b0 = segment_times(r)
        r.update(kategorie.classify(words, r["source"], r.get("peak"), r.get("title"), r.get("created") or 0,
                                    r.get("views") or 0, a0, b0))
        K.pool_add({k: r.get(k) for k in K.POOL_KEYS} | {k: r.get(k) for k in ("cats", "primary", "proven", "evidence",
                                                                           "fun", "signals", "tags", "peak", "cut", "dur")},
                   replace=True)
    return r

def series_score(r, w):
    cats = r.get("cats") or {}
    match = sum(w.get(c, 0) * v for c, v in cats.items())
    return math.log10((r.get("views") or 0) + 10) * (0.5 + min(match, 1.5)) * (0.5 + (r.get("fun") or 0))

def qualifies(r, serie, z, verify=True, vis_cache=None):
    """
    Precision over quantity: gameplay (cheap static-UI pre-filter, then vision), entertainment floor,
    >= 2 independent signals for a topic, vision yes/no. ONE vision call per moment answers both
    "is this gameplay" and the topic question (saves neurons).
    """
    import kategorie
    sk = series_key(serie)
    cfg = SERIES[sk]
    static = (r.get("signals") or {}).get("static")
    if static is None and r.get("source"):
        a0, b0 = r.get("seg") or segment_times(r)
        static = kategorie.static_share(r["source"], a0, b0)
        r.setdefault("signals", {})["static"] = static
    if static is not None and static > kategorie.STATIC_MAX:
        return False, "not gameplay (static screen)"
    sig = r.setdefault("signals", {})
    if "dark" not in sig and r.get("source"):
        a0, b0 = r.get("seg") or segment_times(r)
        sig["luma"], sig["dark"] = kategorie.darkness(r["source"], a0, b0)
    if (sig.get("dark") or 0) > 0.5:
        return False, "too dark"
    if (r.get("fun") or 0) < FUN_MIN:
        return False, "boring"
    need = cfg.get("cats")
    if need:
        if not need & set(r.get("proven") or []):
            return False, "no 2-signal evidence"
        if cfg.get("min_conf") and max((r.get("cats") or {}).get(c, 0) for c in need) < cfg["min_conf"]:
            return False, "low confidence"
    if verify:
        q = cfg.get("verify") if need else None
        key = (r["id"], q)
        if vis_cache is not None and key in vis_cache:
            gp, tp = vis_cache[key]
        else:
            import nahled_ai
            jpg = K.POOL_DIR / r["id"] / "verify.jpg"
            jpg.parent.mkdir(parents=True, exist_ok=True)
            gp = tp = None
            if kategorie.verify_frames(r["source"], r.get("peak"), jpg, r.get("seg")):
                gp, tp = nahled_ai.ask_gameplay(jpg, kategorie.QUESTION.get(q) if q else None)
            if vis_cache is not None:
                vis_cache[key] = (gp, tp)
        if gp is False:
            return False, "vision: not gameplay"
        if tp is False:
            return False, "vision says no"
    return True, "ok"

def pick_episode(a, serie, cands, z, verify=True, vis_cache=None):
    """
    Verified, entertaining moments for one episode, best first, + the verified leftover (reserve).
    Every moment is cut (segment_times) so the length is known.
    """
    sk = series_key(serie)
    w = SERIES[sk]["w"] or SERIES[main_series(z)]["w"]
    lead = None
    if SERIES[sk].get("streamer"):                         # streamer of the week: the biggest with enough moments
        per = {}
        for r in cands:
            per.setdefault(r["channel"], []).append(r)
        if per:
            try:
                import liga
                board = [r["channel"] for r in liga.table(7)]
            except Exception:
                board = []
            ranked_ch = [ch for ch in board if ch in per and len(per[ch]) >= 8]
            lead = ranked_ch[0] if ranked_ch else max(
                per, key=lambda ch: (len(per[ch]) >= 8, sum(x.get("views") or 0 for x in per[ch])))
            cands = per[lead]
    if SERIES[sk].get("night"):
        per = {}
        for r in cands:
            per.setdefault(r["channel"], []).append(r)
        if per:
            lead = max(per, key=lambda ch: (len(per[ch]), sum(x.get("views") or 0 for x in per[ch])))
            cands = per[lead]
    if SERIES[sk].get("same_mission"):
        import kategorie
        groups = mission_groups(cands)
        cands = [r for r in cands if groups and r["id"] in groups[0]]
    cands = sorted(cands, key=lambda r: (r.get("views") or 0), reverse=True)[: a.max_kandidatu]
    good, why = [], {}
    for r in cands:
        try:
            r = classified(r)
        except K.NotGameplay as e:
            why[str(e)[:30]] = why.get(str(e)[:30], 0) + 1
            continue
        except Exception as e:
            log(f"  skip {r['id']}: {str(e)[-120:]}")
            continue
        if r is None:
            continue
        ok, reason = qualifies(r, serie, z, False, vis_cache)    # cheap checks; vision below while picking
        if not ok:
            why[reason] = why.get(reason, 0) + 1
            continue
        s, e = segment_times(r)
        if e - s < 9.0:                                    # source too short for a >= 10 s chapter
            why["too short"] = why.get("too short", 0) + 1
            continue
        r.update(seg=[s, e], score=round(series_score(r, w), 3))
        good.append(r)
    good = drop_canned(good, why)
    good.sort(key=lambda r: -r["score"])
    per_max = 99 if (serie.startswith("streamer:") or lead or SERIES[sk].get("night")) else a.max_na_streamera
    picked, rest, per, total = [], [], {}, 0.0
    themed = bool(SERIES[sk].get("cats"))
    for r in good:
        full = len(picked) >= a.max_momentu or (total >= a.delka and len(picked) >= a.min_momentu)
        if verify and (not full or themed):                # main series: leftovers are not used, no vision for them
            ok, reason = qualifies(r, serie, z, True, vis_cache)
            if not ok:
                why[reason] = why.get(reason, 0) + 1
                continue
        if full or per.get(r["channel"], 0) >= per_max:
            rest.append(r)
            continue
        picked.append(r)
        per[r["channel"]] = per.get(r["channel"], 0) + 1
        total += r["seg"][1] - r["seg"][0] + CHAPTER_EXTRA[z]
    return picked, rest, why, lead

def video_len(picked, z):
    """Estimated video length: cold open + moments (+ score cards) + end slate."""
    return 11.0 + (SWEEP if z == "fanpage" else 0) + sum(r["seg"][1] - r["seg"][0] + CHAPTER_EXTRA[z] for r in picked) + 8.0

def episode_ok(picked, z):
    """A part must stand on its own: >= 5 min, a strong #1 and cold open (Aleš: no weak part 2)."""
    if not picked or video_len(picked, z) < MIN_VIDEO:
        return False
    if story_mode(z):                                      # a story has no #1 to carry: length + every moment fun
        return True
    top3 = sorted(picked, key=lambda r: -r["score"])[:3]
    return (top3[0].get("fun") or 0) >= 0.5 and sum(r.get("fun") or 0 for r in top3) / len(top3) >= 0.45

def plan_day(a, z, publish_day, run_day, used, eps, res, vis_cache=None, verify=True):
    """
    The series for the publish day (calendar, or a promotion), its moments, the fallback to the main
    series when the topic is thin. Returns a plan dict (no files written).
    """
    promo = (res.get("_promo") or {}).get(z, {}).get(str(publish_day))
    serie = a.serie or promo or calendar_series(z, publish_day)
    plan = {"day": str(publish_day), "serie": serie, "planned": serie, "note": ""}
    if not serie:
        plan.update(serie=None, note="podle kalendáře dnes long-form není")
        return plan
    if promo and not a.serie:
        plan["note"] = f"Navíc díl série {promo} (má materiál na 2+ díly, její další termín je za víc než 3 dny)."
    cal = (K.load_json(K.BRANDS, {}).get(z) or {}).get("long_calendar") or {}
    if (not a.serie and not promo and flag(z, "multi_pov")
            and cal.get("same_mission_day") == WD[publish_day.weekday()]):
        try:                                               # multi-POV of one mission replaces the rotating theme
            import kategorie
            od, do, t0, t1 = window_for("same-mission", run_day, a)
            if mission_groups(candidates(a, "same-mission", t0, t1, used)):
                plan["note"] = f"Místo tématu {serie}: stejná mise od více streamerů."
                serie = plan["serie"] = "same-mission"
        except Exception as e:
            log(f"  same mission: {str(e)[-120:]}")
    tried = []
    for s in [serie] + ([main_series(z)] if serie != main_series(z) else []):
        od, do, t0, t1 = window_for(s, run_day, a)
        reserve_ids = {r["id"] for r in reserve_rows(z, s, run_day, res)}
        cands = candidates(a, s, t0, t1, used)
        # the reserve of this series goes first (it is already verified), then the new ones
        cands.sort(key=lambda r: (r["id"] not in reserve_ids,))
        picked, rest, why, lead = pick_episode(a, s, cands, z, verify, vis_cache)
        need = MIN_VERIFIED if SERIES[series_key(s)].get("cats") else a.min_momentu_hlavni
        enough = len(picked) >= min(need, a.min_momentu) and episode_ok(picked, z)
        tried.append((s, len(picked), round(video_len(picked, z)), why))
        if enough:
            n = episode_next(z, s, eps)
            plan.update(serie=s, od=str(od), do=str(do), picked=picked, rest=rest, episode=n, lead=lead,
                        length=round(video_len(picked, z)))
            if s != serie:
                plan["note"] = (plan["note"] + " " if plan["note"] else "") + \
                    f"Téma {serie} nemělo dost momentů ({tried[0][1]}) → vyšlo {s}."
            return plan
    plan.update(serie=None, note=f"Žádné video: téma {serie} ani hlavní série nemají 5 min materiálu "
                                 f"({'; '.join(f'{s}: {n} momentů, {l} s, vyřazeno {w}' for s, n, l, w in tried)}).", tried=tried)
    return plan

def commit_plan(a, z, plan, used, eps, res, run_day):
    """After a built (or simulated) episode: number up, used moments, reserve, maybe a promotion."""
    s = series_key(plan["serie"])
    eps.setdefault(z, {})[s] = plan["episode"]
    for r in plan["picked"]:
        used[r["id"]] = {"znacka": z, "serie": s, "datum": plan["day"]}
    until = str(run_day + timedelta(days=7))
    themed = bool(SERIES[s].get("cats"))                  # main / weekly series: leftovers stay free for others
    keep = [{"id": r["id"], "until": until, "score": r["score"]} for r in plan["rest"]] if themed else []
    old = ([x for x in reserve_rows(z, s, run_day, res) if x["id"] not in {r["id"] for r in plan["picked"]}]
           if themed else [])
    merged = {x["id"]: x for x in old + keep}
    res.setdefault(z, {})[s] = list(merged.values())
    for x in merged.values():                             # nobody else takes a reserved moment
        used.setdefault(x["id"], {"znacka": z, "serie": s, "datum": plan["day"], "reserve": True})
    # material for 2+ more episodes and the series' next slot > 3 days away: an extra episode in the
    # next calendar slot of this channel (replacing that day's series)
    if len(merged) >= 2 * a.min_momentu and plan["serie"] != main_series(z):
        day = date.fromisoformat(plan["day"])
        nxt = next((day + timedelta(days=i) for i in range(1, 15) if calendar_series(z, day + timedelta(days=i)) == s), None)
        free = next((day + timedelta(days=i) for i in range(1, 8) if calendar_series(z, day + timedelta(days=i))), None)
        if free and (nxt is None or (nxt - day).days > 3) and calendar_series(z, free) not in ("top-week", "first-reactions"):
            res.setdefault("_promo", {}).setdefault(z, {})[str(free)] = s
    cutoff = str(run_day - timedelta(days=30))
    for k in [k for k, v in used.items() if v.get("datum", "") < cutoff]:
        used.pop(k)


def log(msg):
    K.log(msg)

def run(cmd, timeout=1800):
    return K.run(cmd, timeout=timeout)

# -----------------------------------------------------------------------------
# texts that go out (English; SlopMonster lint 5/5 on the templates)

def day_n(d):
    return (d - LAUNCH).days + 1

_reach = {}

def reach(moments):
    """
    How big each streamer is (Aleš 17. 11.: the thumbnail and the title lead with the most viral
    streamer): Twitch followers via the API (app token gives the total), else the sum of the views
    of his moments in this video. Sets m["reach"] on every moment.
    """
    logins = sorted({m["channel"] for m in moments if m.get("platform", "twitch") == "twitch"} - set(_reach))
    if logins and all(K.helix_keys()):
        try:
            users = K.helix("users", [("login", l) for l in logins[:100]]).get("data") or []
            for u in users:
                tot = K.helix("channels/followers", {"broadcaster_id": u["id"], "first": 1}).get("total")
                if tot is not None:
                    _reach[u["login"].lower()] = int(tot)
        except Exception as e:
            log(f"  reach: {str(e)[-120:]}")
    views = {}
    for m in moments:
        views[m["channel"]] = views.get(m["channel"], 0) + (m.get("views") or 0)
    for m in moments:
        m["reach"] = _reach.get(m["channel"], views[m["channel"]])
    return max(moments, key=lambda m: m["reach"])["channel"] if moments else None

def names(moments, n=3):
    seen = []
    lead = max(moments, key=lambda m: m.get("reach") or 0) if moments else None
    if lead and lead.get("reach"):                          # the most viral streamer first
        seen.append(lead.get("name") or lead["channel"])
    for m in sorted(moments, key=lambda m: m["rank"]):
        nm = m.get("name") or m["channel"]
        if nm not in seen:
            seen.append(nm)
    return seen[:n]

def title_for(z, serie, d, moments):
    who = ", ".join(names(moments))
    day = f"Day {day_n(d)}" if d >= LAUNCH else d.strftime("%b %d").replace(" 0", " ")
    if serie == "funny-day":
        t = f"GTA 6 Funniest Moments - {day} ({who})"
    elif serie == "wild-day":
        t = f"GTA 6 Wildest Streamer Moments - {day} ({who})"
    elif serie == "glitches":
        t = f"GTA 6 Craziest Glitches and Bugs ({who})"
    elif serie == "chases":
        t = f"GTA 6 Best Car Chases and Crashes ({who})"
    elif serie == "top-week":
        t = f"Top {len(moments)} {'Funniest' if z == 'bananas' else 'Wildest'} GTA 6 Moments This Week ({who})"
    else:
        t = f"{(moments[0].get('name') or moments[0]['channel'])} GTA 6 Best Moments"
    while len(t) > 100 and "," in t:                  # YouTube title limit: drop names from the end
        t = t.rsplit(",", 1)[0] + ")"
    return t[:100]

INTRO = {
    "funny-day": "The funniest GTA 6 streamer moments of {day}, counted down to number one.",
    "wild-day": "The wildest GTA 6 streamer moments of {day}, from small wrecks to full chaos, counted down.",
    "glitches": "GTA 6 glitches and bugs streamers ran into, from weird to completely broken.",
    "chases": "GTA 6 police chases and crashes from streamers, counted down to the craziest one.",
    "top-week": "The best GTA 6 streamer moments of the week, counted down from #{n} to #1.",
    "streamer": "The best GTA 6 moments of {name}, counted down to number one.",
    "streamer-week": "The best GTA 6 moments of {name} this week, counted down to number one.",
    "first-reactions": "Streamers seeing Vice City for the first time, counted down to the best reaction.",
    "rage": "GTA 6 streamers losing it, counted down to the biggest meltdown.",
    "physics": "GTA 6 physics doing things nobody asked for, counted down.",
    "fails": "The biggest GTA 6 fails of the week, counted down to number one.",
    "missions": "GTA 6 missions going wrong for streamers, counted down.",
    "try-not-to-laugh": "Try not to laugh: GTA 6 streamer moments, counted down to the best one.",
    "crashes-stunts": "GTA 6 crashes and stunts from streamers, counted down to the craziest one.",
    "escapes": "Streamers getting away from the police in GTA 6, counted down to the cleanest escape.",
    "easter-eggs": "Easter eggs and secrets streamers found in GTA 6, counted down.",
    "heists": "The best GTA 6 heists and missions from streamers, counted down.",
    "streamer-night": "One night in Vice City with {name}, from the first moment to the wildest one.",
    "after-dark": "Vice City after dark: the night's chaos from GTA 6 streamers, in the order it happened.",
}
INTRO_OTHER = "The best GTA 6 streamer moments, counted down to number one."

def fmt_t(s):
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"

def description(z, serie, d, moments, chapters):
    key = "streamer" if serie.startswith("streamer:") else serie
    day = f"day {day_n(d)}" if d >= LAUNCH else d.strftime("%B %d").replace(" 0", " ")
    intro = INTRO.get(key, INTRO_OTHER).format(day=day, n=len(moments), name=names(moments, 1)[0],
                                               topic=SERIES[key]["title"] if isinstance(SERIES[key]["title"], str) else "")
    lines = [intro, "", "Chapters:"] + [f"{fmt_t(t)} {label}" for t, label in chapters]
    seen, credits = set(), []
    for m in sorted(moments, key=lambda m: m["rank"]):
        if m["channel"] not in seen:
            seen.add(m["channel"])
            credits.append(f"{m.get('name') or m['channel']}: {K.home_url(m)}")
    lines += ["", "Streamers in this video (go follow them):"] + credits
    lines += ["", "Every clip belongs to its streamer and was clipped by viewers. "
              "If you are in it and want it out, comment and we cut it.", ""]
    tag = "#gta6bananaclips" if z == "bananas" else "#gta6fanpageclip"
    lines.append(f"#gta6 #gtavi #gta6clips {tag}")
    return "\n".join(lines)

def yt_tags(z, serie, moments):
    base = ["gta 6", "gta vi", "gta 6 gameplay", "gta 6 streamers", "gta 6 moments", "vice city"]
    per = {"funny-day": ["gta 6 funny moments", "gta 6 funny"], "wild-day": ["gta 6 crazy moments", "gta 6 chaos"],
           "glitches": ["gta 6 glitches", "gta 6 bugs"], "chases": ["gta 6 police chase", "gta 6 car crash"],
           "top-week": ["gta 6 best moments", "gta 6 top moments"]}.get(serie, ["gta 6 best moments"])
    return per + base + [n.lower() for n in names(moments, 8)] + [f"{n.lower()} gta 6" for n in names(moments, 3)]

def suggested_slot(z, serie, d, now=None):
    """
    When to publish (Prague). Aleš 17. 11.: the e-mail comes a day ahead, so the slot is always
    TOMORROW (day after the run), US afternoon = Prague evening; each brand its own time, moved
    -20..+20 min per day, never :00 / :30 (like shorts). Weekly a bit earlier than the daily.
    """
    now = now or datetime.now(TZ)
    hh, mm = {("bananas", 0): (20, 40), ("fanpage", 0): (22, 10),
              ("bananas", 1): (19, 40), ("fanpage", 1): (21, 10)}[(z, int(serie == "top-week"))]
    base_day = now.date() + timedelta(days=1)
    t = datetime(base_day.year, base_day.month, base_day.day, hh, mm, tzinfo=TZ)
    rnd = random.Random(f"{d}:{z}:{serie}:long")
    t += timedelta(minutes=rnd.randint(-20, 20))
    while t.minute in (0, 30):
        t += timedelta(minutes=1)
    return t.replace(second=0, microsecond=0)

# -----------------------------------------------------------------------------
# selection

def ready(r):
    """Source on disk + payoff/cut/tags; download / collect what is missing. Returns the row or None."""
    src = Path(r.get("source") or "")
    if r.get("tags") is not None and r.get("cut") and src.exists():
        return r
    local = K.POOL_DIR / r["id"] / "source.mp4"
    if r.get("tags") is not None and r.get("cut"):
        if not local.exists():
            log(f"  download {r['url']}")
            local.parent.mkdir(parents=True, exist_ok=True)
            K.download(r, local)
        return {**r, "source": str(local)}
    return K.collect(r)

def segment_times(r):
    """HPC cut on the 16:9 source: start in the action (<= 14 s before the payoff), end ~3 s after."""
    dur = r.get("dur") or K.duration(r["source"])
    a, b = r["cut"]
    p = r.get("peak")
    if p is None:
        s, e = a, b
    else:
        s = max(a, p - 14.0)
        e = min(dur, max(p + 2.5, min(b, p + 4.0)))
    if e - s < 10.2:                                       # chapters need >= 10 s
        s = max(0.0, e - 10.2)
        e = min(dur, s + 10.2)
    return round(s, 2), round(e, 2)

# -----------------------------------------------------------------------------
# render

def encoder():
    """One encoder for every segment (concat without re-encoding needs identical streams)."""
    try:
        run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=black:s=256x144:d=0.2",
             "-c:v", "h264_nvenc", "-f", "null", "-"], timeout=60)
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0", "-maxrate", "12M",
                "-bufsize", "24M", "-profile:v", "high"]
    except RuntimeError:
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-maxrate", "12M", "-bufsize", "24M",
                "-profile:v", "high"]

AOUT = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
VOUT = ["-r", str(FPS), "-pix_fmt", "yuv420p", "-video_track_timescale", "90000"]

def text_file(tmp, name, text):
    p = tmp / f"{name}.txt"
    p.write_text(text, encoding="utf-8")
    return K.ff_path(p)

def overlays(tmp, z, m, rank, idx):
    """Corner texts: @channel credit (bottom left), brand watermark (top right), countdown number."""
    wm = "gta6bananaclips" if z == "bananas" else "gta6fanpageclip"
    f = [f"drawtext=fontfile='{K.ff_path(FONT_M)}':textfile='{text_file(tmp, f'c{idx}', '@' + m['channel'])}':"
         f"fontsize=34:fontcolor=white@0.92:box=1:boxcolor=black@0.45:boxborderw=10:x=34:y=h-66",
         f"drawtext=fontfile='{K.ff_path(FONT_M)}':textfile='{text_file(tmp, f'w{idx}', wm)}':"
         f"fontsize=30:fontcolor=0x{COL[z]}@0.9:box=1:boxcolor=black@0.45:boxborderw=9:x=w-tw-34:y=30"]
    if rank:
        f.append(f"drawtext=fontfile='{K.ff_path(FONT_B)}':textfile='{text_file(tmp, f'r{idx}', f'#{rank}')}':"
                 f"fontsize=72:fontcolor=0x{COL[z]}:borderw=6:bordercolor=black:x=34:y=24")
    return ",".join(f)

SFX = HERE / "assets" / "sfx"
CAP16 = {"bananas": ("Montserrat Black", 78, "FFE135", 5, 3), "fanpage": ("Anton", 94, "FF4FD8", 4, 0)}
TEASER = "#1 is insane, stay till the end"
MUSIC_VOL = {"bananas": 0.06, "fanpage": 0.05}

def moment_words(m):
    """Word times of the moment's source: from the pool cache, else Whisper once (rendered shorts)."""
    if m.get("words") is None:
        w = K.load_json(K.POOL_DIR / m["id"] / "meta.json", {}).get("words")
        if w is None and K.has_audio(m["source"]):
            w = K.transcribe(m["source"])
        m["words"] = [tuple(x) for x in (w or [])]
    return m["words"]

def key_words(words, src, peak, a, b):
    """
    Captions only for what matters (Aleš: not wall-to-wall): the lines around the payoff and
    shouted passages (word loudness > 1.8x the moment's median). Radio lyrics are already gone
    (klipuj.transcribe drops sung / doubtful segments).
    """
    ts, rms = K.rms_curve(src)
    med = float(np.median(rms[(ts >= a) & (ts <= b)])) if rms.size and ((ts >= a) & (ts <= b)).any() else 0.0
    out = []
    for w in words:
        if not (a <= w[0] and w[1] <= b):
            continue
        near = peak is not None and peak - 4.5 <= w[0] <= peak + 2.5
        loud = False
        if med > 0:
            mm = (ts >= w[0]) & (ts <= w[1])
            loud = mm.any() and float(rms[mm].mean()) > 1.8 * med
        if near or loud:
            out.append(w)
    return out

def ass16(words, path, z, y=905):
    """Captions for the 16:9 frame in the brand's style: 2 words, the spoken one highlighted, bottom centre."""
    font, size, hl, outline, shadow = CAP16[z]
    hi = K.ass_col(hl)
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\nWrapStyle: 2\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
            "MarginR, MarginV, Encoding\n"
            f"Style: Cap,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,0,0,0,0,100,100,1,0,1,"
            f"{outline},{shadow},2,40,40,0,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
            "MarginV, Effect, Text\n")
    chunks, cur = [], []
    for w in words:
        if cur and (len(cur) == 2 or w[0] - cur[-1][1] > 0.6):
            chunks.append(cur)
            cur = []
        cur.append(w)
        if w[2][-1:] in ".?!":
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    clean = lambda t: t.upper().replace("\\", "").replace("{", "(").replace("}", ")")
    lines = []
    for ci, ch in enumerate(chunks):
        end_chunk = min(ch[-1][1] + 0.5, chunks[ci + 1][0][0] if ci + 1 < len(chunks) else 1e9)
        for i, (s0, e0, _) in enumerate(ch):
            end = ch[i + 1][0] if i + 1 < len(ch) else end_chunk
            txt = " ".join(("{\\c&H%s&\\fscx90\\fscy90\\t(0,110,\\fscx106\\fscy106)}%s{\\r}" % (hi, clean(t)))
                           if j == i else clean(t) for j, (_, _, t) in enumerate(ch))
            lines.append(f"Dialogue: 0,{K.ass_time(s0)},{K.ass_time(max(end, s0 + 0.05))},Cap,,0,0,0,,"
                         f"{{\\blur1\\pos(960,{y})}}{txt}")
    path.write_text(head + "\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)

def zoom_marks(words, jumps, length, peak):
    """
    Zoom expression: 1.0 / 1.06 switched on jump cuts, new sentences and at least every ~4 s
    (the frame never stands still), and the payoff punch 1.12 for 0.6 s.
    """
    marks = set(round(j, 2) for j in jumps)
    prev = None
    for w0, w1, t in words:
        if prev is None or w0 - prev[1] > 0.6 or prev[2][-1:] in ".?!":
            marks.add(round(w0, 2))
        prev = (w0, w1, t)
    out, last = [], 0.0
    for t in sorted(marks) + [length]:
        while t - last > 4.0:                      # nothing happens for 4 s: switch anyway
            last += 3.5
            out.append(round(last, 2))
        if t - last >= 1.5 and 0.5 < t < length - 0.3:
            out.append(t)
            last = t
    edges = [0.0] + sorted(set(out)) + [length]
    on = [(edges[i], edges[i + 1]) for i in range(1, len(edges) - 1, 2)]
    base = "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in on) or "0"
    z = f"(1+0.06*({base}))"
    if peak is not None:
        z = f"if(between(t,{peak:.2f},{peak + 0.6:.2f}),1.12,{z})"
    return z

def story_grade(m, src, a, b):
    """
    Adaptive story look: normally lit footage gets the cinematic grade (teal/orange + vignette); dim
    footage (night, interiors) is NOT darkened further - its shadows are lifted instead.
    """
    import kategorie
    luma = (m.get("signals") or {}).get("luma")
    if luma is None:
        luma, _ = kategorie.darkness(src, a, b)
    if luma is not None and luma < kategorie.DIM_LUMA:
        return f"eq=gamma={kategorie.LIFT_GAMMA}:brightness=0.03:saturation=1.1:contrast=1.04"
    return K.FAN_LOOK

def render_moment(m, out, z, rank, idx, tmp, venc, teaser=False, banana=False, card_on=False, intro_extra="",
                  story=False, cut_to=None, badge=None):
    """
    One moment of the countdown, built like the shorts so it never sits still (Aleš 17. 11.):
      cut      start in the action, jump cuts over dead air (klipuj.dead_air), <= ~25 s
      picture  zoom 1.0/1.06 on sentences or every ~4 s, payoff punch 1.12 + flash, captions of the
               key lines in the brand style, 0.6 s intro card "#N @streamer", corner credit/badge,
               teaser "#1 is insane" over the end of #6 and #3, bananas: banana reaction (top 5) and
               the small score card after the payoff; fanpage: pink flash
      sound    loudness-normalised stream audio, swears bleeped, whoosh on the card, riser 1.2 s
               before the payoff, hit on it (our music comes later, under the whole video, ducked)
    Returns the length (s).
    """
    s, e = m["seg"]
    src = m["source"]
    if not K.has_audio(src):
        render_silent(m, out, z, rank, idx, tmp, venc)
        return K.duration(out)
    dur_src = K.duration(src)
    words = moment_words(m)
    peak = m.get("peak")
    peak = peak if peak is not None and s < peak < e else None
    keep = K.dead_air(words, src, s, e, peak) if words else [(s, e)]
    if sum(y - x for x, y in keep) < 9.0:                 # chapters need >= 10 s (moment + tier flight)
        keep = [(s, e)]
    card_file = K.RATING / f"score_{K.banana_rating(m.get('views'))}.webm"
    card = card_on and z == "bananas" and card_file.exists()
    tail = min(CARD, max(0.0, dur_src - e)) if card else 0.0
    hold = (CARD - tail) if card else 0.0                 # source ends: hold the last frame under the card
    keep[-1] = (keep[-1][0], e + tail)
    starts, acc = [], 0.0
    for x, y in keep:
        starts.append(acc)
        acc += y - x
    length = acc + hold
    if cut_to and cut_to < length:                         # story After Dark: end exactly on a beat
        length = cut_to
    remap = lambda t: next((st + t - x for (x, y), st in zip(keep, starts) if x <= t <= y), None)
    main_end = remap(e) if remap(e) is not None else acc - tail
    pk = remap(peak) if peak is not None else None
    jumps = starts[1:]
    rw = [(remap(w[0]), remap(w[1]), w[2]) for w in words if remap(w[0]) is not None and remap(w[1]) is not None]
    cap_words = []
    for w in key_words(words, src, peak, s, e):
        a0, b0 = remap(w[0]), remap(w[1])
        if a0 is not None and b0 is not None:
            cap_words.append((a0, b0, w[2]))
    cap_words = [w for w in cap_words if w[0] >= (0.0 if story else 0.65) and w[1] <= length - 0.2]   # not under the intro card
    cap_words, mute = K.mask_swears(cap_words)
    for w in words:                                       # bleep every hard swear, captioned or not
        a0, b0 = remap(w[0]), remap(w[1])
        if a0 is not None and b0 is not None and K.SWEAR_RE.match(K._tok(w[2])) and (a0, b0) not in mute:
            mute.append((a0, b0))
    ass = tmp / f"cap{idx}.ass"
    cam = K.load_json(K.STREAMERS, {}).get(m["channel"], {}).get("cam")
    cpx = [v / 100 * d for v, d in zip(cam, (W, H, W, H))] if cam else None
    hits = lambda x0, y0, x1, y1: cpx and not (cpx[0] + cpx[2] < x0 or cpx[0] > x1 or cpx[1] + cpx[3] < y0 or cpx[1] > y1)
    cap_y = 160 if hits(420, 820, 1500, 980) else 905       # never over the facecam
    ass16(cap_words, ass, z, cap_y)

    norm = K.src_norm(src).replace(f"scale={K.SW}:{K.SH}:force_original_aspect_ratio=increase,crop={K.SW}:{K.SH}",
                                   f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2")
    t0 = keep[0][0]
    ins = ["-ss", f"{t0:.2f}", "-t", f"{keep[-1][1] - t0:.2f}", "-i", str(src)]
    g = []
    for i, (x, y) in enumerate(keep):
        g.append(f"[0:v]trim={x - t0:.3f}:{y - t0:.3f},setpts=PTS-STARTPTS,{norm},fps={FPS},setsar=1[v{i}]")
        g.append(f"[0:a]atrim={x - t0:.3f}:{y - t0:.3f},asetpts=PTS-STARTPTS,aresample=48000,"
                 f"aformat=channel_layouts=stereo[a{i}]")
    g.append("".join(f"[v{i}][a{i}]" for i in range(len(keep))) + f"concat=n={len(keep)}:v=1:a=1[vc][ac]")
    zexpr = zoom_marks(rw, jumps, main_end, pk)
    flash_col = "white@0.55" if z == "bananas" else f"0x{COL[z]}@0.5"
    v = (f"[vc]" + (f"tpad=stop_mode=clone:stop_duration={hold:.2f}," if hold else "")
         + f"scale=w='{W}*{zexpr}':h=-2:eval=frame,crop={W}:{H}:(iw-ow)/2:(ih-oh)/2,setsar=1"
         + (f",drawbox=enable='between(t,{pk:.2f},{pk + 0.12:.2f})':color={flash_col}:t=fill" if pk is not None else "")
         + f",subtitles=filename='{K.ff_path(ass)}':fontsdir='{K.ff_path(K.CAP_FONTS)}'"
         + (f",{story_grade(m, src, s, e)}" if story else "")
         + (f",drawbox=x=0:y=0:w=iw:h={LETTERBOX}:color=black:t=fill:enable='lt(t,0.45)+gt(t,{length - 0.45:.2f})'"
            f",drawbox=x=0:y=ih-{LETTERBOX}:w=iw:h={LETTERBOX}:color=black:t=fill:enable='lt(t,0.45)+gt(t,{length - 0.45:.2f})'"
            if story else "")
         + f",{overlays(tmp, z, m, None if story else rank, idx)}"
         + (f",drawtext=fontfile='{K.ff_path(FONT_B)}':textfile='{text_file(tmp, f'bd{idx}', badge)}':fontsize=46:"
            f"fontcolor=0x8BE04E:borderw=3:bordercolor=0x1E4A12:box=1:boxcolor=0x2E313B:boxborderw=16:"
            f"x=w-tw-46:y=h-th-120" if badge else "")
         + ("" if story else
            f",drawtext=fontfile='{K.ff_path(FONT_B)}':textfile='{text_file(tmp, f'i{idx}', f'#{rank}   @' + m['channel'] + intro_extra)}':"
            f"fontsize=88:fontcolor=black:box=1:boxcolor=0x{COL[z]}:boxborderw=26:x=(w-tw)/2:"
            f"y={'h*0.55' if hits(520, 280, 1400, 460) else 'h*0.3'}:"
            f"enable='lt(t,0.6)':alpha='if(lt(t,0.45),1,(0.6-t)/0.15)'"))
    if teaser:
        v += (f",drawtext=fontfile='{K.ff_path(FONT_B)}':textfile='{text_file(tmp, f't{idx}', TEASER.upper())}':"
              f"fontsize=54:fontcolor=white:box=1:boxcolor=black@0.62:boxborderw=18:x=(w-tw)/2:y=150:"
              f"enable='between(t,{max(0.8, main_end - 3.0):.2f},{length:.2f})'")
    g.append(v + "[v0]")
    vlast, n, mixes = "[v0]", 1, []
    a = "[ac]loudnorm=I=-16:TP=-1.5:LRA=11" + (f",apad=whole_dur={length:.2f}" if hold else "")
    for a0, b0 in mute:
        a += f",volume=enable='between(t,{a0:.2f},{b0:.2f})':volume=0"
    g.append(a + "[a0]")
    mixes.append("[a0]")
    if mute:                                              # 1 kHz bleep over the swears (same as shorts)
        ins += ["-f", "lavfi", "-t", f"{length:.2f}", "-i", "sine=f=1000:r=48000"]
        on = "+".join(f"between(t,{a0:.2f},{b0:.2f})" for a0, b0 in mute)
        g.append(f"[{n}:a]volume=0.18,volume=enable='not({on})':volume=0,aformat=channel_layouts=stereo[bp]")
        mixes.append("[bp]")
        n += 1

    def sfx(name, at, vol):
        nonlocal n
        f = SFX / name
        if not f.exists() or at is None or at < 0 or at >= length:
            return
        ins.extend(["-i", str(f)])
        g.append(f"[{n}:a]aresample=48000,aformat=channel_layouts=stereo,volume={vol},adelay={int(at * 1000)}:all=1[s{n}]")
        mixes.append(f"[s{n}]")
        n += 1

    if not story:
        sfx("flash.wav", 0.0, 0.3)                        # whoosh on the intro card
    if pk is not None:
        sfx("riser.wav", pk - 1.2, 0.45)
        sfx("hit.wav", pk, 0.55)
    if banana and pk is not None:
        rx = K.pick_reactions(rw, main_end, m["id"], pk)[:1]
        for t, name in rx:
            vv, rs, re_ = K.REACTIONS[name]
            ins.extend(["-ss", f"{rs}", "-t", f"{re_ - rs}", "-i", str(K.BANANA / f"reakce{vv}.mp4")])
            g.append(f"[{n}:v]setpts=PTS-STARTPTS+{t:.2f}/TB,chromakey={K.BANANA_KEY}:0.16:0.06,scale=240:428,"
                     f"format=yuva420p[ban];{vlast}[ban]overlay=x={'W-w-40' if hits(0, 500, 400, 1080) else '40'}:y=H-h-100:"
                     f"eof_action=pass[vb]")
            vlast, n = "[vb]", n + 1
    if card:
        ins.extend(["-c:v", "libvpx-vp9", "-i", str(card_file)])
        g.append(f"[{n}:v]scale=-2:560,setpts=PTS+{main_end:.2f}/TB[cv];{vlast}[cv]overlay=x=W-w-40:y=H-h-40:"
                 f"eof_action=pass[vk]")
        g.append(f"[{n}:a]adelay={int(main_end * 1000)}:all=1,volume=0.45[ca]")
        vlast, n = "[vk]", n + 1
        mixes.append("[ca]")
    g.append(f"{''.join(mixes)}amix=inputs={len(mixes)}:duration=first:normalize=0,alimiter=limit=0.95,"
             f"afade=t=out:st={max(0.0, length - 0.15):.2f}:d=0.15[aout]")
    run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error"] + ins +
        ["-filter_complex", ";".join(g), "-map", vlast, "-map", "[aout]", "-t", f"{length:.2f}"] + VOUT + venc + AOUT
        + [str(out)])
    return length

def music_pass(video, out, z, music, mstart, louder, vol=None):
    """
    Our music under the WHOLE video, quietly (bananas 0.06, fanpage 0.05), ducked under speech
    (sidechaincompress keyed by the stream audio), 3x louder on transitions (`louder` = (start, end)).
    Video is copied, only the audio is mixed again.
    """
    if not music:
        Path(video).replace(out)
        return
    vol = vol or MUSIC_VOL[z]
    boost = "+".join(f"2*between(t,{a:.2f},{b:.2f})" for a, b in louder) or "0"
    total = K.duration(video)
    g = (f"[1:a]aresample=48000,aformat=channel_layouts=stereo,aloop=loop=-1:size=2147483647,atrim=0:{total:.2f},"
         f"volume='{vol}*(1+{boost})':eval=frame[mu];"
         "[0:a]asplit=2[sa][sc];"
         "[mu][sc]sidechaincompress=threshold=0.02:ratio=6:attack=15:release=350[duck];"
         f"[sa][duck]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[a]")
    run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", str(video),
         "-ss", f"{mstart:.2f}", "-i", str(music), "-filter_complex", g,
         "-map", "0:v", "-map", "[a]", "-c:v", "copy"] + AOUT + ["-movflags", "+faststart", str(out)])
    Path(video).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# tier list board (Aleš 17. 11.): S/A/B/C/D fills up during the video. Data only: the moment's own
# payoff frame and @streamer fly into the row their score earns. Empty board after the cold open
# ("where does #1 go"), the full board before the end card.

TIERS = ["S", "A", "B", "C", "D"]
TIER_SHARE = [0.10, 0.20, 0.30, 0.25, 1.0]          # S = top 10 %, A next 20 %, B 30 %, C 25 %, D the rest
TIER_COL = {"bananas": {"S": "FF3B3B", "A": "FF9F1C", "B": "FFE135", "C": "9BE15D", "D": "5DADE2"},
            "fanpage": {"S": "FF4FD8", "A": "D24BFF", "B": "8E5CFF", "C": "4F8CFF", "D": "2EE6D6"}}
TIER_ANIM = 1.5
BOARD_TOP, ROW_H, ROW_GAP, LETTER_W, GRID_X = 70, 182, 8, 170, 230

def assign_tiers(ranked):
    """ranked = best first -> m["tier"] by position (S top 10 %, at least one)."""
    n, i = len(ranked), 0
    bounds, acc = [], 0.0
    for share in TIER_SHARE:
        acc += share
        bounds.append(max(1, round(n * min(acc, 1.0))))
    for pos, m in enumerate(ranked):
        while pos >= bounds[i] and i < len(TIERS) - 1:
            i += 1
        m["tier"] = TIERS[i]

def tier_layout(ranked):
    """Fixed slot (x, y, w, h) for every moment, sized so each row fits all of its moments."""
    rows = {t: [m for m in ranked if m["tier"] == t] for t in TIERS}
    for ti, t in enumerate(TIERS):
        ms = list(reversed(rows[t]))                    # filled in countdown order: weakest first, left to right
        c = max(1, len(ms))
        w = int(min(256, (W - GRID_X - 30) / c - 10))
        h = int(w * 9 / 16)
        y = BOARD_TOP + ti * (ROW_H + ROW_GAP) + (ROW_H - h - 26) // 2
        for j, m in enumerate(ms):
            m["slot"] = (GRID_X + j * (w + 10), y, w, h)

def tier_thumb(m, tmp):
    """The moment's payoff frame, small."""
    p = tmp / f"th_{m['rank']}.jpg"
    if not p.exists():
        t = m.get("peak") or (m["seg"][0] + m["seg"][1]) / 2
        run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", m["source"], "-frames:v", "1",
             "-vf", "scale=512:288:force_original_aspect_ratio=increase,crop=512:288", str(p)])
    return p

CARD_SLATE = (59, 49, 46)                            # BGR of the score card's dark pill (#2E313B)
CARD_TIER = {"S": "8BE04E", "A": "FFE135", "B": "FFB238", "C": "FF8FB8", "D": "8FD3FF"}   # card's lime 7/10, banana, ...

def board_bg(z, tmp):
    """
    bananas: banana-yellow paper (grain + a few brown freckles like the banana skins), the rows are dark
    slate pills like the score card's label, hand-cut (slightly rough) edges and a soft drop shadow.
    """
    import cv2
    path = tmp / f"board_bg_{z}.png"
    if path.exists():
        return path
    rnd = np.random.default_rng(7)
    img = np.zeros((H, W, 3), np.float32)
    img[:] = (53, 225, 255)                              # #FFE135
    img += rnd.normal(0, 7, (H, W, 1))                   # paper grain
    img *= np.clip(1.06 - 0.25 * ((np.mgrid[0:H, 0:W][0] / H - 0.5) ** 2 + (np.mgrid[0:H, 0:W][1] / W - 0.5) ** 2), 0, 1)[..., None]
    for _ in range(140):                                 # banana freckles
        cv2.circle(img, (int(rnd.uniform(0, W)), int(rnd.uniform(0, H))), int(rnd.uniform(2, 6)), (30, 70, 110), -1)
    img = np.clip(img, 0, 255).astype(np.uint8)

    def rough(x0, y0, x1, y1, step=36, j=3.5):
        pts = [(x, y0 + rnd.uniform(-j, j)) for x in np.arange(x0, x1, step)]
        pts += [(x1 + rnd.uniform(-j, j), y) for y in np.arange(y0, y1, step)]
        pts += [(x, y1 + rnd.uniform(-j, j)) for x in np.arange(x1, x0, -step)]
        pts += [(x0 + rnd.uniform(-j, j), y) for y in np.arange(y1, y0, -step)]
        return np.array(pts, np.int32)

    shadow = np.zeros((H, W), np.uint8)
    for ti in range(len(TIERS)):
        y = BOARD_TOP + ti * (ROW_H + ROW_GAP)
        cv2.fillPoly(shadow, [rough(36, y + 8, W - 24, y + ROW_H + 8)], 255)
    shadow = cv2.GaussianBlur(shadow, (0, 0), 9)[..., None] / 255.0
    img = (img * (1 - 0.45 * shadow)).astype(np.uint8)
    for ti in range(len(TIERS)):
        y = BOARD_TOP + ti * (ROW_H + ROW_GAP)
        cv2.fillPoly(img, [rough(30, y, W - 30, y + ROW_H)], CARD_SLATE, cv2.LINE_AA)
        cv2.polylines(img, [rough(30, y, W - 30, y + ROW_H, j=2)], True, (20, 20, 20), 3, cv2.LINE_AA)
    cv2.imwrite(str(path), img)
    return path

def board_graph(z, placed, tmp, base_label="[bg]", length=2.0, bg_index=None):
    """ffmpeg filter pieces drawing the board (rows, letters) + the already placed moments."""
    col = TIER_COL[z]
    if bg_index is not None:                             # bananas: the score card look
        draw = ""
        for ti, t in enumerate(TIERS):
            y = BOARD_TOP + ti * (ROW_H + ROW_GAP)
            draw += (f",drawtext=fontfile='{K.ff_path(FONT_B)}':text='{t}':fontsize=140:fontcolor=0x{CARD_TIER[t]}:"
                     f"borderw=9:bordercolor=0x1E4A12:shadowx=4:shadowy=6:shadowcolor=black@0.5:"
                     f"x=30+({LETTER_W}-tw)/2:y={y}+({ROW_H}-th)/2")
        return [f"[{bg_index}:v]format=rgb24{draw}{base_label}"]
    g = [f"color=0x{DARK[z]}:s={W}x{H}:r={FPS}:d={length:.2f}[bg0]"]
    draw = ""
    for ti, t in enumerate(TIERS):
        y = BOARD_TOP + ti * (ROW_H + ROW_GAP)
        draw += (f",drawbox=x=30:y={y}:w={W - 60}:h={ROW_H}:color=0x{col[t]}@0.16:t=fill"
                 f",drawbox=x=30:y={y}:w={LETTER_W}:h={ROW_H}:color=0x{col[t]}:t=fill"
                 f",drawtext=fontfile='{K.ff_path(FONT_B)}':text='{t}':fontsize=120:fontcolor=0x111111:"
                 f"x=30+({LETTER_W}-tw)/2:y={y}+({ROW_H}-th)/2")
    g.append(f"[bg0]{draw[1:]}{base_label}")
    return g

def render_board(z, placed, new, out, tmp, venc, length, caption=None):
    """
    A board segment: placed moments sit in their rows; `new` (or None) flies from the centre into its
    slot in 0.55 s with a swoosh and lands with a hit. `placed` + `new` keep their fixed slots.
    """
    ins = ["-f", "lavfi", "-t", f"{length:.2f}", "-i", f"anullsrc=r=48000:cl=stereo"]
    if z == "bananas":
        ins += ["-loop", "1", "-framerate", str(FPS), "-t", f"{length:.2f}", "-i", str(board_bg(z, tmp))]
        g = board_graph(z, placed, tmp, "[b0]", length, bg_index=1)
        last, n = "[b0]", 2
    else:
        g = board_graph(z, placed, tmp, "[b0]", length)
        last, n = "[b0]", 1
    items = list(placed) + ([new] if new else [])
    for i, m in enumerate(items):
        x, y, w, h = m["slot"]
        ins += ["-loop", "1", "-t", f"{length:.2f}", "-i", str(tier_thumb(m, tmp))]
        if m is new:
            cx, cy, bw = (W - w * 2) // 2, (H - h * 2) // 2, w * 2
            ease = "min(1,t/0.55)"
            g.append(f"[{n}:v]scale={w - 8}:{h - 8},pad={w}:{h}:4:4:white,setsar=1[t{i}]")
            g.append(f"{last}[t{i}]overlay=x='{cx}+({x}-{cx})*{ease}':y='{cy}+({y}-{cy})*{ease}':eval=frame[o{i}]")
        else:
            g.append(f"[{n}:v]scale={w - 8}:{h - 8},pad={w}:{h}:4:4:white,setsar=1[t{i}]")
            g.append(f"{last}[t{i}]overlay=x={x}:y={y}[o{i}]")
        name = text_file(tmp, f"bn{m['rank']}", "@" + m["channel"])
        fs = max(14, min(24, w // 9))
        en = ":enable='gte(t,0.55)'" if m is new else ""
        g.append(f"[o{i}]drawtext=fontfile='{K.ff_path(FONT_M)}':textfile='{name}':fontsize={fs}:fontcolor=white:"
                 f"x={x}:y={y + h + 3}{en}[n{i}]")
        last, n = f"[n{i}]", n + 1
    if caption:                                            # "#1 is insane, stay till the end" (max 2x)
        g.append(f"{last}drawtext=fontfile='{K.ff_path(FONT_B)}':textfile='{text_file(tmp, 'teaser', caption.upper())}':"
                 f"fontsize=40:fontcolor=white:borderw=4:bordercolor=0x1E1E1E:x=(w-tw)/2:y=1030[cap]")
        last = "[cap]"
    g.append(f"{last}format=yuv420p[v]")
    mixes = ["[0:a]"]
    for name, at, vol in (("flash.wav", 0.0, 0.35), ("hit.wav", 0.55, 0.5)) if new else (("flash.wav", 0.0, 0.3),):
        f = SFX / name
        if f.exists():
            ins += ["-i", str(f)]
            g.append(f"[{n}:a]aresample=48000,aformat=channel_layouts=stereo,volume={vol},adelay={int(at * 1000)}:all=1[s{n}]")
            mixes.append(f"[s{n}]")
            n += 1
    g.append(f"{''.join(mixes)}amix=inputs={len(mixes)}:duration=first:normalize=0[a]")
    run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error"] + ins +
        ["-filter_complex", ";".join(g), "-map", "[v]", "-map", "[a]", "-t", f"{length:.2f}"] + VOUT + venc + AOUT + [str(out)])


def render_silent(m, out, z, rank, idx, tmp, venc):
    s, e = m["seg"]
    norm = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2"
    run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{s:.2f}", "-t", f"{e - s:.2f}",
         "-i", m["source"], "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-filter_complex",
         f"[0:v]{norm},fps={FPS},setsar=1,{overlays(tmp, z, m, rank, idx)}[v]", "-map", "[v]", "-map", "1:a",
         "-t", f"{e - s:.2f}"] + VOUT + venc + AOUT + [str(out)])

def render_sweep(out, z, venc, music, mstart):
    """fanpage transition: dark frame, a neon pink bar sweeps across, whoosh + a breath of our music."""
    whoosh = HERE / "assets" / "sfx" / "flash.wav"
    ins = ["-f", "lavfi", "-t", f"{SWEEP}", "-i", f"color=0x{DARK[z]}:s={W}x{H}:r={FPS}",
           "-f", "lavfi", "-t", f"{SWEEP}", "-i", f"color=0x{COL[z]}:s=520x{H}:r={FPS}"]
    graph = [f"[1:v]format=rgba,rotate=0.25:c=none:ow=rotw(0.25):oh=roth(0.25)[bar];"
             f"[0:v][bar]overlay=x='-w+(W+w)*t/{SWEEP}':y='(H-h)/2':shortest=1,format=yuv420p[v]"]
    mixes, n = [], 2
    if music:
        ins += ["-ss", f"{mstart:.2f}", "-i", str(music)]
        graph.append(f"[{n}:a]atrim=0:{SWEEP},asetpts=PTS-STARTPTS,volume=0.16,aresample=48000[mu]")
        mixes.append("[mu]")
        n += 1
    if whoosh.exists():
        ins += ["-i", str(whoosh)]
        graph.append(f"[{n}:a]atrim=0:{SWEEP},asetpts=PTS-STARTPTS,volume=0.5,aresample=48000[wh]")
        mixes.append("[wh]")
        n += 1
    ins += ["-f", "lavfi", "-t", f"{SWEEP}", "-i", "anullsrc=r=48000:cl=stereo"]
    mixes.append(f"[{n}:a]")
    graph.append(f"{''.join(mixes)}amix=inputs={len(mixes)}:duration=longest:normalize=0,"
                 f"aformat=channel_layouts=stereo,atrim=0:{SWEEP}[a]")
    run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error"] + ins +
        ["-filter_complex", ";".join(graph), "-map", "[v]", "-map", "[a]", "-t", f"{SWEEP}"] + VOUT + venc + AOUT + [str(out)])

def render_end(out, z, venc, music, mstart, tmp, length=8.0):
    """End slate for YouTube end-screen elements: dark, brand name, (bananas: our banana), music."""
    wm = "gta6bananaclips" if z == "bananas" else "gta6fanpageclip"
    pfp = HERE / "assets" / "brand" / "pfp_bananas.webp"
    ins = ["-f", "lavfi", "-t", f"{length}", "-i", f"color=0x{DARK[z]}:s={W}x{H}:r={FPS}"]
    v = "[0:v]"
    graph = []
    n = 1
    if z == "bananas" and pfp.exists():
        ins += ["-loop", "1", "-t", f"{length}", "-i", str(pfp)]
        graph.append(f"[1:v]scale=300:300,format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
                     f"a='if(lt(hypot(X-150,Y-150),148),255,0)'[p];{v}[p]overlay=x=(W-w)/2:y=250[v1]")
        v, n = "[v1]", 2
    graph.append(f"{v}drawtext=fontfile='{K.ff_path(FONT_B)}':textfile='{text_file(tmp, 'end', wm)}':fontsize=84:"
                 f"fontcolor=0x{COL[z]}:x=(w-tw)/2:y=600,fade=t=in:d=0.4,fade=t=out:st={length - 0.6:.2f}:d=0.6,"
                 f"format=yuv420p[v]")
    if music:
        ins += ["-ss", f"{mstart:.2f}", "-i", str(music)]
        graph.append(f"[{n}:a]atrim=0:{length},asetpts=PTS-STARTPTS,volume=0.3,afade=t=in:d=0.5,"
                     f"afade=t=out:st={length - 1.5:.2f}:d=1.5,aresample=48000,aformat=channel_layouts=stereo,"
                     f"apad=whole_dur={length},atrim=0:{length}[a]")
        amap = "[a]"
    else:
        ins += ["-f", "lavfi", "-t", f"{length}", "-i", "anullsrc=r=48000:cl=stereo"]
        amap = f"{n}:a"
    run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error"] + ins +
        ["-filter_complex", ";".join(graph), "-map", "[v]", "-map", amap, "-t", f"{length}"] + VOUT + venc + AOUT + [str(out)])

def render_cold_open(top3, out, z, tmp, venc):
    """10-12 s teaser: the payoffs of the best 3 (3.7 s each), hard cuts, credits, no numbers."""
    parts = []
    for i, m in enumerate(top3):
        p = m.get("peak") or (m["seg"][0] + m["seg"][1]) / 2
        s = max(0.0, p - 2.5)
        e = min(K.duration(m["source"]), p + 1.2)
        mm = {**m, "seg": [s, e]}
        seg = tmp / f"cold_{i}.mp4"
        render_moment_plain(mm, seg, z, i, tmp, venc)
        parts.append(seg)
    concat(parts, out)

def render_moment_plain(m, out, z, idx, tmp, venc):
    s, e = m["seg"]
    src = m["source"]
    if not K.has_audio(src):
        return render_silent(m, out, z, None, f"o{idx}", tmp, venc)
    norm = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2"
    run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{s:.2f}", "-t", f"{e - s:.2f}",
         "-i", src, "-filter_complex",
         f"[0:v]{norm},fps={FPS},setsar=1,{overlays(tmp, z, m, None, f'o{idx}')}[v];"
         f"[0:a]aresample=48000,aformat=channel_layouts=stereo,loudnorm=I=-16:TP=-1.5:LRA=11,"
         f"apad=whole_dur={e - s:.2f},afade=t=in:d=0.04,afade=t=out:st={e - s - 0.1:.2f}:d=0.1[a]",
         "-map", "[v]", "-map", "[a]", "-t", f"{e - s:.2f}"] + VOUT + venc + AOUT + [str(out)])

def concat(parts, out):
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{Path(p).resolve().as_posix()}'\n" for p in parts), encoding="utf-8")
    run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(out)])
    lst.unlink()

def flag(z, name, default=True):
    """Every new element can be switched off per brand: brands.json -> <brand> -> long_flags."""
    return bool(((K.load_json(K.BRANDS, {}).get(z) or {}).get("long_flags") or {}).get(name, default))

def render_league_card(z, rows, out, tmp, venc, length=2.5):
    """Banana League standings (real data: liga.table) between blocks of 5 moments."""
    lines = [f"#{r['rank']}  @{r['channel']}   avg {r['avg']:.1f}   {__import__('liga').movement(r)}" for r in rows[:5]]
    f = (f"drawtext=fontfile='{K.ff_path(FONT_B)}':textfile='{text_file(tmp, 'lg_t', 'BANANA LEAGUE')}':fontsize=96:"
         f"fontcolor=0x{COL[z]}:x=(w-tw)/2:y=110")
    for i, ln in enumerate(lines):
        f += (f",drawtext=fontfile='{K.ff_path(FONT_M)}':textfile='{text_file(tmp, f'lg{i}', ln)}':fontsize=54:"
              f"fontcolor=white:box=1:boxcolor=0x{COL[z]}@0.18:boxborderw=14:x=(w-tw)/2:y={300 + i * 120}")
    ins = ["-f", "lavfi", "-t", f"{length}", "-i", f"color=0x{DARK[z]}:s={W}x{H}:r={FPS}",
           "-f", "lavfi", "-t", f"{length}", "-i", "anullsrc=r=48000:cl=stereo"]
    g = [f"[0:v]{f},fade=t=in:d=0.25,format=yuv420p[v]"]
    mixes = ["[1:a]"]
    if (SFX / "flash.wav").exists():
        ins += ["-i", str(SFX / "flash.wav")]
        g.append("[2:a]aresample=48000,aformat=channel_layouts=stereo,volume=0.3[s2]")
        mixes.append("[s2]")
    g.append(f"{''.join(mixes)}amix=inputs={len(mixes)}:duration=first:normalize=0[a]")
    run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error"] + ins +
        ["-filter_complex", ";".join(g), "-map", "[v]", "-map", "[a]", "-t", f"{length}"] + VOUT + venc + AOUT + [str(out)])


# ---------------------------------------------------------------------------
# fanpage = "THE STORY / NIGHT IN VICE CITY" (Aleš 17. 11.: the two accounts test different concepts).
# No ranking, no tier list, no countdown, no #N on screen: chronological, cinematic, music carries it.

MODEL_REAKCE = HERE / "assets" / "model_reakce"
LETTERBOX = int((H - W / 2.35) / 2)                       # 2.35:1 bars: 131 px top and bottom

def main_series(z):
    """The channel's main series (fallback of every themed day)."""
    return "after-dark" if z == "fanpage" and flag(z, "fanpage_story") else DAILY[z]

def story_mode(z):
    return z == "fanpage" and flag(z, "fanpage_story")

def chapter_label(m, serie):
    """Data only: hour of the stream (VOD offset) for one streamer's night, clock time for After Dark."""
    if serie == "streamer-night" and m.get("vod_offset") is not None:
        return f"HOUR {int(m['vod_offset'] // 3600) + 1}"
    t = datetime.fromtimestamp(m.get("created") or 0, TZ)
    return t.strftime("%I %p").lstrip("0")

def render_chapter_card(z, label, out, tmp, venc, length=1.2):
    """Black 2.35:1 card with the chapter title (time of the stream), the bars close in, a soft hit."""
    ins = ["-f", "lavfi", "-t", f"{length}", "-i", f"color=black:s={W}x{H}:r={FPS}",
           "-f", "lavfi", "-t", f"{length}", "-i", "anullsrc=r=48000:cl=stereo"]
    g = [f"[0:v]drawtext=fontfile='{K.ff_path(K.CAP_FONTS / 'anton.ttf')}':textfile='{text_file(tmp, 'ch_' + label.replace(' ', '_'), label)}':"
         f"fontsize=120:fontcolor=white:x=(w-tw)/2:y=(h-th)/2:alpha='min(1,t/0.25)',"
         f"drawbox=x=0:y=(h-4)/2+90:w=iw:h=4:color=0x{COL[z]}@0.9:t=fill,fade=t=out:st={length - 0.2:.2f}:d=0.2,"
         f"format=yuv420p[v]"]
    mixes, n = ["[1:a]"], 2
    if (SFX / "hit.wav").exists():
        ins += ["-i", str(SFX / "hit.wav")]
        g.append("[2:a]aresample=48000,aformat=channel_layouts=stereo,volume=0.35[h]")
        mixes.append("[h]")
    g.append(f"{''.join(mixes)}amix=inputs={len(mixes)}:duration=first:normalize=0[a]")
    run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error"] + ins +
        ["-filter_complex", ";".join(g), "-map", "[v]", "-map", "[a]", "-t", f"{length}"] + VOUT + venc + AOUT + [str(out)])

def render_model_card(z, out, tmp, venc, seed, length=1.0):
    """Our presenter's silent reaction (a still from assets/model_reakce/ with a spring pop); None if none."""
    stills = sorted(p for p in MODEL_REAKCE.glob("*.png")) if MODEL_REAKCE.exists() else []
    if not stills:
        return None
    img = random.Random(seed).choice(stills)
    pop = "min(1,1.15*sin(min(t,0.35)/0.35*PI/2)+0.0)"   # quick overshoot pop
    g = [f"[1:v]scale=-2:820,format=rgba[m];[0:v][m]overlay=x=(W-w)/2:y=(H-h)/2+40*(1-{pop}):eval=frame,"
         f"fade=t=in:d=0.15,fade=t=out:st={length - 0.2:.2f}:d=0.2,format=yuv420p[v]"]
    run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-t", f"{length}", "-i", f"color=0x{DARK[z]}:s={W}x{H}:r={FPS}",
         "-loop", "1", "-t", f"{length}", "-i", str(img),
         "-f", "lavfi", "-t", f"{length}", "-i", "anullsrc=r=48000:cl=stereo",
         "-filter_complex", ";".join(g), "-map", "[v]", "-map", "2:a", "-t", f"{length}"] + VOUT + venc + AOUT + [str(out)])
    return out

def render_outro16(z, out, tmp, venc):
    """The model's outro (vertical) centred on the brand's dark background + the Follow card over its end."""
    b = (K.load_json(K.BRANDS, {}).get(z) or {})
    outro = HERE / b["outro"] if b.get("outro") else None
    if not outro or not outro.exists():
        return None
    length = K.duration(outro)
    follow = HERE / b["follow"] if b.get("follow") else None
    ins = ["-f", "lavfi", "-t", f"{length:.2f}", "-i", f"color=0x{DARK[z]}:s={W}x{H}:r={FPS}", "-i", str(outro)]
    g = [f"[1:v]scale=-2:{H},setsar=1[o];[0:v][o]overlay=x=(W-w)/2:y=0:eof_action=pass[v0]"]
    last = "[v0]"
    if follow and follow.exists() and length > 3:
        ins += ["-c:v", "libvpx-vp9", "-i", str(follow)]
        g.append(f"[2:v]scale=-2:{H},setpts=PTS+{length - 2.2:.2f}/TB[f];{last}[f]overlay=x=(W-w)/2:y=0:eof_action=pass[v1]")
        last = "[v1]"
    g.append(f"{last}format=yuv420p[v]")
    amap = "1:a" if K.has_audio(outro) else None
    if amap is None:
        ins += ["-f", "lavfi", "-t", f"{length:.2f}", "-i", "anullsrc=r=48000:cl=stereo"]
        amap = f"{len([x for x in ins if x == '-i']) - 1}:a"
    else:
        g.append(f"[1:a]aresample=48000,aformat=channel_layouts=stereo,apad=whole_dur={length:.2f}[oa]")
        amap = "[oa]"
    run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error"] + ins +
        ["-filter_complex", ";".join(g), "-map", "[v]", "-map", amap, "-t", f"{length:.2f}"] + VOUT + venc + AOUT + [str(out)])
    return out

def build_story(a, picked, d, tmp):
    """
    Chronological story (fanpage): [chapter card "HOUR 2" / "11 PM"] -> moments of that chapter (no ranking,
    no on-screen numbers, cinematic grade, 2.35:1 bars at the cuts) -> model reaction card between chapters
    (if assets/model_reakce/ exists) -> ... -> the model's outro + Follow card. After Dark: every moment
    ends on a beat of the music. Returns (path, chapters, duration, info for qa_long).
    """
    z, serie = a.znacka, series_key(a.serie)
    venc = encoder()
    music, _, mstart = K.music_cfg(f"{d}:{z}:{serie}", 60, (K.load_json(K.BRANDS, {}).get(z) or {}).get("music"))
    mlen = K.duration(music) if music else 0.0
    m0 = (mstart + 7) % max(mlen - 12, 1.0) if music else 0.0
    beats = K.beat_grid(music, m0, 900) if (music and serie == "after-dark") else None
    story = sorted(picked, key=lambda m: ((m.get("video_id") or ""), m.get("vod_offset") or 0)
                   if serie == "streamer-night" else (m.get("created") or 0, m.get("vod_offset") or 0))
    for i, m in enumerate(story, 1):
        m["rank"] = i                                      # internal order only, never on screen
    parts, chapters, static, louder, t = [], [], [], [], 0.0
    info = {"moments": [], "board_entries": 0, "tiers": False}

    def add(path, kind):
        nonlocal t
        dur = K.duration(path)
        parts.append(path)
        if kind in ("card", "end"):
            static.append((t, t + dur))
        t += dur

    groups = []
    for m in story:
        lab = chapter_label(m, serie)
        if not groups or groups[-1][0] != lab:
            groups.append((lab, []))
        groups[-1][1].append(m)
    for gi, (lab, ms) in enumerate(groups):
        if gi and flag(z, "model_cards"):
            mc = render_model_card(z, tmp / f"model{gi:02d}.mp4", tmp, venc, f"{d}:{gi}")
            if mc:
                louder.append((t, t + 1.0))
                add(mc, "card")
        cc = tmp / f"chap{gi:02d}.mp4"
        render_chapter_card(z, lab, cc, tmp, venc)
        chapters.append((t, f"{lab} - {ms[0].get('name') or ms[0]['channel']}"))
        louder.append((t, t + 1.2))
        add(cc, "card")
        for m in ms:
            seg = tmp / f"m{m['rank']:02d}.mp4"
            cut_to = None
            if beats:                                      # After Dark: the cut lands on a beat
                T, ph = beats
                natural = (m["seg"][1] - m["seg"][0])
                end = t + natural
                k = math.floor((end - ph) / T)
                target = ph + k * T
                if target - t > 6.0:
                    cut_to = target - t
            log(f"  {lab} {m['channel']} {m['seg'][0]:.1f}-{m['seg'][1]:.1f} s" + (f" -> {cut_to:.2f} s on beat" if cut_to else ""))
            render_moment(m, seg, z, None, m["rank"], tmp, venc, story=True, cut_to=cut_to)
            m["at"] = round(t, 2)
            info["moments"].append({"rank": m["rank"], "channel": m["channel"], "at": t})
            add(seg, "moment")
    end = tmp / "outro.mp4"
    if not render_outro16(z, end, tmp, venc):
        render_end(end, z, venc, None, 0.0, tmp)
    louder.append((t, t + K.duration(end)))
    add(end, "end")
    if len(chapters) < 3 or any(b_[0] - a_[0] < 10 for a_, b_ in zip(chapters, chapters[1:])):
        chapters = [(0.0, "Start")] + [(mm["at"], f"{mm.get('name') or mm['channel']}") for mm in story[1:]]
        merged = [chapters[0]]
        for c in chapters[1:]:
            if c[0] - merged[-1][0] >= 10:
                merged.append(c)
        chapters = merged
    else:
        chapters[0] = (0.0, chapters[0][1])
    out = OUT / z / f"{d}_{a.serie.replace(':', '-')}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    raw = tmp / "concat.mp4"
    concat(parts, raw)
    music_pass(raw, out, z, music, m0, louder, vol=MUSIC_VOL_STORY)
    info.update(static=static, parts_total=round(t, 2), n_moments=len(story), order=[])
    return out, chapters, K.duration(out), info

MUSIC_VOL_STORY = 0.09                                    # the music carries the story (ducked under speech)

def build(a, picked, d, tmp, tiers=True):
    """
    Fixed structure, nothing improvised (Aleš 17. 11.):
      cold open 10-12 s -> empty tier board 2 s -> for #N..#1: [intro card 0.6 s over] moment -> flight into
      the tier list 1.5 s -> full board 3 s -> end card. League cards (bananas Sunday) only between blocks
      of 5 moments, max 3x; teaser "stay till #1" max 2x, on the board after #6 and #3 (never over a moment).
      tiers=False: plain countdown (the QA fallback).
    Returns (path, chapters, duration, info for qa_long).
    """
    z = a.znacka
    if story_mode(z):                                      # fanpage: the story, never a ranking
        return build_story(a, picked, d, tmp)
    tiers = tiers and flag(z, "tier_list")
    venc = encoder()
    music, _, mstart = K.music_cfg(f"{d}:{z}:{a.serie}", 60, (K.load_json(K.BRANDS, {}).get(z) or {}).get("music"))
    mlen = K.duration(music) if music else 0.0
    mpos = lambda i: (mstart + 7 * i) % max(mlen - 12, 1.0)
    ranked = sorted(picked, key=lambda m: -m["score"])     # best first
    if SERIES[series_key(a.serie)].get("league") and flag(z, "league"):
        try:
            import liga
            table = {r["channel"]: r for r in liga.table(7)}
        except Exception as e:
            table = {}
            log(f"  league: {e}")
        best = {}
        for m in ranked:                                   # each streamer once: his best moment of the week
            if m["channel"] in table and m["channel"] not in best:
                best[m["channel"]] = m
                m["league"] = table[m["channel"]]
        if len(best) >= 3:
            ranked = sorted(best.values(), key=lambda m: m["league"]["rank"])
    for i, m in enumerate(ranked, 1):
        m["rank"] = i
    order = list(reversed(ranked))                         # countdown: weakest first, #1 last
    assign_tiers(ranked)
    tier_layout(ranked)
    league = []
    if SERIES[series_key(a.serie)].get("league") and flag(z, "league"):
        try:
            import liga
            league = liga.table(7)
        except Exception as e:
            log(f"  league: {e}")
    parts, chapters, static, t = [], [], [], 0.0
    info = {"moments": [], "board_entries": 0, "tiers": tiers}

    def add(path, kind):
        nonlocal t
        dur = K.duration(path)
        parts.append(path)
        if kind in ("board", "league", "end"):
            static.append((t, t + dur))
        t += dur
        return dur

    cold = tmp / "cold.mp4"
    render_cold_open(ranked[:3], cold, z, tmp, venc)
    chapters.append((0, "Best moments preview"))
    add(cold, "cold")
    louder = [(max(0.0, t - 0.6), t + 0.4)]
    if tiers:
        b0 = tmp / "board0.mp4"                            # empty board: where does #1 go?
        render_board(z, [], None, b0, tmp, venc, 2.0)
        louder.append((t, t + 2.0))
        add(b0, "board")
    placed, teasers, league_cards = [], {r for r in (6, 3) if r < len(ranked)}, 0
    for i, m in enumerate(order):
        seg = tmp / f"m{i:02d}.mp4"
        log(f"  #{m['rank']} {m['channel']} {m['seg'][0]:.1f}-{m['seg'][1]:.1f} s tier {m['tier']}")
        lg = m.get("league")
        render_moment(m, seg, z, m["rank"], i, tmp, venc,
                      banana=z == "bananas" and m["rank"] <= 5 and flag(z, "banana_reaction"),
                      badge=f"#{lg['rank']} · {lg['avg']:.1f} AVG" if lg else None)
        title = re.sub(r"\s+", " ", m.get("title") or "").strip()[:50]
        chapters.append((t, f"#{m['rank']} {m.get('name') or m['channel']}" + (f" - {title}" if title else "")))
        m["at"] = round(t, 2)
        louder.append((t, t + 0.7))
        info["moments"].append({"rank": m["rank"], "channel": m["channel"], "at": t})
        add(seg, "moment")
        if tiers:
            tb = tmp / f"tier{i:02d}.mp4"                  # the moment flies into its tier row
            render_board(z, placed, m, tb, tmp, venc, TIER_ANIM,
                         caption=TEASER if (m["rank"] in teasers and m["rank"] != 1) else None)
            placed.append(m)
            louder.append((t, t + TIER_ANIM))
            add(tb, "board")
        elif m["rank"] in teasers:
            pass                                           # plain countdown: no teaser (never over a moment)
        pass
    info["board_entries"] = len(placed)
    if tiers:
        full = tmp / "board_full.mp4"                      # the whole board before the end card
        render_board(z, placed, None, full, tmp, venc, 3.0)
        louder.append((t, t + 3.0))
        add(full, "board")
    louder.append((t, t + 8.0))
    end = tmp / "end.mp4"
    render_end(end, z, venc, None, 0.0, tmp)
    add(end, "end")
    out = OUT / z / f"{d}_{a.serie.replace(':', '-')}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    raw = tmp / "concat.mp4"
    concat(parts, raw)
    music_pass(raw, out, z, music, mpos(1), louder)
    info.update(static=static, parts_total=round(t, 2), n_moments=len(order), order=[m["rank"] for m in order])
    return out, chapters, K.duration(out), info

def qa_long(out, chapters, info, min_len, max_len=900.0):
    """
    Automatic QA of the finished video. Returns a list of problems ([] = OK):
    length, chapters vs real times, credits, black / frozen picture inside moments, loudness -16 LUFS
    +-1.5 and true peak <= -1 dBTP, silence, tier board entries, countdown order.
    """
    bad = []
    dur = K.duration(out)
    if not min_len <= dur <= max_len:
        bad.append(f"length {dur:.0f} s outside {min_len:.0f}-{max_len:.0f} s")
    if abs(dur - info["parts_total"]) > 1.0:
        bad.append(f"chapters drift: parts {info['parts_total']:.1f} s vs video {dur:.1f} s")
    if chapters[0][0] != 0 or any(b[0] - a_[0] < 10 for a_, b in zip(chapters, chapters[1:])):
        bad.append("chapters: first not at 0:00 or one shorter than 10 s")
    if any(not m["channel"] for m in info["moments"]):
        bad.append("a moment without credit")
    if info["order"] != sorted(info["order"], reverse=True):
        bad.append(f"countdown order wrong: {info['order']}")
    if info["tiers"] and info["board_entries"] != info["n_moments"]:
        bad.append(f"tier board has {info['board_entries']} entries for {info['n_moments']} moments")
    p = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(out),
                        "-vf", "blackdetect=d=1.5:pic_th=0.97:pix_th=0.06,freezedetect=n=0.002:d=4",
                        "-af", "ebur128=peak=true,silencedetect=n=-50dB:d=2", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
    err = p.stderr
    inside = lambda t0: not any(s0 - 0.5 <= t0 <= s1 + 0.5 for s0, s1 in info["static"])
    for m0 in re.finditer(r"black_start:([\d.]+)", err):
        if inside(float(m0.group(1))):
            bad.append(f"black picture at {float(m0.group(1)):.1f} s")
            break
    for m0 in re.finditer(r"freeze_start: ([\d.]+)", err):
        if inside(float(m0.group(1))):
            bad.append(f"frozen picture at {float(m0.group(1)):.1f} s")
            break
    for sil in re.finditer(r"silence_start: (-?[\d.]+)", err):
        t0 = max(0.0, float(sil.group(1)))
        if t0 < dur - 8.5:                                  # the end card fades out on purpose
            bad.append(f"silence at {t0:.1f} s")
            break
    summ = err[err.rfind("Summary:"):] if "Summary:" in err else ""
    li = re.search(r"I:\s+(-?[\d.]+) LUFS", summ)
    tp = re.search(r"True peak:\s+Peak:\s+(-?[\d.]+|-inf) dBFS", summ) or re.search(r"Peak:\s+(-?[\d.]+) dBFS", summ)
    if li and abs(float(li.group(1)) + 16) > 1.5:
        bad.append(f"loudness {li.group(1)} LUFS (target -16 +-1.5)")
    if tp and tp.group(1) != "-inf" and float(tp.group(1)) > -1.0:
        bad.append(f"true peak {tp.group(1)} dBTP (> -1)")
    log(f"  QA: {dur:.0f} s, {li.group(1) if li else '?'} LUFS, peak {tp.group(1) if tp else '?'} dBTP -> "
        + ("OK" if not bad else "; ".join(bad)))
    return bad

# -----------------------------------------------------------------------------

def main():
    global MIN_VERIFIED, MIN_VIDEO
    p = argparse.ArgumentParser(description="long-form YouTube compilations (16:9) from stream moments")
    p.add_argument("--znacka", required=True, choices=["bananas", "fanpage"])
    p.add_argument("--serie", default=None, help="override the calendar: " + ", ".join(SERIES) + ", streamer:<login>")
    p.add_argument("--den", help="publish day YYYY-MM-DD (default tomorrow, Prague): its calendar series")
    p.add_argument("--od", help="first day of moments (YYYY-MM-DD, Prague)")
    p.add_argument("--do", help="last day of moments (YYYY-MM-DD, Prague)")
    p.add_argument("--delka", type=float, default=560, help="target length of the moments (s); video 8-12 min")
    p.add_argument("--min-video", type=float, default=MIN_VIDEO, help="shorter video than this: none (s)")
    p.add_argument("--min-overeno", type=int, default=MIN_VERIFIED, help="verified moments a themed series needs")
    p.add_argument("--min-momentu", type=int, default=25)
    p.add_argument("--min-momentu-hlavni", type=int, default=3, help="main series: at least this many moments")
    p.add_argument("--max-momentu", type=int, default=35)
    p.add_argument("--max-na-streamera", type=int, default=4)
    p.add_argument("--max-kandidatu", type=int, default=120, help="classify at most this many candidates")
    p.add_argument("--hra", help="Twitch category instead of GTA VI (test: 'Grand Theft Auto V')")
    p.add_argument("--pages", type=int, default=3, help="Twitch API pages of 100 clips")
    p.add_argument("--whisper", default="small.en")
    p.add_argument("--zkouska", action="store_true", help="plan only, nothing rendered or saved")
    p.add_argument("--kalendar", type=int, default=0, help="with --zkouska: simulate N publish days")
    p.add_argument("--bez-overeni", action="store_true", help="skip the vision check (test)")
    p.add_argument("--bez-nahledu", action="store_true")
    p.add_argument("--out", help="also copy the video here (e.g. out/_ukazka/long_bananas.mp4)")
    a = p.parse_args()
    MIN_VERIFIED, MIN_VIDEO = a.min_overeno, a.min_video
    if a.serie and series_key(a.serie) not in SERIES:
        sys.exit(f"unknown series {a.serie}")
    K.WHISPER_MODEL = a.whisper
    z = a.znacka
    run_day = datetime.now(TZ).date()
    publish = date.fromisoformat(a.den) if a.den else run_day + timedelta(days=1)
    used, eps, res = K.load_json(USED, {}), K.load_json(EPISODES, {}), K.load_json(RESERVE, {})
    vis = {}
    if a.kalendar:                                         # simulation: nothing is saved
        print(f"\n{z}: calendar from {publish} ({a.kalendar} days), moments {a.od or 'per series'}..{a.do or ''}")
        print(f"{'day':<11} {'planned':<17} {'series':<17} {'#':>3} {'len':>6} {'mom':>4} {'reserve':>7}  note")
        for i in range(a.kalendar):
            d = publish + timedelta(days=i)
            plan = plan_day(a, z, d, d - timedelta(days=1), used, eps, res, vis, not a.bez_overeni)
            if plan.get("serie"):
                commit_plan(a, z, plan, used, eps, res, d - timedelta(days=1))
                rsv = len(reserve_rows(z, plan["serie"], d, res))
                print(f"{plan['day']:<11} {plan['planned'] or '-':<17} {plan['serie']:<17} {plan['episode']:>3} "
                      f"{plan['length'] // 60:>3}:{plan['length'] % 60:02d} {len(plan['picked']):>4} {rsv:>7}  {plan['note']}")
            else:
                print(f"{plan['day']:<11} {plan['planned'] or '-':<17} {'-':<17} {'-':>3} {'-':>6} {'-':>4} {'-':>7}  {plan['note']}")
        return 0
    plan = plan_day(a, z, publish, run_day, used, eps, res, vis, not a.bez_overeni)
    if not plan.get("serie"):
        print(f"::warning::Long-form {z} {publish}: {plan['note']}")
        return 0
    picked = plan["picked"]
    log(f"{z} {plan['serie']} #{plan['episode']} for {publish}: {len(picked)} moments ~{plan['length'] / 60:.1f} min, "
        f"{len(plan['rest'])} to the reserve. {plan['note']}")
    if a.zkouska:
        for m in picked:
            log(f"  {m['score']:.2f} fun {m.get('fun')} {m['channel']} {m.get('views')} views {m.get('proven')} "
                f"'{(m.get('title') or '')[:40]}'")
        return 0
    a.serie = plan["serie"]
    do = date.fromisoformat(plan["do"])
    tmp = OUT / f"_tmp_{z}_{os.getpid()}"
    tmp.mkdir(parents=True, exist_ok=True)
    problems = []
    try:
        for tiers in (True, False):                        # QA failed: once more as a plain countdown
            for f in tmp.glob("*"):
                f.unlink()
            out, chapters, dur, info = build(a, picked, do, tmp, tiers=tiers)
            problems = qa_long(out, chapters, info, MIN_VIDEO)
            if not problems:
                break
            log(f"  QA failed ({'with' if tiers else 'without'} tier list): {'; '.join(problems)}")
    finally:
        for f in tmp.glob("*"):
            f.unlink()
        tmp.rmdir()
    if problems:
        fail = out.with_name(out.name.replace(".mp4", ".failed.json"))
        out.unlink(missing_ok=True)
        fail.write_text(json.dumps({"znacka": z, "serie": a.serie, "publish_day": str(publish),
                                    "duvod": "; ".join(problems)}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"::warning::Long-form {z} {publish} failed QA: {'; '.join(problems)}")
        return 0
    moments = sorted(picked, key=lambda m: m["rank"])
    lead = reach(moments)
    log(f"  most viral streamer: {lead} ({max(m['reach'] for m in moments):,} reach)")
    slot = suggested_slot(z, a.serie, do)
    meta = {
        "znacka": z, "kanal": "gta6bananaclips" if z == "bananas" else "gta6fanpageclip",
        "serie": a.serie, "episode": plan["episode"], "datum": str(do), "od": plan["od"], "do": plan["do"],
        "publish_day": str(publish), "poznamka": plan["note"],
        "title": titles_texts(z, a.serie, plan["episode"], do, moments),
        "description": description(z, a.serie, do, moments, chapters),
        "tags": yt_tags(z, a.serie, moments), "video": str(out), "duration_s": round(dur, 1),
        "nahrat": slot.isoformat(), "playlist": a.serie if not a.serie.startswith("streamer:") else "streamers",
        "moments": [{k: m.get(k) for k in ("id", "rank", "channel", "name", "url", "title", "views", "tags", "peak",
                                           "seg", "at", "source", "platform", "reach", "tag_scores", "primary",
                                           "proven", "fun", "tier")} for m in moments],
        "made_at": datetime.now(TZ).isoformat(timespec="seconds"),
    }
    mp = out.with_name(out.name.replace(".mp4", ".meta.json"))
    mp.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"  -> {out} ({dur / 60:.1f} min), '{meta['title']}', upload {slot:%Y-%m-%d %H:%M}")
    commit_plan(a, z, plan, used, eps, res, run_day)      # the number goes up only now, after the build
    K.save_json(USED, used)
    K.save_json(EPISODES, eps)
    K.save_json(RESERVE, res)
    if not a.bez_nahledu:
        try:
            import nahled
            import nahled_ai
            nahled.prepare_long(mp)
            meta = json.loads(mp.read_text(encoding="utf-8"))
            thumb = nahled_ai.make(Path(meta["nahled_vstup"]))
            meta["thumbnail"] = thumb
            mp.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:                             # no thumbnail is fine: YouTube picks a frame
            log(f"  thumbnail: {str(e)[-200:]}")
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_bytes(out.read_bytes())
    return 0

if __name__ == "__main__":
    sys.exit(main())
