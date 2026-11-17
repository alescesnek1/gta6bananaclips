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
import shutil
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
CHAPTER_EXTRA = {"bananas": 1.9, "fanpage": 0.0}           # the tier list placement after every moment
COLD_SHOT = 1.9                   # cold open: <= 2 s per moment, the build-up, never the cut used later
sys.path.insert(0, str(HERE / "tools" / "long"))            # tier, endcard, hudba, zabava

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
        import zabava
        if zabava.low_action(r):                           # low motion + low loudness + no laughter / scream
            why["low action"] = why.get("low action", 0) + 1
            continue
        s, e = segment_times(r)
        if e - s < zabava.MIN_LEN:                         # source too short for a >= 10 s chapter
            why["too short"] = why.get("too short", 0) + 1
            continue
        pk = r.get("peak")
        keep = (zabava.tight_cut(moment_words(r), r["source"], s, e, pk if pk is not None and s < pk < e else None)
                if K.has_audio(r["source"]) else [(s, e)])  # no sound: nothing to measure, the plain cut
        if keep is None:                                   # > 2 s of nothing around the payoff
            why["dead stretch"] = why.get("dead stretch", 0) + 1
            continue
        r.update(seg=[s, e], keep=keep, play=round(sum(y - x for x, y in keep), 2), score=round(series_score(r, w), 3))
        good.append(r)
    good = drop_canned(good, why)
    good.sort(key=lambda r: -r["score"])
    if not SERIES[sk].get("same_mission"):                 # the same moment never twice (same-mission IS multi-POV)
        import zabava
        for r in good:
            r.setdefault("cam", K.load_json(K.STREAMERS, {}).get(r["channel"], {}).get("cam"))
        good, dups = zabava.dedupe(good)
        for i, (k, reason) in dups.items():
            log(f"  duplicate {i[-14:]} of {k[-14:]}: {reason}")
        if dups:
            why["duplicate"] = len(dups)
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
        total += moment_len(r) + CHAPTER_EXTRA[z]
    return picked, rest, why, lead

def video_len(picked, z):
    """Estimated video length: cold open + moments (+ score cards) + end slate."""
    return 3 * COLD_SHOT + sum(moment_len(r) + CHAPTER_EXTRA[z] for r in picked) + 8.0

def moment_len(r):
    """Length of the moment in the video (after the jump cuts)."""
    return r.get("play") or (r["seg"][1] - r["seg"][0])

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
    """HPC cut on the 16:9 source: start on a sentence ~10 s before the payoff, end ~2-3.5 s after
    (tools/long/zabava.py; Aleš 17. 11.: tighter heads and tails, average moment 12-16 s)."""
    import zabava
    dur = r.get("dur") or K.duration(r["source"])
    try:
        words = moment_words(r)
    except Exception:
        words = []
    return zabava.snap_segment(words, r["cut"], r.get("peak"), dur)

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

LUFS = -16.0                      # every moment's stream audio lands here (two-pass, linear gain)

TAME = "acompressor=threshold=-13dB:ratio=5:attack=4:release=140,alimiter=limit=0.84:level=0:attack=3:release=80"

def level_gain(src, pieces, t0=0.0, words=None):
    """
    Gain (dB) that brings the STREAMER'S VOICE in these pieces to LUFS (Aleš 17. 11.: some streams are really
    quiet, every stream must be about as loud). Pass 1 of two: EBU R128 integrated loudness of the parts
    where he speaks (word times; all of it when there are under 2 s of speech), in the voice band
    (120 Hz - 6 kHz) so an engine or the game's music does not turn him down. Measuring everything would
    let one scream set the level and leave the talking 20 dB under it. The gain is linear; what is far
    louder than the voice (screams, crashes) is then held by TAME (compressor + limiter).
    """
    spans = []
    for w0, w1, _ in words or []:
        for x, y in pieces:
            lo, hi = max(x, w0 - 0.12), min(y, w1 + 0.12)
            if hi - lo > 0.05:
                if spans and lo - spans[-1][1] < 0.25:
                    spans[-1] = (spans[-1][0], hi)
                else:
                    spans.append((lo, hi))
    if sum(y - x for x, y in spans) < 2.0:
        spans = list(pieces)
    spans = spans[:60]
    parts = "".join(f"[0:a]atrim={x - t0:.3f}:{y - t0:.3f},asetpts=PTS-STARTPTS[p{i}];" for i, (x, y) in enumerate(spans))
    g = (parts + "".join(f"[p{i}]" for i in range(len(spans))) + f"concat=n={len(spans)}:v=0:a=1,"
         "highpass=f=120,lowpass=f=6000,ebur128=framelog=quiet[o]")
    p = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-ss", f"{t0:.2f}", "-t", f"{pieces[-1][1] - t0 + 0.5:.2f}",
                        "-i", str(src), "-filter_complex", g, "-map", "[o]", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    summ = p.stderr[p.stderr.rfind("Summary:"):] if "Summary:" in p.stderr else ""
    m = re.search(r"I:\s+(-?[\d.]+) LUFS", summ)
    if not m or float(m.group(1)) < -60:
        return 0.0
    return max(-20.0, min(30.0, LUFS - 1.0 - float(m.group(1))))    # the band-limited reading sits ~1 LU under the full one

