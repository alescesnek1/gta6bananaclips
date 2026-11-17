"""
experimenty.py - experimental short formats next to the proven clipping (Aleš 17. 11., GTA 6 launch).

The classic shorts stay the base of both accounts. On top of them: at most ONE experiment per day
and account (brands.json exp_max_per_day), in its own slot that never counts toward the quota and
never replaces a classic clip. An experiment is switched on by DATA (enough strong material of the
right kind), never by date, and judged by numbers (Buffer post metrics 48 h after publishing).

Concepts (brands.json flags, false = off):
  fanpage  exp_multi_pov  "Same moment, 3 streamers": >= 3 streamers at the same mission / place with
                          DIFFERENT outcomes (podobnost.py: canned scenes and cutscenes out, each with a
                          strong reaction), 3 x ~7 s, whip cuts, @streamer on each
           exp_story      one streamer, >= 4 good moments of one stream, the 3 best in stream order, 20-35 s
  bananas  exp_first      "Who found it first": a glitch / easter egg found by >= 3 streamers, the earliest
                          clip (Helix created_at) first; small data label FIRST / +2h only (data exception)
           exp_tier3      3 moments of the day ~7 s each, then a 2 s 9:16 tier board (tools/long/tier.py)
Every segment passes the classic thresholds (virality >= good_score, entertainment >= FUN_MIN, the game
filter), the hook comes from the strongest payoff, at most one extra element is on screen at a time and
klipuj.qa_short checks the result. Anything missing or failing -> no experiment today (the classic
shorts are not touched either way).

Measurement (experimenty.json, committed by denne.yml):
  posts     "<brand>:<published key>" -> concept ('classic', 'top3' or the experiment), due, total views
            (views, else impressions, else reach per network) and engagement 48 h after publishing
  concepts  {"<brand>": {"<concept>": {"state": on|off|boost, "n", "ratio", "why", "at", "since"}}}
            after 5 measured posts: < 60 % of the classic average of the same days -> off (stays off; to
            switch it on again set "state": "on" and "since": "<ISO time>", only newer posts count),
            > 100 % -> boost = 2 a day. enabled() / per_day() read this file.

  python experimenty.py vyrob --znacka fanpage [--zkouska]   # today's experiment if the data allows
  python experimenty.py vyrob --znacka bananas --vynutit tier3 --hra "Grand Theft Auto V" \
         --vystup out/_ukazka/exp_tier3.mp4                  # sample from test material (never published)
  python experimenty.py mer [--zkouska] [--hned]             # 48 h stats + auto off / boost (1x per 20 h)
  python experimenty.py stav
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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
STATE = Path(os.environ.get("KLIPY_EXP") or HERE / "experimenty.json")
OUT = HERE / "out" / "_exp"
BRANDS = HERE / "brands.json"
SOURCES = HERE / "sources.json"
TZ = ZoneInfo("Europe/Prague")

CONCEPTS = ["multi_pov", "story", "first", "tier3"]
SEG = {"multi_pov": 7.0, "story": 9.0, "first": 7.0, "tier3": 7.0}   # seconds per segment
FUN_MIN = 0.35                     # entertainment floor (same as the long-form): quiet walking / shopping out
MIN_POSTS, OFF_RATIO, BOOST_RATIO = 5, 0.6, 1.0
MEASURE_H = 48
MAX_TRIES, TRY_GAP_H = 4, 1.3      # heavy attempts (downloads, Whisper, render) per brand and day
HOOK_LEN = 1.8
BOARD_DUR = 2.0
TIER_OF = [(9, "S"), (7, "A"), (5, "B"), (3, "C"), (0, "D")]       # banana score -> tier
FIND_CATS = {"glitch", "easter-egg"}
# words that say "glitch" but not WHICH one: they never link two moments ("found the same thing")
GENERIC = set("""glitch glitched glitches glitching bug bugs bugged broken stuck secret secrets hidden easter egg
eggs found find finding what this that there they them with have just like gonna yeah okay right look
oh my god bro dude chat guys what's it's that's there's game gta vice city real really actually going""".split())
STOP = set("""the and for you are was were lol bro yes now see got way guy let one two its out off use but not his her him she he its our out all can get got how why who
when then than from into over under about been being did does doing done more most some such only very
your yours mine ours their theirs here where would could should will shall may might must also because""".split())

def log(msg):
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    enc = sys.stdout.encoding or "utf-8"
    print(line.encode(enc, "replace").decode(enc), flush=True)

def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def save_json(path, data):
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)

def load_state():
    st = load_json(STATE, {})
    for k in ("posts", "concepts", "tries"):
        st.setdefault(k, {})
    return st

# -----------------------------------------------------------------------------
# switches: brands.json flag AND the measured state (experimenty.json)

def concept_state(brand, concept, st=None):
    st = st if st is not None else load_state()
    return ((st.get("concepts") or {}).get(brand) or {}).get(concept, {}).get("state", "on")

def enabled(brand, concept, cfg=None, st=None):
    cfg = cfg if cfg is not None else load_json(BRANDS, {})
    return bool((cfg.get(brand) or {}).get(f"exp_{concept}")) and concept_state(brand, concept, st) != "off"

def per_day(brand, concept, cfg=None, st=None):
    """Experiments a day for this brand when this concept is next: exp_max_per_day (1), 2 when it beats the classics."""
    cfg = cfg if cfg is not None else load_json(BRANDS, {})
    base = int((cfg.get(brand) or {}).get("exp_max_per_day", 1))
    return max(base, 2) if concept_state(brand, concept, st) == "boost" else base

# -----------------------------------------------------------------------------
# gating (pure: rows in, plan out; heavy checks through ctx["prepare"])

def virality(r, now):
    """Same formula as klipuj.virality / publikuj.candidates: views per softened hour of age, growth, cluster."""
    age_h = (now - r["created"]) / 3600 if r.get("created") else 24
    base = (r.get("views") or 0) / (max(age_h, 0) + 1) ** 0.5
    return base * (1 + min(r.get("growth") or 0, 3)) * (1 + 0.5 * ((r.get("cluster") or 1) - 1))

def good(r, ctx):
    """A segment must be as strong as a classic short: the game, not blocked, virality, entertainment."""
    if not re.search(ctx["game_rx"], r.get("category") or "", re.I):
        return False
    if (r.get("channel") or "").lower() in ctx.get("blocked", ()):
        return False
    if r.get("platform") == "youtube":             # a YouTube window's views are the whole video's
        return False
    if r.get("fun") is not None and r["fun"] < ctx.get("fun_min", FUN_MIN):
        return False
    return virality(r, ctx["now"]) >= ctx["good_score"]

def brand_ok(r, ctx):
    """Keep the accounts apart: nothing the other brand rendered / published."""
    other = ctx.get("other")
    rendered = r.get("rendered") or []
    if other and other in rendered and ctx["brand"] not in rendered:
        return False
    return r["id"] not in ctx.get("other_published", ())

def strong(rows, ctx, hours):
    t0 = ctx["now"] - (7 * 24 if ctx.get("force") else hours) * 3600     # samples: test material may be older
    out = [r for r in rows if (r.get("created") or 0) >= t0 and brand_ok(r, ctx) and good(r, ctx)]
    return sorted(out, key=lambda r: -virality(r, ctx["now"]))

def best_per_channel(rows, ctx):
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: -virality(r, ctx["now"])):
        if r["channel"] not in seen:
            seen.add(r["channel"])
            out.append(r)
    return out

def prepared(rows, ctx, limit=12):
    """Rows with source / words / payoff (heavy: download + Whisper) - only the `limit` strongest."""
    prep = ctx.get("prepare")
    if not prep:
        return rows
    out = []
    for r in rows[:limit]:
        try:
            x = prep(r)
        except Exception as e:                    # one broken clip never kills the experiment
            log(f"  {r['id']}: not usable ({str(e)[-100:]})")
            x = None
        if x and good(x, ctx):                    # preparing may reveal a weak reaction (fun): recheck
            out.append(x)
    return out

def gate_multi_pov(rows, ctx):
    """
    >= 3 streamers at the same anchor (shared NPC lines) whose outcomes differ, canned scenes and
    cutscenes out, each with a strong reaction (podobnost.refine_groups). 3 POVs, weakest first.
    """
    cand = strong(rows, ctx, 36)
    if len({r["channel"] for r in cand}) < 3:
        return None, f"{len({r['channel'] for r in cand})} streamers with a strong moment (< 3)"
    if not ctx.get("prepare"):
        return {"concept": "multi_pov", "items": best_per_channel(cand, ctx)[:3], "tentative": True}, "prefilter ok"
    cand = prepared(cand, ctx, ctx.get("limit", 12))
    groups = ctx.get("groups_fn", mission_groups)(cand)
    by_id = {r["id"]: r for r in cand}
    best = None
    for g in groups:
        ms = best_per_channel([by_id[i] for i in g if i in by_id], ctx)
        if len(ms) >= 3:
            score = sum(virality(m, ctx["now"]) for m in ms[:3])
            if not best or score > best[0]:
                best = (score, ms[:3])
    if not best:
        if ctx.get("force"):
            ms = best_per_channel(cand, ctx)[:3]
            if len(ms) == 3:
                return {"concept": "multi_pov", "items": sorted(ms, key=lambda r: virality(r, ctx["now"])), "forced": True}, "forced"
        return None, f"no same-moment group with 3 different outcomes ({len(groups)} groups)"
    items = sorted(best[1], key=lambda r: virality(r, ctx["now"]))          # build up to the strongest
    return {"concept": "multi_pov", "items": items}, "same moment, different outcomes"

def mission_groups(rows):
    import kategorie
    import podobnost
    rows = [r for r in rows if r.get("seg") and r.get("source")]
    return podobnost.refine_groups(kategorie.same_mission(rows), rows)

def stream_key(r):
    """Moments of one stream: the same VOD (Helix video_id), else the same channel within a 3 h gap chain."""
    return r.get("video_id") or None

def gate_story(rows, ctx):
    """One streamer, >= 4 good moments from ONE stream; the 3 best, in stream order, 20-35 s."""
    cand = strong(rows, ctx, 30)
    streams, chain = {}, {}
    for r in sorted(cand, key=lambda r: r.get("created") or 0):
        key = "all" if ctx.get("force") else stream_key(r)     # samples: any stream of the streamer
        if key is None:                                     # no VOD id: one stream while clips are < 3 h apart
            last = chain.get(r["channel"])
            if not last or (r.get("created") or 0) - last[1] >= 3 * 3600:
                last = [f"t{r.get('created') or 0}", 0]
            last[1] = r.get("created") or 0
            chain[r["channel"]] = last
            key = last[0]
        streams.setdefault((r["channel"], key), []).append(r)
    ok = []
    for (ch, key), ms in streams.items():
        uniq = []                                           # several viewers clipping one moment = one moment
        for m in sorted(ms, key=lambda r: -virality(r, ctx["now"])):
            if not any(same_moment(m, u) for u in uniq):
                uniq.append(m)
        if len(uniq) >= 4 or (ctx.get("force") and len(uniq) >= 3):
            ok.append((sum(virality(m, ctx["now"]) for m in uniq[:3]), uniq))
    if not ok:
        n = max((len(v) for v in streams.values()), default=0)
        return None, f"no stream with 4 good moments (best {n})"
    for _, uniq in sorted(ok, key=lambda x: -x[0]):         # strongest stream first; next one if unusable
        usable = prepared(uniq, ctx, 6) if ctx.get("prepare") else uniq
        top = usable[:3]
        if len(top) < 3:
            continue
        items = sorted(top, key=lambda r: (r.get("vod_offset") if r.get("vod_offset") is not None
                                           and all(x.get("vod_offset") is not None for x in top) else 0,
                                           r.get("created") or 0))
        return ({"concept": "story", "items": items, "moments": len(uniq), "spares": usable[3:]},
                f"{len(uniq)} good moments in one stream")
    return None, "story moments not usable (facecam / download)"

def same_moment(a, b):
    if a.get("video_id") and a.get("video_id") == b.get("video_id") and a.get("vod_offset") is not None \
            and b.get("vod_offset") is not None:
        return abs(a["vod_offset"] - b["vod_offset"]) < 30
    return abs((a.get("created") or 0) - (b.get("created") or 0)) < 45

def find_text(r):
    words = " ".join(w[2] for w in (r.get("words") or []) if len(w) > 2)
    return f"{r.get('title') or ''} {r.get('quote') or ''} {words}".lower()

def keywords(text):
    return {w for w in re.findall(r"[a-z][a-z']{2,}", text) if w not in GENERIC and w not in STOP}

def is_find(r):
    cats = r.get("cats") or {}
    return bool(FIND_CATS & set(r.get("proven") or []) or r.get("primary") in FIND_CATS
                or "glitch" in (r.get("tags") or []) or any(cats.get(c, 0) >= 0.5 for c in FIND_CATS))

def gate_first(rows, ctx):
    """
    A glitch / easter egg found by >= 3 streamers: moments with glitch / easter-egg evidence linked by
    a distinctive shared word (title, quote, speech) - and, when sources are on disk, the game picture
    must look alike (the same spot / thing). Earliest first; labels FIRST, +2h (data only).
    """
    cand = [r for r in strong(rows, ctx, 72) if is_find(r)]
    if len({r["channel"] for r in cand}) < 3 and not ctx.get("force"):
        return None, f"{len({r['channel'] for r in cand})} streamers with a glitch / easter egg (< 3)"
    if ctx.get("prepare"):                                   # (cheap mode: title + quote decide alone)
        cand = prepared(cand, ctx, ctx.get("limit", 12))
    kw ={r["id"]: keywords(find_text(r)) for r in cand}
    df = {}
    for ws in kw.values():
        for w in ws:
            df[w] = df.get(w, 0) + 1
    rare = lambda w: df[w] <= max(3, int(0.4 * len(cand))) or len(cand) <= 4
    best = None
    for w in sorted(df):
        if df[w] < 3 or not rare(w):
            continue
        ms = [r for r in cand if w in kw[r["id"]]]
        firsts = {}
        for r in sorted(ms, key=lambda r: r.get("created") or 0):
            firsts.setdefault(r["channel"], r)
        ms = sorted(firsts.values(), key=lambda r: r.get("created") or 0)
        if ctx.get("prepare") and ctx.get("visual", True):
            ms = [ms[0]] + [r for r in ms[1:] if looks_alike(ms[0], r)]
        if len(ms) >= 3:
            score = sum(virality(m, ctx["now"]) for m in ms[:3])
            if not best or score > best[0]:
                best = (score, ms[:3], w)
    if not best:
        if ctx.get("force"):
            pool = cand or prepared(best_per_channel(strong(rows, ctx, 72), ctx), ctx, 8)
            ms = sorted(best_per_channel(pool, ctx)[:3], key=lambda r: r.get("created") or 0)
            if len(ms) == 3:
                return {"concept": "first", "items": ms, "labels": first_labels(ms), "word": None, "forced": True}, "forced"
        return None, "no glitch / easter egg shared by 3 streamers"
    ms = best[1]
    return {"concept": "first", "items": ms, "labels": first_labels(ms), "word": best[2]}, f"'{best[2]}' found by {len(ms)}"

def first_labels(ms):
    out = ["FIRST"]
    t0 = ms[0].get("created") or 0
    for r in ms[1:]:
        d = max(0, (r.get("created") or 0) - t0)
        out.append(f"+{max(1, round(d / 60))}m" if d < 3600 * 0.75 else f"+{max(1, round(d / 3600))}h")
    return out

def looks_alike(a, b, th=0.55):
    """The same glitch spot: the game pictures (facecams masked) of the two moments resemble each other."""
    try:
        import podobnost
        return podobnost.seq_similarity(podobnost._frames(a), podobnost._frames(b)) >= th
    except Exception:
        return True                                         # no picture to compare: the word decides

def tier_of(score):
    return next(t for lo, t in TIER_OF if score >= lo)

def gate_tier3(rows, ctx):
    """3 moments of the day nobody posted yet (classic shorts and the Top 3 stay untouched), weakest first."""
    used = ctx.get("published", set())
    cand = [r for r in strong(rows, ctx, 24) if r["id"] not in used and not (r.get("rendered") or [])]
    ms = best_per_channel(cand, ctx)
    if len(ms) < 3:
        return None, f"{len(ms)} unposted strong moments of the day (< 3)"
    if ctx.get("prepare"):
        ms = prepared(ms, ctx, 8)
        if len(ms) < 3:
            return None, "moments not usable (facecam / download)"
    ms = ms[:3]
    ms = sorted(ms, key=lambda r: virality(r, ctx["now"]))
    rating = ctx.get("rating") or (lambda v: int(round(max(1, min(10, 1 + math.log10(max(v or 1, 1) / 50) * 2.25)))))
    return {"concept": "tier3", "items": ms, "tiers": [tier_of(rating(m.get("views"))) for m in ms]}, "3 moments of the day"

GATES = {"multi_pov": gate_multi_pov, "story": gate_story, "first": gate_first, "tier3": gate_tier3}

# -----------------------------------------------------------------------------
# measurement -> state

def post_views(metrics):
    """{network: {type: value}} -> (views, engagement): views, else impressions, else reach per network."""
    v = e = 0.0
    for m in metrics.values():
        v += m.get("views") or m.get("impressions") or m.get("reach") or 0
        e += sum(m.get(k) or 0 for k in ("reactions", "comments", "shares", "saves", "reposts", "quotes"))
    return v, e

def evaluate(st, now=None, cfg=None):
    """
    Per brand + experiment concept with >= MIN_POSTS measured posts (since its 'since'): ratio of its
    mean views to the mean of the brand's classic posts of the same days (+-2 days). < 60 % -> off
    (sticky), > 100 % -> boost (2 a day), else on. Returns the changes [(brand, concept, old, new, ratio)].
    """
    now = now or time.time()
    posts = list((st.get("posts") or {}).values())
    changes = []
    for brand in sorted({p["brand"] for p in posts}):
        for concept in sorted({p["concept"] for p in posts if p["brand"] == brand} - {"classic", "top3"}):
            ent = st.setdefault("concepts", {}).setdefault(brand, {}).setdefault(concept, {"state": "on"})
            since = ent.get("since")
            t_since = datetime.fromisoformat(since).timestamp() if since else 0
            exp = sorted((p for p in posts if p["brand"] == brand and p["concept"] == concept
                          and p["due_ts"] >= t_since), key=lambda p: p["due_ts"])
            ent["n"] = len(exp)
            if len(exp) < MIN_POSTS or ent.get("state") == "off":
                continue
            last = exp[-10:]
            lo, hi = last[0]["due_ts"] - 2 * 86400, last[-1]["due_ts"] + 2 * 86400
            cls = [p for p in posts if p["brand"] == brand and p["concept"] == "classic" and lo <= p["due_ts"] <= hi]
            if len(cls) < 3:
                cls = [p for p in posts if p["brand"] == brand and p["concept"] == "classic"]
            if len(cls) < 3:
                ent["why"] = "too few measured classic posts to compare"
                continue
            ce = sum(p["views"] for p in cls) / len(cls)
            xe = sum(p["views"] for p in last) / len(last)
            ratio = round(xe / max(ce, 1.0), 2)
            new = "off" if ratio < OFF_RATIO else "boost" if ratio > BOOST_RATIO else "on"
            old = ent.get("state", "on")
            ent.update(state=new, ratio=ratio, at=datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
                       why=f"{len(last)} posts avg {xe:.0f} views vs classic {ce:.0f} ({len(cls)} posts)")
            if new != old:
                changes.append((brand, concept, old, new, ratio))
    return changes

def cmd_mer(a):
    """Buffer Post.metrics of every post 48 h after it went out -> experimenty.json, then evaluate()."""
    st = load_state()
    now = time.time()
    if not a.hned and now - st.get("measured_ts", 0) < 20 * 3600:
        log(f"measured {((now - st['measured_ts']) / 3600):.1f} h ago, next run later")
        return
    import publikuj as P
    P.load_env()
    cfg = load_json(BRANDS, {})
    n_new = 0
    for brand in [k for k, v in cfg.items() if isinstance(v, dict)]:
        P.set_brand(brand)
        chans = {c["id"]: c for c in load_json(P.CHANNELS, [])}
        done = load_json(P.PUBLISHED, {})
        todo = []
        for key, rec in done.items():
            if rec.get("draft") or not rec.get("posts") or f"{brand}:{key}" in st["posts"]:
                continue
            try:
                due = datetime.fromisoformat(rec.get("due") or "").timestamp()
            except ValueError:
                continue
            if not MEASURE_H * 3600 <= now - due <= 8 * 86400:
                continue
            todo.append((key, rec, due))
        if not todo:
            continue
        by_key = {}
        for key, rec, due in todo:
            for ch_id, pid in rec["posts"].items():
                ch = chans.get(ch_id) or {}
                by_key.setdefault(ch.get("key") or "BUFFER_API_KEY_1", []).append((key, ch.get("service", "?").lower(), pid))
        metrics, incomplete = {}, set()
        for bkey, items in by_key.items():
            if not os.environ.get(bkey):
                log(f"{brand}: {bkey} missing, {len(items)} posts not measured")
                continue
            for i in range(0, len(items), 20):              # one request per 20 posts (aliases)
                chunk = items[i:i + 20]
                q = "query { " + " ".join(
                    f'p{j}: post(input: {{id: {json.dumps(pid)}}}) {{ status metrics {{ type value }} }}'
                    for j, (_, _, pid) in enumerate(chunk)) + " }"
                if a.zkouska:
                    log(f"  {brand} {bkey}: {len(chunk)} posts  {q[:160]}...")
                    continue
                try:
                    data = P.gql(q, bkey)
                except Exception as e:
                    log(f"  {brand}: Buffer metrics failed ({str(e)[-160:]})")
                    incomplete.update(k for k, _, _ in chunk)
                    continue
                for j, (key, service, _) in enumerate(chunk):
                    p = data.get(f"p{j}") or {}
                    if p.get("status") != "sent" or not p.get("metrics"):
                        incomplete.add(key)                 # not out yet / no numbers yet: measure next run
                        continue
                    metrics.setdefault(key, {})[service] = {m["type"]: m["value"] for m in p["metrics"]}
        for key, rec, due in todo:
            if key not in metrics or key in incomplete or len(metrics[key]) < len(rec["posts"]):
                continue                                    # only complete measurements count
            v, e = post_views(metrics[key])
            concept = rec.get("concept") or ("top3" if key.startswith("top3_") else "classic")
            st["posts"][f"{brand}:{key}"] = {"brand": brand, "concept": concept, "due": rec.get("due"), "due_ts": due,
                                            "age_h": round((now - due) / 3600, 1), "views": v, "eng": e,
                                            "nets": metrics[key]}
            n_new += 1
    if a.zkouska:
        return
    for brand, concept, old, new, ratio in evaluate(st, now, cfg):
        log(f"{brand} {concept}: {old} -> {new} (ratio {ratio})")
        if new == "off":
            print(f"::warning::Experiment {brand}/{concept} vypnut: {ratio * 100:.0f} % klasickych klipu")
    st["measured_ts"] = now
    st["measured_at"] = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")
    save_json(STATE, st)
    log(f"measured {n_new} new posts (Buffer); state -> {STATE.name}")

def cmd_stav(a):
    st = load_state()
    cfg = load_json(BRANDS, {})
    for brand in [k for k, v in cfg.items() if isinstance(v, dict)]:
        for c in CONCEPTS:
            if f"exp_{c}" in cfg[brand]:
                ent = (st["concepts"].get(brand) or {}).get(c, {})
                n = sum(1 for p in st["posts"].values() if p["brand"] == brand and p["concept"] == c)
                log(f"{brand:8} {c:10} flag={cfg[brand][f'exp_{c}']!s:5} state={ent.get('state', 'on'):5} "
                    f"measured={n} ratio={ent.get('ratio', '-')} {ent.get('why', '')}")
    log(f"classic measured: {sum(1 for p in st['posts'].values() if p['concept'] == 'classic')}, "
        f"last measurement {st.get('measured_at', 'never')}")

# -----------------------------------------------------------------------------
# render: each segment in the brand's look (klipuj pipeline), then hook + whip cuts + labels / board

def window(r, length):
    """Segment around the payoff: ~70 % before it (the build-up), ~30 % after (the reaction)."""
    dur = r.get("dur") or 0
    p = r.get("peak")
    if p is None:
        a0, b0 = r.get("cut") or (0, dur)
        p = (a0 + b0) / 2
    a = max(0.0, p - 0.7 * length)
    b = a + length
    if dur and b > dur:
        b, a = dur, max(0.0, dur - length)
    return round(a, 2), round(b, 2)

def render_part(r, a, b, bc, d, i):
    """One segment [a, b] of the 16:9 source -> 9:16 in the brand's look (facecam, tracked game, captions,
    watermarks, @streamer). Returns the part dict or (None, why)."""
    import klipuj as K
    seg = d / f"src{i}.mp4"
    K.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", f"{a:.3f}", "-i", str(r["source"]), "-t", f"{b - a:.3f}",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(seg)],
          timeout=300)
    dur = K.duration(seg)
    fan = bc["key"] == "fanpage"
    cam_h = 720 if fan else K.CAM_H
    game_h = K.H - cam_h
    words = [(s - a, e - a, t) for s, e, t in (r.get("words") or []) if s >= a and e <= b]
    words, mute = K.mask_swears(words)
    ass = d / f"captions{i}.ass"
    cap = bc.get("caption") or {}
    if fan:
        K.captions_ass(words, ass, y=cam_h, cap={**cap, "outline": 3, "shadow": 0}, per=1, anim="bounce")
    else:
        K.captions_ass(words, ass, y=cam_h, cap=cap or None)
    mark = d / f"mark{i}.txt"
    mark.write_text(f"@{r['channel']}", encoding="utf-8")
    vol = "+".join(f"between(t\\,{s:.2f}\\,{e:.2f})" for s, e in mute)
    out = d / f"part{i}.mp4"
    orig = Path(r["source"])
    probs = ["no facecam"]
    # the same repair passes as klipuj.render: saved box -> wider detection + loose bars + 6 % zoom ->
    # content box (big black areas cut off). No facecam = no segment (every short has the face on top).
    for mode, loose, zoom in (("saved", False, 0.0), ("again", True, 0.06), ("content", True, 0.03)):
        cam = (K.get_cam(r["channel"], orig) if mode == "saved" else K.detect_cam(orig, mult=3.4) if mode == "again"
               else K.get_cam(r["channel"], orig) or K.detect_cam(orig, mult=3.4))
        if not cam:
            continue
        xlim = None
        if mode == "content":
            x0, y0, x1, y1 = K.content_box(seg)
            top, bot, xlim = y0, K.SH - y1, (x0, x1)
        else:
            top, bot = K.game_bars(seg, loose=loose)
        bot = max(bot, K.subtitle_band(seg, top, bot))
        crop_h = K.SH - top - bot
        cut = int(crop_h * zoom / 2)
        top, crop_h = top + cut, crop_h - 2 * cut
        crop_w = round(K.W * crop_h / game_h)
        xs, lo, hi, _ = K.track_path(seg, cam, crop_w, dur, xlim)
        if bc.get("crop_shift"):
            xs = K.shift_path(xs, float(bc["crop_shift"]) * crop_w, lo, hi)
        cmds = d / f"track{i}.cmd"
        cmds.write_text("".join(f"{k / K.FPS:.4f} crop@g x {x};\n" for k, x in enumerate(xs)), encoding="utf-8")
        flt = K.build_filter(cam, K.ff_path(cmds), int(xs[0]), crop_w, game_h, crop_h, top, ass_file=K.ff_path(ass),
                             mark_file=K.ff_path(mark), nf=K.src_norm(seg),
                             grade=",".join(x for x in (bc.get("grade"), K.FAN_LOOK if fan else "") if x),
                             wm_text=bc.get("watermark") or K.BRAND, cam_h=cam_h,
                             push=[0.0, round(dur, 2)] if fan else None)
        flt += ";[0:a]aresample=48000" + (f",volume=0:enable='{vol}'" if vol else "") + "[pa]"
        K.encode(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", str(seg),
                  "-filter_complex", flt, "-map", "[v]", "-map", "[pa]"], out)
        probs = K.quality_check(out, cam, cam_h)
        if not probs:
            break
        log(f"  segment {r['channel']}: {'; '.join(probs)} ({mode}) -> next repair")
    if probs:
        return None, f"quality: {'; '.join(probs)}"
    peak = (r.get("peak") - a) if r.get("peak") is not None else dur * 0.7
    return {"file": out, "dur": K.duration(out), "cam_h": cam_h, "peak": min(max(0.3, peak), dur - 0.3),
            "words": words, "row": r, "size": int(cap.get("size", K.CAP_SIZE))}, "ok"

def thumb(part, d, i):
    """Square still of the game band at the payoff (for the tier board)."""
    from PIL import Image
    jpg = d / f"thumb{i}.jpg"
    y = part["cam_h"] + 60
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", f"{part['peak']:.2f}", "-i", str(part["file"]),
                    "-frames:v", "1", "-vf", f"crop=1080:1080:0:{y}", str(jpg)], check=True, timeout=60)
    return Image.open(jpg).copy()

def assemble(parts, hook, labels, board, bc, out, seed):
    """
    hook (strongest payoff) -> brand transition (bananas white flash + whoosh, fanpage whip + swish) ->
    the segments with whip cuts -> (tier board). Our music under everything, one limiter at the end.
    Returns (total, joins, label boxes [(t0, t1, name, box)], cap_times).
    """
    import klipuj as K
    pieces = [(hook["file"], hook["start"], hook["len"])] + [(p["file"], 0.0, p["dur"]) for p in parts]
    if board:
        pieces.append((board, 0.0, BOARD_DUR))
    ins, flt, starts, t = [], "", [], 0.0
    for j, (f, s, ln) in enumerate(pieces):
        ins += ["-ss", f"{s:.3f}", "-t", f"{ln:.3f}", "-i", str(f)]
        flt += (f"[{j}:v]fps={K.FPS},scale={K.W}:{K.H},setsar=1,format=yuv420p,setpts=PTS-STARTPTS[v{j}];"
                f"[{j}:a]aresample=48000,aformat=channel_layouts=stereo,asetpts=PTS-STARTPTS[a{j}];")
        starts.append(t)
        t += ln
    total = t
    n = len(pieces)
    flt += "".join(f"[v{j}][a{j}]" for j in range(n)) + f"concat=n={n}:v=1:a=1[cv][ca];"
    joins = starts[1:]
    hook_end = joins[0]
    fan = bc["key"] == "fanpage"
    whip = joins if fan else joins[1:]                       # bananas: the hook ends in its white flash
    z = "+".join(f"between(it\\,{T:.3f}\\,{T + 0.2:.3f})*({T + 0.2:.3f}-it)/0.2" for T in whip)
    v = "[cv]"
    if z:
        flt += f"[cv]zoompan=z='1+0.15*({z})':d=1:x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':s={K.W}x{K.H}:fps={K.FPS}[cz];"
        v = "[cz]"
    chain = []
    if not fan:                                              # the classic bananas hook flash
        T = hook_end
        chain.append(f"eq=eval=frame:brightness='0.85*if(lt(t\\,{T:.3f})\\,clip((t-{T - 0.35:.3f})/0.35\\,0\\,1)"
                     f"\\,clip(1-(t-{T + 0.1:.3f})/0.4\\,0\\,1))'")
    boxes = []
    font = K.ff_path(HERE / "fonts" / "cap" / "montserrat-900.ttf")
    for k, text in enumerate(labels or []):
        if not text:
            continue
        t0, t1 = starts[1 + k] + 0.15, starts[1 + k] + 1.75
        y = parts[k]["cam_h"] + 150
        col = "0xFFE135" if text == "FIRST" else "white"
        chain.append(f"drawtext=fontfile='{font}':text='{text}':fontsize=64:fontcolor={col}:box=1:boxcolor=black@0.55:"
                     f"boxborderw=18:x=40:y={y}:enable='between(t\\,{t0:.2f}\\,{t1:.2f})'")
        boxes.append((t0, t1, f"label {text}", (22, y - 18, 22 + 60 + 44 * len(text), y + 64 + 18)))
    flt += f"{v}{','.join(chain) if chain else 'null'}[v];"
    # sound: the clips + our music at the brand's steady level + transition sounds, one limiter
    aud, idx, mix = "[ca]", n, []
    mpath, mvol, mstart = K.music_cfg(seed, total, bc.get("music"))
    mvol = float(bc.get("music_volume", mvol))
    if mpath:
        ins += ["-ss", f"{mstart:.2f}", "-i", str(mpath)]
        flt += (f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{total:.3f},asetpts=PTS-STARTPTS,"
                f"volume={mvol:.3f},afade=t=out:st={max(0.0, total - 0.6):.3f}:d=0.6[mu];")
        mix.append("[mu]")
        idx += 1
    sfx = [(HERE / "assets" / "sfx" / ("whip.wav" if fan else "flash.wav"), max(0.0, hook_end - (0.12 if fan else 0.45)), 0.7)]
    sfx += [(HERE / "assets" / "sfx" / "whip.wav", max(0.0, T - 0.12), 0.45) for T in joins[1:]]
    for k, (f, at, vol) in enumerate(sfx):
        if not f.exists():
            continue
        ins += ["-i", str(f)]
        ms = int(at * 1000)
        flt += f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={ms}|{ms},volume={vol}[s{k}];"
        mix.append(f"[s{k}]")
        idx += 1
    flt += (f"{aud}{''.join(mix)}amix=inputs={1 + len(mix)}:normalize=0:duration=first,"
            f"alimiter=limit=0.7:level=0[aud]")
    K.encode(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error"] + ins +
             ["-filter_complex", flt, "-map", "[v]", "-map", "[aud]", "-t", f"{total:.3f}"], out)
    caps = []
    for k, p in enumerate(parts):
        caps += [(starts[1 + k] + w0, starts[1 + k] + w1 + 0.6) for w0, w1, _ in p["words"]]
    caps += [(w0 - hook["start"], w1 - hook["start"] + 0.6) for w0, w1, _ in hook["part"]["words"]
             if hook["start"] <= w0 < hook["start"] + hook["len"]]
    return total, starts, boxes, caps

def build(plan, brand, day, dest=None, tmp_keep=False, suffix=""):
    """Plan -> finished experiment short + meta.json in out/_exp/<brand>/<day>_<concept>/. None on any failure."""
    import klipuj as K
    concept = plan["concept"]
    cfg = K.brands()
    bc = K.brand_cfg(cfg, brand) if brand in cfg else {**K.DEFAULT_BRAND, "key": brand}
    d = OUT / brand / f"{day:%Y%m%d}_{concept}{suffix}"
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    parts = []
    queue, spares, i = list(plan["items"]), list(plan.get("spares") or []), 0
    while queue:
        r = queue.pop(0)
        a, b = window(r, SEG[concept])
        try:
            part, why = render_part(r, a, b, bc, d, i)
        except K.NotGameplay as e:
            part, why = None, str(e)
        except Exception as e:
            part, why = None, f"render failed ({str(e)[-160:]})"
        i += 1
        if not part:
            if spares:                                      # story: another good moment of the same stream
                log(f"  {concept}: segment {r['channel']}/{r['id']} {why} -> spare moment")
                queue.append(spares.pop(0))
                continue
            log(f"  {concept}: segment {r['channel']}/{r['id']} {why} -> no experiment")
            return None
        parts.append(part)
    if concept == "story":                                  # stream order (a spare may have come in late)
        parts.sort(key=lambda p: (p["row"].get("vod_offset") or 0, p["row"].get("created") or 0)
                   if len({p["row"].get("video_id") for p in parts}) == 1 else (0, p["row"].get("created") or 0))
    if concept == "story":
        total_est = sum(p["dur"] for p in parts) + HOOK_LEN
        if not 20 <= total_est <= 35:
            log(f"  story: {total_est:.0f} s outside 20-35 s -> no experiment")
            return None
    strongest = max(parts, key=lambda p: (p["row"].get("views") or 0))
    hs = K.loud_start(strongest["file"], strongest["peak"])
    hl = min(HOOK_LEN, strongest["dur"] - hs)
    if hl < 1.0:
        log(f"  {concept}: no room for a hook -> no experiment")
        return None
    hook = {"file": strongest["file"], "start": hs, "len": hl, "part": strongest}
    board = None
    if concept == "tier3":
        sys.path.insert(0, str(HERE / "tools" / "long"))
        import tier
        entries = [{"tier": t, "thumb": thumb(p, d, i), "label": "@" + p["row"]["channel"]}
                   for i, (p, t) in enumerate(zip(parts, plan["tiers"]))]
        cap = tier.capacity([e["tier"] for e in entries])
        board = tier.render_fly_in(entries[:-1], entries[-1], d / "board.mp4", dur=BOARD_DUR, size=(K.W, K.H),
                                   brand=brand, capacity=cap, venc=["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"])
    out = Path(dest) if dest else d / "exp.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        total, starts, boxes, caps = assemble(parts, hook, plan.get("labels"), board, bc, out, f"{day}:{brand}:{concept}")
    except Exception as e:
        log(f"  {concept}: assembly failed ({str(e)[-200:]}) -> no experiment")
        return None
    main_end = total - (BOARD_DUR if board else 0.0)
    cam_h = parts[0]["cam_h"]
    size = parts[0]["size"]
    elements = list(boxes)
    if board:
        elements.append((main_end, total, "tier board", (0, 0, K.W, K.H)))
    qplan = dict(total=total, main_end=main_end, card_at=main_end, card=None, heat_on=False, heat=False, loud_hook=True,
                 elements=sorted(elements), face_bottom=cam_h, cap_times=caps,
                 cap_box=(40, cam_h - int(size * 0.65), 1040, cam_h + int(size * 0.65)))
    probs = K.qa_short(out, qplan) if bc.get("qa", True) else []
    if probs:
        log(f"  {concept}: QA {'; '.join(probs)} -> no experiment")
        if not dest:
            out.unlink(missing_ok=True)
        return None
    items = [p["row"] for p in parts]
    pk = hook["part"]["peak"] - hs
    meta = {
        "id": f"exp_{concept}_{day:%Y%m%d}{suffix}", "concept": concept, "brand": brand, "platform": "exp", "channel": "exp",
        "file": str(out), "duration": round(total, 2), "peak": round(min(max(0.5, pk), hl), 2),
        "category": "Grand Theft Auto VI" if all(re.search(load_json(SOURCES, {}).get("game", "GTA ?6"),
                                                           r.get("category") or "", re.I) for r in items) else
                    " / ".join(sorted({r.get("category") or "?" for r in items})),
        "items": [{"id": r["id"], "channel": r["channel"], "platform": r.get("platform") or "twitch",
                   "views": r.get("views"), "created": r.get("created")} for r in items],
        "labels": plan.get("labels"), "tiers": plan.get("tiers"), "word": plan.get("word"),
        "forced": bool(plan.get("forced")), "starts": [round(s, 2) for s in starts],
        "rendered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "views": sum(r.get("views") or 0 for r in items),
    }
    save_json(d / "meta.json", meta)
    if not tmp_keep:
        for f in list(d.glob("src*.mp4")) + list(d.glob("part*.mp4")) + list(d.glob("track*.cmd")) \
                + list(d.glob("captions*.ass")) + list(d.glob("mark*.txt")) + list(d.glob("thumb*.jpg")) + [d / "board.mp4"]:
            f.unlink(missing_ok=True)
    log(f"  {concept}: done -> {out} ({total:.1f} s, {', '.join(r['channel'] for r in items)})")
    return meta

# -----------------------------------------------------------------------------
# vyrob: today's experiment for one brand

def published_sets(brand):
    cfg = load_json(BRANDS, {})
    mine, other = set(), set()
    for k, v in cfg.items():
        if isinstance(v, dict):
            pub = load_json(HERE / v.get("published", "publikovano.json"), {})
            ids = set(pub) | {i for rec in pub.values() for i in (rec.get("items") or [])}   # clips inside experiments
            (mine if k == brand else other).update(ids)
    return mine, other

def exps_today(brand, day):
    import publikuj as P
    P.set_brand(brand)
    return sum(1 for k, rec in load_json(P.PUBLISHED, {}).items() if k.startswith("exp_") and P.in_day(rec, day))

def waiting(brand, day):
    """Rendered experiments of this day not published yet."""
    import publikuj as P
    P.set_brand(brand)
    done = load_json(P.PUBLISHED, {})
    out = []
    for mf in sorted((OUT / brand).glob(f"{day:%Y%m%d}_*/meta.json")):
        m = load_json(mf, {})
        if m and not m.get("forced") and m.get("id") not in done and Path(m.get("file", "")).exists():
            out.append(m)
    return out

def make_ctx(brand, a, now, heavy):
    import klipuj as K
    src = load_json(SOURCES, {})
    mine, other_pub = published_sets(brand)
    cfg = load_json(BRANDS, {})
    names = [k for k, v in cfg.items() if isinstance(v, dict)]
    ctx = {"brand": brand, "now": now, "good_score": src.get("good_score", 1000), "fun_min": FUN_MIN,
           "game_rx": a.hra or src.get("game", "Grand Theft Auto VI"), "blocked": {b.lower() for b in src.get("blocklist", [])},
           "other": next((n for n in names if n != brand), None), "other_published": other_pub,
           "published": mine | other_pub, "rating": K.banana_rating, "force": bool(a.vynutit), "limit": 12}
    if a.vynutit:
        ctx["good_score"], ctx["fun_min"] = 0, 0.0
    if heavy:
        import kompilace as KP

        def prepare(r):
            r = KP.ready(dict(r))
            if not r or not r.get("source") or not Path(r["source"]).exists():
                return None
            KP.moment_words(r)
            r.setdefault("dur", K.duration(r["source"]))
            r.setdefault("seg", list(KP.segment_times(r)))
            r["cam"] = K.get_cam(r["channel"], Path(r["source"]))      # every segment needs the face on top
            return r if r["cam"] else None
        ctx["prepare"] = prepare
    return ctx

def cmd_vyrob(a):
    import klipuj as K
    now = time.time()
    day = date.fromisoformat(a.den) if a.den else datetime.now(TZ).date()
    brand = a.znacka
    cfg = load_json(BRANDS, {})
    st = load_state()
    concepts = [a.vynutit] if a.vynutit else [c for c in CONCEPTS if enabled(brand, c, cfg, st)]
    if not concepts:
        log(f"{brand}: no experiment switched on")
        return
    if not a.vynutit:
        if waiting(brand, day):
            log(f"{brand}: today's experiment is rendered and waits for its slot")
            return
        n_today = exps_today(brand, day)
        concepts = [c for c in concepts if n_today < per_day(brand, c, cfg, st)]
        if not concepts:
            log(f"{brand}: {n_today} experiment(s) today = the limit")
            return
        import publikuj as P
        P.set_brand(brand)
        if P.budget_level() >= 1:
            log(f"{brand}: Netlify budget tight, no experiment today")
            return
        taken = {rec.get("slot") for k, rec in load_json(P.PUBLISHED, {}).items() if k.startswith("exp_")}
        soon = datetime.now(TZ) + timedelta(minutes=45)        # render + deploy before the slot
        if not any(t > soon and b.isoformat() not in taken for b, t in P.exp_slots(day)):
            log(f"{brand}: no experiment slot left today")
            return
    # concept with the fewest posts first (5 measured posts each as soon as possible)
    counts = {c: sum(1 for p in st["posts"].values() if p["brand"] == brand and p["concept"] == c) for c in concepts}
    concepts.sort(key=lambda c: (counts[c], CONCEPTS.index(c)))
    rows = list(K.pool_load().values())
    cheap = make_ctx(brand, a, now, heavy=False)
    tries = st["tries"].get(brand) or {}
    if tries.get("day") != str(day):
        tries = {"day": str(day), "n": 0, "last": 0}
    for c in concepts:
        plan, why = GATES[c](rows, cheap)
        log(f"{brand} {c}: {'candidate' if plan else 'no'} ({why})")
        if not plan:
            continue
        if a.zkouska:
            log("   " + ", ".join(f"{r['channel']}:{r['id']} ({r.get('views')} views)" for r in plan["items"]))
            continue
        if not a.vynutit and tries["last"] != now and (tries["n"] >= MAX_TRIES or now - tries["last"] < TRY_GAP_H * 3600):
            log(f"{brand}: {tries['n']} heavy attempts today, the last {(now - tries['last']) / 60:.0f} min ago - later")
            return
        if not a.vynutit and tries["last"] != now:          # one attempt = one run (it may try every concept)
            tries.update(n=tries["n"] + 1, last=now)
            st["tries"][brand] = tries
            save_json(STATE, st)
        plan, why = GATES[c](rows, make_ctx(brand, a, now, heavy=True))
        log(f"{brand} {c}: {'GO' if plan else 'no'} ({why})")
        if plan and a.vynutit:
            plan["forced"] = True                           # a sample: waiting() never hands it to publikuj
        mine, _ = published_sets(brand)
        k = sum(1 for i in mine if i.startswith(f"exp_{c}_{day:%Y%m%d}"))
        if plan and build(plan, brand, day, dest=a.vystup, tmp_keep=a.nech, suffix=f"_{k + 1}" if k else ""):
            return
    if not a.zkouska:
        log(f"{brand}: no experiment today")

def preview(brand, day, hra=None):
    """Cheap report for `publikuj.py den --zkouska`: rendered ones waiting + which concepts the data allows."""
    out = [f"exp {brand}: waiting {m['concept']} ({', '.join(x['channel'] for x in m['items'])})" for m in waiting(brand, day)]
    try:
        import klipuj as K
        st, cfg = load_state(), load_json(BRANDS, {})
        ns = argparse.Namespace(hra=hra, vynutit=None)
        ctx = make_ctx(brand, ns, time.time(), heavy=False)
        rows = list(K.pool_load().values())
        for c in CONCEPTS:
            if f"exp_{c}" not in (cfg.get(brand) or {}):
                continue
            if not enabled(brand, c, cfg, st):
                out.append(f"exp {brand} {c}: off ({concept_state(brand, c, st)})")
                continue
            plan, why = GATES[c](rows, ctx)
            out.append(f"exp {brand} {c}: {'candidate' if plan else 'no'} - {why} (max {per_day(brand, c, cfg, st)}/day)")
    except Exception as e:
        out.append(f"exp {brand}: preview failed ({str(e)[-120:]})")
    return out

def main():
    p = argparse.ArgumentParser(description="experimental short formats (data-gated, measured)")
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("vyrob", help="render today's experiment of a brand if the data allows it")
    v.add_argument("--znacka", default="bananas")
    v.add_argument("--den", help="YYYY-MM-DD (default today, Prague)")
    v.add_argument("--zkouska", action="store_true", help="only the cheap gate report, no download / render")
    v.add_argument("--vynutit", choices=CONCEPTS, help="sample: this concept, relaxed data gates (never published)")
    v.add_argument("--hra", help="category regex instead of sources.json game (test material: 'Grand Theft Auto V')")
    v.add_argument("--vystup", help="write the video here (samples: out/_ukazka/exp_<concept>.mp4)")
    v.add_argument("--whisper", default="small.en")
    v.add_argument("--nech", action="store_true", help="keep the intermediate files")
    v.set_defaults(fn=cmd_vyrob)
    m = sub.add_parser("mer", help="48 h Buffer stats of every post -> experimenty.json, auto off / boost")
    m.add_argument("--zkouska", action="store_true", help="print the queries, write nothing")
    m.add_argument("--hned", action="store_true", help="ignore the once-per-20-h throttle")
    m.set_defaults(fn=cmd_mer)
    s = sub.add_parser("stav", help="switches, measured posts, ratios")
    s.set_defaults(fn=cmd_stav)
    a = p.parse_args()
    if getattr(a, "whisper", None):
        import klipuj as K
        K.WHISPER_MODEL = a.whisper
    a.fn(a)

if __name__ == "__main__":
    main()
