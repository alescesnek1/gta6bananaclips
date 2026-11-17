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

SERIES = {   # tag weights; "need" = the moment must carry one of these tags
    "funny-day": {"w": {"funny": 1.0, "rage": 0.6, "wow": 0.4, "glitch": 0.4, "crash": 0.3}},
    "wild-day": {"w": {"crash": 1.0, "chase": 0.9, "wow": 0.7, "rage": 0.6, "glitch": 0.6, "first-reaction": 0.5}},
    "glitches": {"w": {"glitch": 1.0, "crash": 0.3, "wow": 0.2}, "need": {"glitch"}},
    "chases": {"w": {"chase": 1.0, "crash": 0.9, "wow": 0.3}, "need": {"chase", "crash"}},
    "top-week": {"w": None},       # the brand's daily weights
    "streamer": {"w": {t: 0.5 for t in K.TAG_WORDS}},
}
DAILY = {"bananas": "funny-day", "fanpage": "wild-day"}

def log(msg):
    K.log(msg)

def run(cmd, timeout=1800):
    return K.run(cmd, timeout=timeout)

# -----------------------------------------------------------------------------
# texts that go out (English; SlopMonster lint 5/5 on the templates)

def day_n(d):
    return (d - LAUNCH).days + 1

def names(moments, n=3):
    seen = []
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
}

def fmt_t(s):
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"