LOUD_PART = -14.0                 # where the loud part of every moment lands (its integrated level is ~ -16)

def loudness(path, ss=None, t=None):
    """
    Level of the LOUD PART of a file's audio (LUFS): the 95th percentile of the short-term loudness (EBU
    R128 "LRA high"), the integrated value when the file is too short for it; None when unmeasurable.
    This is what "as loud as the other streams" means to a viewer, and unlike the integrated value it does
    not move when music fills the pauses (they pass the gate then and pull the average down by up to 4 LU).
    """
    cut = (["-ss", f"{ss:.2f}"] if ss is not None else []) + (["-t", f"{t:.2f}"] if t is not None else [])
    p = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner"] + cut + ["-i", str(path), "-vn", "-af", "ebur128=framelog=quiet",
                        "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    summ = p.stderr[p.stderr.rfind("Summary:"):] if "Summary:" in p.stderr else ""
    m = re.search(r"LRA high:\s+(-?[\d.]+) LUFS", summ)
    if not m or float(m.group(1)) < -60:
        m = re.search(r"I:\s+(-?[\d.]+) LUFS", summ)
    return float(m.group(1)) if m and float(m.group(1)) > -60 else None

def level_fix(seg, passes=2):
    """
    Pass 2 of the levelling: the finished moment (voice at its level, screams tamed, our effects on top) is
    measured as the viewer hears it and its loud part brought to LOUD_PART with one more small linear gain;
    the picture is copied.
    """
    for _ in range(passes):
        cur = loudness(seg)
        if cur is None or abs(LOUD_PART - cur) <= 0.4:
            return
        fixed = seg.with_name(seg.stem + "_lv.mp4")
        run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", str(seg), "-map", "0:v", "-map", "0:a",
             "-c:v", "copy", "-af", f"volume={max(-8.0, min(8.0, LOUD_PART - cur)):.2f}dB,alimiter=limit=0.84:level=0:attack=3:release=80"]
            + AOUT + [str(fixed)])
        fixed.replace(seg)

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
                  story=False, cut_to=None, badge=None, chapter=None, sticker=None):
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
        render_silent({**m, "seg": [s, min(e, s + cut_to) if cut_to else e]}, out, z, rank, idx, tmp, venc)
        return K.duration(out)
    dur_src = K.duration(src)
    words = moment_words(m)
    peak = m.get("peak")
    peak = peak if peak is not None and s < peak < e else None
    import zabava
    keep = [tuple(k) for k in (m.get("keep") or [])] if not cut_to else None
    if not keep or abs(keep[0][0] - s) > 0.01 or keep[-1][1] > e + 0.01:
        keep = zabava.tight_cut(words, src, s, e, peak) or (K.dead_air(words, src, s, e, peak) if words else [(s, e)])
    if sum(y - x for x, y in keep) < 8.0:                 # chapters need >= 10 s (moment + tier placement)
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
    cap_words = [w for w in cap_words if w[0] >= (CHAPTER_CARD + 0.1 if chapter else 0.0 if story else 0.65)
                 and w[1] <= length - 0.2]                 # not under the intro card / chapter title
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
                 f"aformat=channel_layouts=stereo,afade=t=in:d=0.012,afade=t=out:st={max(0.0, y - x - 0.012):.3f}:d=0.012[a{i}]")
    g.append("".join(f"[v{i}][a{i}]" for i in range(len(keep))) + f"concat=n={len(keep)}:v=1:a=1[vc][ac]")
    zexpr = zoom_marks(rw, jumps, main_end, pk)
    flash_col = "white@0.55" if z == "bananas" else f"0x{COL[z]}@0.5"
    v = (f"[vc]" + (f"tpad=stop_mode=clone:stop_duration={hold:.2f}," if hold else "")
         + f"scale=w='{W}*{zexpr}':h=-2:eval=frame,crop={W}:{H}:(iw-ow)/2:(ih-oh)/2,setsar=1"
         + (f",drawbox=enable='between(t,{pk:.2f},{pk + 0.12:.2f})':color={flash_col}:t=fill" if pk is not None else "")
         + f",subtitles=filename='{K.ff_path(ass)}':fontsdir='{K.ff_path(K.CAP_FONTS)}'"
         + (f",{story_grade(m, src, s, e)}" if story else "")
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
    if story:                                             # 2.35:1 bars SLIDE away after the cut and back in before
        k = f"min(min(1,t/0.45),max(0,({length:.2f}-t)/0.45))"       # the next one (no bars popping on and off)
        g.append(f"color=c=black:s={W}x{LETTERBOX}:r={FPS},split[lb1][lb2];"
                 f"[v0][lb1]overlay=x=0:y='-{LETTERBOX}*{k}':shortest=1[vl1];"
                 f"[vl1][lb2]overlay=x=0:y='{H - LETTERBOX}+{LETTERBOX}*{k}':shortest=1[vl2]")
        vlast = "[vl2]"
    if chapter:                                           # the chapter title: words fade up blurred -> sharp, then leave
        import titulek
        pat, tx, ty = titulek.frames(chapter, tmp / f"ch{idx}", K.CAP_FONTS / "anton.ttf", 150, CHAPTER_CARD,
                                     tuple(int(COL[z][i:i + 2], 16) for i in (0, 2, 4)), FPS, (W, H))
        ins.extend(["-framerate", str(FPS), "-i", pat])
        g.append(f"{vlast}[{n}:v]overlay=x={tx}:y={ty}:eof_action=pass[vch]")
        vlast, n = "[vch]", n + 1
    if sticker and main_end > 4.0:                        # our presenter reacts over the end of the moment: she
        t0 = max(0.5, (pk + 0.5) if pk is not None else 0.0, main_end - 1.75)     # slides up from the edge
        t1 = min(main_end, t0 + 1.7)                                              # and leaves the same way
        e = (f"if(lt(t,{t0 + 0.32:.2f}),1-pow(1-(t-{t0:.2f})/0.32,3),"
             f"if(gt(t,{t1 - 0.26:.2f}),1-pow(1-({t1:.2f}-t)/0.26,2),1))")
        ins.extend(["-c:v", "libvpx-vp9", "-i", str(sticker)])
        g.append(f"[{n}:v]scale=-2:600,setpts=PTS+{t0:.2f}/TB[ms];{vlast}[ms]overlay="
                 f"x={'60' if hits(1300, 500, 1920, 1080) else 'W-w-60'}:y='H-h*max(0,{e})':"
                 f"enable='between(t,{t0:.2f},{t1:.2f})':eof_action=pass[vms]")
        vlast, n = "[vms]", n + 1
    gain = level_gain(src, keep, t0, words)
    a = (f"[ac]volume={gain:.2f}dB,{TAME}"
         + (f",apad=whole_dur={length:.2f}" if hold else ""))
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
    elif chapter:
        sfx("hit.wav", 0.0, 0.4)                          # the chapter title lands
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
             f"afade=t=in:d=0.03,afade=t=out:st={max(0.0, length - 0.12):.2f}:d=0.12[aout]")
    run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error"] + ins +
        ["-filter_complex", ";".join(g), "-map", vlast, "-map", "[aout]", "-t", f"{length:.2f}"] + VOUT + venc + AOUT
        + [str(out)])
    level_fix(Path(out))
    return length