def description(z, serie, d, moments, chapters):
    key = "streamer" if serie.startswith("streamer:") else serie
    day = f"day {day_n(d)}" if d >= LAUNCH else d.strftime("%B %d").replace(" 0", " ")
    intro = INTRO[key].format(day=day, n=len(moments), name=moments[0].get("name") or moments[0]["channel"])
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
    When to publish (Prague): daily series ~2 h after the day closes (US evening), weekly on Sunday
    evening; each brand its own time, moved -20..+20 min per day, never :00 / :30 (like shorts).
    """
    now = now or datetime.now(TZ)
    hh, mm = {("bananas", 0): (1, 40), ("fanpage", 0): (3, 10),
              ("bananas", 1): (19, 40), ("fanpage", 1): (21, 10)}[(z, int(serie == "top-week"))]
    base_day = d + timedelta(days=1) if serie != "top-week" else d
    t = datetime(base_day.year, base_day.month, base_day.day, hh, mm, tzinfo=TZ)
    rnd = random.Random(f"{d}:{z}:{serie}:long")
    t += timedelta(minutes=rnd.randint(-20, 20))
    if t < now + timedelta(minutes=45):
        t = now + timedelta(minutes=45 + rnd.randint(0, 25))
    while t.minute in (0, 30):
        t += timedelta(minutes=1)
    return t.replace(second=0, microsecond=0)

# -----------------------------------------------------------------------------
# selection

def window(a):
    today = datetime.now(TZ).date()
    if a.od or a.do:
        od = date.fromisoformat(a.od or a.do)
        do = date.fromisoformat(a.do or a.od)
    elif a.serie == "top-week":
        od, do = today - timedelta(days=7), today - timedelta(days=1)
    else:
        od = do = today - timedelta(days=1)
    t0 = datetime(od.year, od.month, od.day, tzinfo=TZ).timestamp()
    t1 = datetime(do.year, do.month, do.day, tzinfo=TZ).timestamp() + 86400
    return od, do, t0, t1

def candidates(a, t0, t1):
    rows = {r["id"]: dict(r) for r in K.pool_load().values()}
    game = a.hra or K.load_json(K.SOURCES, {}).get("twitch_game", "Grand Theft Auto VI")
    if all(K.helix_keys()):
        try:
            for c in K.game_clips(game, t0, t1, pages=a.pages):
                rows.setdefault(c["id"], c).update(views=max(c["views"], rows[c["id"]].get("views") or 0))
        except Exception as e:
            log(f"twitch api: {str(e)[-150:]}")
    game_rx = re.escape(a.hra) if a.hra else K.game_re()
    blocked = {b.lower() for b in K.load_json(K.SOURCES, {}).get("blocklist", [])}
    used = K.load_json(USED, {})
    other = "fanpage" if a.znacka == "bananas" else "bananas"
    out = []
    for r in rows.values():
        if not (t0 <= (r.get("created") or 0) < t1) or r.get("channel", "").lower() in blocked:
            continue
        if not re.search(game_rx, r.get("category") or "", re.I):
            continue
        u = used.get(r["id"])
        if u and (u["znacka"] != a.znacka or a.serie != "top-week"):
            continue                                       # the other brand's, or already in our daily
        if other in (r.get("rendered") or []) and a.znacka not in (r.get("rendered") or []):
            continue                                       # the other brand's short: keep the accounts apart
        if a.serie.startswith("streamer:") and r["channel"] != a.serie.split(":", 1)[1].lower():
            continue
        out.append(r)
    return out

def weights(a):
    key = "streamer" if a.serie.startswith("streamer:") else a.serie
    w = SERIES[key]["w"] or SERIES[DAILY[a.znacka]]["w"]
    return w, SERIES[key].get("need")

def tag_match(r, w):
    if r.get("tag_scores"):
        return sum(w.get(t, 0) * v for t, v in r["tag_scores"].items() if not t.startswith("_"))
    tl = (r.get("title") or "").lower()                    # not tagged yet: the viewer's title only
    return sum(wt * 0.6 for t, wt in w.items() if re.search(K.TAG_WORDS[t], tl, re.I))

def score(r, w):
    return math.log10((r.get("views") or 0) + 10) * (0.6 + min(tag_match(r, w), 1.5))

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

def pick(a, cands):
    w, need = weights(a)
    per_max = 99 if a.serie.startswith("streamer:") else a.max_na_streamera
    cands = [c for c in cands if not need or (c.get("tags") is None or need & set(c.get("tags") or []))]
    cands.sort(key=lambda r: score(r, w), reverse=True)
    picked, per, total = [], {}, 0.0
    extra = SWEEP if a.znacka == "fanpage" else CARD
    for r in cands:
        if len(picked) >= a.max_momentu or (total >= a.delka and len(picked) >= a.min_momentu):
            break
        if per.get(r["channel"], 0) >= per_max:
            continue
        try:
            r = ready(r)
        except K.NotGameplay as e:
            log(f"  skip {r['id']}: {e}")
            continue
        except Exception as e:
            log(f"  skip {r['id']}: {str(e)[-150:]}")
            continue
        if not r or (need and not need & set(r.get("tags") or [])):
            continue
        s, e = segment_times(r)
        r.update(seg=[s, e], score=round(score(r, w), 3))
        picked.append(r)
        per[r["channel"]] = per.get(r["channel"], 0) + 1
        total += e - s + extra
    return picked

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

def render_moment(m, out, z, rank, idx, tmp, venc, music, mstart):
    """One moment, 16:9, normalised loudness, corner texts; bananas: + score card over its tail."""
    s, e = m["seg"]
    src = m["source"]
    if not K.has_audio(src):
        return render_silent(m, out, z, rank, idx, tmp, venc)   # rare: clip without sound
    dur_src = K.duration(src)
    norm = K.src_norm(src).replace(f"scale={K.SW}:{K.SH}:force_original_aspect_ratio=increase,crop={K.SW}:{K.SH}",
                                   f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2")
    card = z == "bananas" and (K.RATING / f"score_{K.banana_rating(m.get('views'))}.webm").exists()
    tail = CARD if card else 0.0
    e2 = min(dur_src, e + tail)
    pad = max(0.0, e + tail - e2)                          # source too short: hold the last frame
    length = e - s + tail
    ins = ["-ss", f"{s:.2f}", "-t", f"{e2 - s:.2f}", "-i", str(src)]
    v = (f"[0:v]{norm},fps={FPS},setsar=1" + (f",tpad=stop_mode=clone:stop_duration={pad:.2f}" if pad else "")
         + f",{overlays(tmp, z, m, rank, idx)}[v0]")
    a = (f"[0:a]aresample=48000,aformat=channel_layouts=stereo,loudnorm=I=-16:TP=-1.5:LRA=11,"
         f"apad=whole_dur={length:.2f},afade=t=in:d=0.05,afade=t=out:st={length - 0.2:.2f}:d=0.2[a0]")
    graph, vlast, alast, n = [v, a], "[v0]", "[a0]", 1
    if card:
        ins += ["-c:v", "libvpx-vp9", "-i", str(K.RATING / f"score_{K.banana_rating(m.get('views'))}.webm")]
        at = e - s
        graph.append(f"[{n}:v]scale=-2:560,setpts=PTS+{at:.2f}/TB[cv];{vlast}[cv]overlay=x=W-w-40:y=H-h-40:eof_action=pass[v1]")
        graph.append(f"[{n}:a]adelay={int(at * 1000)}:all=1,volume=0.45[ca]")
        vlast, n = "[v1]", n + 1
        mixes = [alast, "[ca]"]
        if music:                                          # quiet music under the card only
            ins += ["-ss", f"{mstart:.2f}", "-i", str(music)]
            graph.append(f"[{n}:a]atrim=0:{tail:.2f},asetpts=PTS-STARTPTS,volume=0.14,afade=t=in:d=0.3,"
                         f"afade=t=out:st={tail - 0.5:.2f}:d=0.5,adelay={int(at * 1000)}:all=1[mu]")
            mixes.append("[mu]")
            n += 1
        graph.append(f"{''.join(mixes)}amix=inputs={len(mixes)}:duration=first:normalize=0[a1]")
        alast = "[a1]"
    run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error"] + ins +
        ["-filter_complex", ";".join(graph), "-map", vlast, "-map", alast, "-t", f"{length:.2f}"] + VOUT + venc + AOUT + [str(out)])

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
    """~15 s teaser: the payoff (+-) of the best 3, hard cuts, credits, no numbers."""
    parts = []
    for i, m in enumerate(top3):
        p = m.get("peak") or (m["seg"][0] + m["seg"][1]) / 2
        s = max(0.0, p - 3.5)
        e = min(K.duration(m["source"]), p + 1.5)
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

def build(a, picked, d, tmp):
    """Cold open + countdown (#N .. #1) + end slate -> one mp4. Returns (path, chapters, duration)."""
    z = a.znacka
    venc = encoder()
    music, _, mstart = K.music_cfg(f"{d}:{z}:{a.serie}", 60, (K.load_json(K.BRANDS, {}).get(z) or {}).get("music"))
    mlen = K.duration(music) if music else 0.0
    mpos = lambda i: (mstart + 7 * i) % max(mlen - 12, 1.0)    # every transition another bar of the track
    ranked = sorted(picked, key=lambda m: -m["score"])     # best first
    for i, m in enumerate(ranked, 1):
        m["rank"] = i
    order = list(reversed(ranked))                         # countdown: weakest first, #1 last
    parts, chapters, t = [], [], 0.0
    cold = tmp / "cold.mp4"
    render_cold_open(ranked[:3], cold, z, tmp, venc)
    parts.append(cold)
    chapters.append((0, "Best moments preview"))
    t += K.duration(cold)
    sweep = tmp / "sweep.mp4"
    if z == "fanpage":
        render_sweep(sweep, z, venc, music, mstart)
        parts.append(sweep)
        t += K.duration(sweep)
    for i, m in enumerate(order):
        seg = tmp / f"m{i:02d}.mp4"
        log(f"  #{m['rank']} {m['channel']} {m['seg'][0]:.1f}-{m['seg'][1]:.1f} s ({', '.join(m.get('tags') or []) or '-'})")
        render_moment(m, seg, z, m["rank"], i, tmp, venc, music, mpos(i + 1))
        title = re.sub(r"\s+", " ", m.get("title") or "").strip()[:50]
        chapters.append((t, f"#{m['rank']} {m.get('name') or m['channel']}" + (f" - {title}" if title else "")))
        m["at"] = round(t, 2)
        parts.append(seg)
        t += K.duration(seg)
        if z == "fanpage" and i < len(order) - 1:
            parts.append(sweep)
            t += K.duration(sweep)
    end = tmp / "end.mp4"
    render_end(end, z, venc, music, mstart, tmp)
    parts.append(end)
    out = OUT / z / f"{d}_{a.serie.replace(':', '-')}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    concat(parts, out)
    return out, chapters, K.duration(out)

# -----------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="long-form YouTube compilations (16:9) from stream moments")
    p.add_argument("--znacka", required=True, choices=["bananas", "fanpage"])
    p.add_argument("--serie", default=None, help="funny-day, wild-day, glitches, chases, top-week, streamer:<login>")
    p.add_argument("--od", help="first day (YYYY-MM-DD, Prague)")
    p.add_argument("--do", help="last day (YYYY-MM-DD, Prague)")
    p.add_argument("--delka", type=float, default=660, help="target length of the moments (s)")
    p.add_argument("--min-delka", type=float, default=480, help="shorter than this: no video (s)")
    p.add_argument("--min-momentu", type=int, default=20)
    p.add_argument("--max-momentu", type=int, default=40)
    p.add_argument("--max-na-streamera", type=int, default=4)
    p.add_argument("--hra", help="Twitch category instead of GTA VI (test: 'Grand Theft Auto V')")
    p.add_argument("--pages", type=int, default=3, help="Twitch API pages of 100 clips")
    p.add_argument("--whisper", default="small.en")
    p.add_argument("--zkouska", action="store_true", help="plan only: pick moments, print title/chapters guess")
    p.add_argument("--bez-nahledu", action="store_true")
    p.add_argument("--out", help="also copy the video here (e.g. out/_ukazka/long_bananas.mp4)")
    a = p.parse_args()
    a.serie = a.serie or DAILY[a.znacka]
    if a.serie not in SERIES and not a.serie.startswith("streamer:"):
        sys.exit(f"unknown series {a.serie}")
    K.WHISPER_MODEL = a.whisper
    od, do, t0, t1 = window(a)
    log(f"{a.znacka} {a.serie}: moments {od} .. {do}")
    cands = candidates(a, t0, t1)
    log(f"  {len(cands)} candidates")
    picked = pick(a, cands)
    total = sum(m["seg"][1] - m["seg"][0] for m in picked)
    log(f"  picked {len(picked)} moments, {total / 60:.1f} min of moments")
    if total < a.min_delka or len(picked) < 3:
        print(f"::warning::Long-form {a.znacka} {a.serie}: malo momentu ({len(picked)}, {total / 60:.1f} min) - video se nedela.")
        return 0
    if a.zkouska:
        for m in sorted(picked, key=lambda m: -m["score"]):
            log(f"  {m['score']:.2f} {m['channel']} {m.get('views')} views {m.get('tags')} '{(m.get('title') or '')[:40]}'")
        return 0
    tmp = OUT / f"_tmp_{a.znacka}_{os.getpid()}"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        out, chapters, dur = build(a, picked, do, tmp)
    finally:
        for f in tmp.glob("*"):
            f.unlink()
        tmp.rmdir()
    moments = sorted(picked, key=lambda m: m["rank"])
    slot = suggested_slot(a.znacka, a.serie, do)
    meta = {
        "znacka": a.znacka, "kanal": "gta6bananaclips" if a.znacka == "bananas" else "gta6fanpageclip",
        "serie": a.serie, "datum": str(do), "od": str(od), "do": str(do),
        "title": title_for(a.znacka, a.serie, do, moments),
        "description": description(a.znacka, a.serie, do, moments, chapters),
        "tags": yt_tags(a.znacka, a.serie, moments), "video": str(out), "duration_s": round(dur, 1),
        "nahrat": slot.isoformat(), "playlist": a.serie if not a.serie.startswith("streamer:") else "streamers",
        "moments": [{k: m.get(k) for k in ("id", "rank", "channel", "name", "url", "title", "views", "tags", "peak",
                                           "seg", "at", "source", "platform")} for m in moments],
        "made_at": datetime.now(TZ).isoformat(timespec="seconds"),
    }
    mp = out.with_name(out.name.replace(".mp4", ".meta.json"))
    mp.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"  -> {out} ({dur / 60:.1f} min), '{meta['title']}', upload {slot:%Y-%m-%d %H:%M}")
    used = K.load_json(USED, {})                          # shared: the other brand never takes these
    for m in moments:
        used.setdefault(m["id"], {"znacka": a.znacka, "serie": a.serie, "datum": str(do)})
    cutoff = str(do - timedelta(days=30))
    K.save_json(USED, {k: v for k, v in used.items() if v.get("datum", "") >= cutoff})
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