MUSIC_BASE = {"bananas": 0.22, "fanpage": 0.27}          # bed level against a -16 LUFS track (ducked under speech)

def block_of(i, n):
    """Music block of the i-th played moment out of n: blocks of 4-5 moments, the last one never tiny."""
    import hudba
    nb = max(1, math.ceil(n / hudba.BLOCK))                # 12 moments -> 3 blocks of 4, 17 -> 5 + 4 + 4 + 4
    return min(i * nb // max(n, 1), nb - 1), nb

def music_bed(video, out, z, plan, block_starts, boosts, stings, stop_at, tmp):
    """
    The music database bed (tools/long/hudba.py) under the whole video: a new track every block with a
    crossfade, the placement jingle on every tier segment, louder on boards / cards, rising towards the
    end, ducked under speech with a floor (never silent). False = no database for this brand.
    """
    import hudba
    if not plan:
        return False
    total = K.duration(video)
    bed = hudba.build_bed(plan, total, block_starts, tmp / "bed.wav", base=MUSIC_BASE[z], boosts=boosts, stings=stings,
                          sting_dur=TIER_ANIM, stop_at=stop_at)
    hudba.mix(video, bed, out, AOUT)
    Path(video).unlink(missing_ok=True)
    bed.unlink(missing_ok=True)
    return True

def music_pass(video, out, z, music, mstart, louder, vol=None, stop_at=None):
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
    fade = f"*if(gt(t,{stop_at:.2f}),max(0,1-(t-{stop_at:.2f})/0.6),1)" if stop_at else ""   # stop under the end card
    g = (f"[1:a]aresample=48000,aformat=channel_layouts=stereo,aloop=loop=-1:size=2147483647,atrim=0:{total:.2f},"
         f"volume='{vol}*(1+{boost}){fade}':eval=frame[mu];"
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
TIER_ANIM = 1.9

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

def tier_thumb(m, tmp):
    """
    The moment's payoff frame, small. The payoff itself can be a black "wasted" screen or a flash: of the
    frames around it the one with the most picture in it (contrast) is taken.
    """
    p = tmp / f"th_{m['rank']}.jpg"
    if not p.exists():
        t = m.get("peak") or (m["seg"][0] + m["seg"][1]) / 2
        best = None
        for i, dt in enumerate((0.0, -0.7, -1.5, 0.6)):
            c = tmp / f"th_{m['rank']}_{i}.jpg"
            try:
                run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", f"{max(m['seg'][0], t + dt):.2f}", "-i", m["source"],
                     "-frames:v", "1", "-vf", "scale=960:540:force_original_aspect_ratio=increase,crop=960:540", str(c)])
                import cv2
                g = cv2.imread(str(c), cv2.IMREAD_GRAYSCALE)
                score = float(g.std()) if g is not None else -1.0
            except Exception:
                score = -1.0
            if best is None or score > best[0] * (1.25 if i else 1.0):   # the payoff frame wins unless clearly emptier
                best = (score, c)
        if best and best[1].exists():
            best[1].replace(p)
        for c in tmp.glob(f"th_{m['rank']}_*.jpg"):
            c.unlink()
    return p

def tier_entry(m, tmp, seg=None):
    """Board entry of a moment; seg = its rendered segment: the placement starts from its very last frame."""
    e = {"tier": m["tier"], "thumb": str(tier_thumb(m, tmp)), "label": "@" + m["channel"]}
    if seg is not None:
        last = tmp / f"last_{m['rank']}.jpg"
        try:
            run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-sseof", "-0.25", "-i", str(seg), "-update", "1", "-q:v", "2",
                 str(last)])
            e["from"] = str(last)
        except RuntimeError:
            pass
    return e

def end_card(z, out, venc):
    """
    The brand's end card (tools/long/endcard.py, already at -16 LUFS: no separate normalising). The
    prebuilt assets/long/end_<brand>.mp4 is used when it fits (fanpage only while there is no 16:9 model
    outro); its picture is re-encoded with our encoder so the concat stays seamless, the sound is copied.
    """
    import endcard
    pre = HERE / "assets" / "long" / f"end_{z}.mp4"
    if pre.exists():                                       # rebuilt by `python tools/long/endcard.py` after any change
        run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", str(pre), "-map", "0:v", "-map", "0:a"]
            + VOUT + venc + ["-c:a", "copy", str(out)])
        return {"path": str(out), "dur": K.duration(out)}
    return (endcard.bananas if z == "bananas" else endcard.fanpage)(out, venc=venc)

def render_silent(m, out, z, rank, idx, tmp, venc):
    s, e = m["seg"]
    norm = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2"
    run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{s:.2f}", "-t", f"{e - s:.2f}",
         "-i", m["source"], "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-filter_complex",
         f"[0:v]{norm},fps={FPS},setsar=1,{overlays(tmp, z, m, rank, idx)}[v]", "-map", "[v]", "-map", "1:a",
         "-t", f"{e - s:.2f}"] + VOUT + venc + AOUT + [str(out)])

def render_cold_open(top3, out, z, tmp, venc):
    """
    ~6 s teaser of the best 3: <= 2 s each, the BUILD-UP into the payoff (it cuts away the instant the
    payoff starts), punched in 1.15x so it is a different shot than the moment later. Hard cuts, credits.
    Weakest of the three first, #1 last.
    """
    parts = []
    for i, m in enumerate(reversed(top3)):
        p = m.get("peak") or (m["seg"][0] + m["seg"][1]) / 2
        e = min(K.duration(m["source"]), p + 0.3)
        s = max(0.0, e - COLD_SHOT)
        mm = {**m, "seg": [s, e]}
        seg = tmp / f"cold_{i}.mp4"
        render_moment_plain(mm, seg, z, i, tmp, venc, punch=1.15)
        parts.append(seg)
    concat(parts, out)

def render_moment_plain(m, out, z, idx, tmp, venc, punch=1.0):
    s, e = m["seg"]
    src = m["source"]
    if not K.has_audio(src):
        return render_silent(m, out, z, None, f"o{idx}", tmp, venc)
    norm = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2"
    if punch > 1.0:                                        # slow push 1.0 -> punch over the shot
        norm += (f",scale=w='{W}*(1+{punch - 1:.3f}*(0.4+0.6*t/{e - s:.2f}))':h=-2:eval=frame,"
                 f"crop={W}:{H}:(iw-ow)/2:(ih-oh)/2")
    run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{s:.2f}", "-t", f"{e - s:.2f}",
         "-i", src, "-filter_complex",
         f"[0:v]{norm},fps={FPS},setsar=1,{overlays(tmp, z, m, None, f'o{idx}')}[v];"
         f"[0:a]aresample=48000,aformat=channel_layouts=stereo,volume={level_gain(src, [(s, e)], s):.2f}dB,"
         f"{TAME},apad=whole_dur={e - s:.2f},afade=t=in:d=0.04,afade=t=out:st={e - s - 0.06:.2f}:d=0.06[a]",
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

def model_sticker(seed):
    """One of our presenter's reactions (assets/model_reakce/*.webm, VP9 with alpha), or None when there is none."""
    clips = sorted(MODEL_REAKCE.glob("*.webm")) if MODEL_REAKCE.exists() else []
    return random.Random(seed).choice(clips) if clips else None

def build_story(a, picked, d, tmp):
    """
    Chronological story (fanpage): [chapter card "HOUR 2" / "11 PM"] -> moments of that chapter (no ranking,
    no on-screen numbers, cinematic grade, 2.35:1 bars sliding at the cuts) -> our model's reaction slides in
    over the end of the last moment of a chapter (if assets/model_reakce/ exists) -> ... -> the model's outro + Follow card. The chapter title sits over
    the first second of the running picture (no black card), a ~6 s cold open of the 3 strongest build-ups first. After Dark: every moment
    ends on a beat of the music. Returns (path, chapters, duration, info for qa_long).
    """
    z, serie = a.znacka, series_key(a.serie)
    venc = encoder()
    music, _, mstart = K.music_cfg(f"{d}:{z}:{serie}", 60, (K.load_json(K.BRANDS, {}).get(z) or {}).get("music"))
    mlen = K.duration(music) if music else 0.0
    m0 = (mstart + 7) % max(mlen - 12, 1.0) if music else 0.0
    import hudba
    story = sorted(picked, key=lambda m: ((m.get("video_id") or ""), m.get("vod_offset") or 0)
                   if serie == "streamer-night" else (m.get("created") or 0, m.get("vod_offset") or 0))
    for i, m in enumerate(story, 1):
        m["rank"] = i                                      # internal order only, never on screen
    parts, chapters, static, louder, t = [], [], [], [], 0.0
    info = {"moments": [], "board_entries": 0, "tiers": False}
    plan = hudba.Plan(z, f"{d}:{z}:{serie}", block_of(0, len(story))[1])
    block_starts, boosts, played = [0.0], [], 0
    beats = K.beat_grid(music, m0, 900) if (music and serie == "after-dark" and not plan) else None

    def add(path, kind):
        nonlocal t
        dur = K.duration(path)
        parts.append(path)
        if kind in ("card", "end"):
            static.append((t, t + dur))
        t += dur
        return dur

    cold = tmp / "cold.mp4"                                # the 3 strongest build-ups first: it starts in action
    render_cold_open(sorted(story, key=lambda m: -m.get("score", 0))[:3], cold, z, tmp, venc)
    add(cold, "cold")
    groups = []
    for m in story:
        lab = chapter_label(m, serie)
        if not groups or groups[-1][0] != lab:
            groups.append((lab, []))
        groups[-1][1].append(m)
    for gi, (lab, ms) in enumerate(groups):
        chapters.append((t, f"{lab} - {ms[0].get('name') or ms[0]['channel']}"))
        louder.append((t, t + CHAPTER_CARD))
        for mi, m in enumerate(ms):
            seg = tmp / f"m{m['rank']:02d}.mp4"
            cut_to = None
            bi = block_of(played, len(story))[0]
            if bi == len(block_starts):                    # a new music block starts with this moment
                block_starts.append(t)
            played += 1
            if plan and serie == "after-dark":             # the beat of the track playing in this block
                beats = plan.beats(bi, block_starts[bi])
            if beats:                                      # After Dark: the cut lands on a beat
                T, ph = beats
                natural = moment_len(m)
                end = t + natural
                k = math.floor((end - ph) / T)
                target = ph + k * T
                if target - t > 6.0:
                    cut_to = target - t
            log(f"  {lab} {m['channel']} {m['seg'][0]:.1f}-{m['seg'][1]:.1f} s" + (f" -> {cut_to:.2f} s on beat" if cut_to else ""))
            last_of_chapter = mi == len(ms) - 1 and gi < len(groups) - 1 and flag(z, "model_cards")
            render_moment(m, seg, z, None, m["rank"], tmp, venc, story=True, cut_to=cut_to,
                          chapter=lab if mi == 0 else None,   # the title over the running picture, no black card
                          sticker=model_sticker(f"{d}:{gi}") if last_of_chapter else None)
            m["at"] = round(t, 2)
            info["moments"].append({"rank": m["rank"], "channel": m["channel"], "at": t, "id": m["id"]})
            info.setdefault("lens", []).append(add(seg, "moment"))
    end = tmp / "outro.mp4"
    end_card(z, end, venc)
    stop = t
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
    if music_bed(raw, out, z, plan, block_starts, boosts, [], stop, tmp):
        info["music"] = plan.credits()
        log("  music: " + " -> ".join(b["title"] for b in plan.blocks[: len(block_starts)]))
    else:
        music_pass(raw, out, z, music, m0, louder, vol=MUSIC_VOL_STORY, stop_at=stop)
    info.update(static=static, parts_total=round(t, 2), n_moments=len(story), order=[])
    return out, chapters, K.duration(out), info

CHAPTER_CARD = 1.1                                        # s, the chapter title over the first moment of a chapter
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
    import hudba
    import zabava
    ranked = list(reversed(zabava.order_countdown(picked, lambda m: m.get("primary"))))   # best first, with variety
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
    plan = hudba.Plan(z, f"{d}:{z}:{a.serie}", block_of(0, len(order))[1])
    block_starts, boosts, stings = [0.0], [], []
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
    add(cold, "cold")                                      # ~6 s: too short for a chapter, it belongs to the first one
    louder = [(max(0.0, t - 0.6), t + 0.4)]
    if tiers:
        import tier
        cap = tier.capacity([m["tier"] for m in ranked])
        if flag(z, "tier_intro", False):                   # empty board after the cold open (off: 2 s of nothing)
            b0 = tmp / "board0.mp4"
            tier.render_hold([], b0, dur=1.2, brand=z, capacity=cap, venc=venc)
            louder.append((t, t + 1.2))
            boosts.append((t, t + 1.2, 2.2))
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
        chapters.append((t if chapters else 0, f"#{m['rank']} {m.get('name') or m['channel']}" + (f" - {title}" if title else "")))
        m["at"] = round(t, 2)
        louder.append((t, t + 0.7))
        if block_of(i, len(order))[0] == len(block_starts):   # a new music block starts with this moment
            block_starts.append(t)
        info["moments"].append({"rank": m["rank"], "channel": m["channel"], "at": t, "id": m["id"]})
        info.setdefault("lens", []).append(add(seg, "moment"))
        if tiers:
            stings.append((t, tier.LAND_FR * TIER_ANIM))
            tb = tmp / f"tier{i:02d}.mp4"                  # the moment is placed into its tier row
            tier.render_fly_in([tier_entry(p, tmp) for p in placed], tier_entry(m, tmp, seg), tb, dur=TIER_ANIM,
                               brand=z, capacity=cap, venc=venc,
                               teaser=TEASER.upper() if m["rank"] in teasers and m["rank"] != 1 else None)
            placed.append(m)
            louder.append((t, t + TIER_ANIM))
            add(tb, "board")
    info["board_entries"] = len(placed)
    merged = chapters[:1]
    for c in chapters[1:]:                                 # YouTube needs >= 10 s per chapter
        if c[0] - merged[-1][0] >= 10:
            merged.append(c)
    chapters = merged
    if tiers:
        full = tmp / "board_full.mp4"                      # the whole board before the end card
        tier.render_hold([tier_entry(p, tmp) for p in placed], full, dur=2.4, brand=z, capacity=cap, venc=venc,
                         zoom0=1 + tier.PUSH, focus=placed[-1]["tier"])       # the camera carries on from the last placement
        louder.append((t, t + 2.4))
        boosts.append((t, t + 2.4, 2.4))
        add(full, "board")
    end = tmp / "end.mp4"
    end_card(z, end, venc)
    stop = t                                               # the end card has its own sound bed: ours stops
    add(end, "end")
    out = OUT / z / f"{d}_{a.serie.replace(':', '-')}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    raw = tmp / "concat.mp4"
    concat(parts, raw)
    if music_bed(raw, out, z, plan, block_starts, boosts, stings if flag(z, "tier_sting") else [], stop, tmp):
        info["music"] = plan.credits()
        log("  music: " + " -> ".join(b["title"] for b in plan.blocks[: len(block_starts)])
            + (f" | sting: {plan.sting['title']}" if stings else ""))
    else:
        music_pass(raw, out, z, music, mpos(1), louder, stop_at=stop)
    info.update(static=static, parts_total=round(t, 2), n_moments=len(order), order=[m["rank"] for m in order])
    return out, chapters, K.duration(out), info

def moment_levels(out, info):
    """Level of the loud part (see loudness) of every moment inside the finished video, music and effects included."""
    lv = [loudness(out, m["at"], ln) for m, ln in zip(info["moments"], info.get("lens") or [])]
    return [x for x in lv if x is not None]

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
    lv = moment_levels(out, info)
    if lv:
        med = float(np.median(lv))
        log("  QA: moment levels " + " ".join(f"{x:.1f}" for x in lv) + f" LUFS (median {med:.1f})")
        info["levels"] = [round(x, 1) for x in lv]
        if max(abs(x - med) for x in lv) > 1.5:
            bad.append(f"moment loudness spread: {min(lv):.1f}..{max(lv):.1f} LUFS (median {med:.1f}, max +-1.5)")
    ids = [m.get("id") for m in info["moments"] if m.get("id")]
    if len(ids) != len(set(ids)):
        bad.append("the same moment twice")
    lens = info.get("lens") or []
    if lens:
        log(f"  QA: moments {len(lens)}, average {sum(lens) / len(lens):.1f} s, {min(lens):.1f}-{max(lens):.1f} s")
        if sum(lens) / len(lens) > 20.0:                   # target 12-20 s; shorter is fine (short sources)
            bad.append(f"average moment {sum(lens) / len(lens):.1f} s (> 20)")
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
                shutil.rmtree(f) if f.is_dir() else f.unlink()
            out, chapters, dur, info = build(a, picked, do, tmp, tiers=tiers)
            problems = qa_long(out, chapters, info, MIN_VIDEO)
            if not problems:
                break
            log(f"  QA failed ({'with' if tiers else 'without'} tier list): {'; '.join(problems)}")
    finally:
        for f in tmp.glob("*"):
            shutil.rmtree(f) if f.is_dir() else f.unlink()
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
        "music": info.get("music") or [],
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
