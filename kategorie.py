"""
kategorie.py - what a stream moment is about (long-form topic series), with evidence.

Every moment gets confidences 0..1 for 13 categories and a primary one:
  funny, rage, fail, physics, glitch, chase, crash, stunt, escape, easter-egg, mission,
  first-reaction, wow
Precision over quantity (Aleš 17. 11.: "a police chase must not be someone going shopping"):
a category counts as PROVEN only with >= 2 independent signals out of
  text    transcript + the viewer's clip title (police, cops, wanted, lost them / heist, mission /
          secret, easter egg / glitch, bug, clipping ...)
  audio   siren detector (a tone sweeping up and down in 500-1800 Hz), crash = a sharp transient,
          shouting = loud payoff over the moment's median
  motion  sustained high motion (>= 40 % of the moment) for chases/escapes, a spike for crashes/stunts
and themed videos additionally ask the vision model (verify(), kompilace.py) a yes/no question.
`fun` = entertainment score 0..1 (payoff loudness, laughter/hype words, motion, views): quiet
walking / shopping / menus stay out of every video.
"""

import math
import re
import subprocess
from datetime import date, datetime, timezone

import cv2
import numpy as np

CATS = ["funny", "rage", "fail", "physics", "glitch", "chase", "crash", "stunt", "escape", "easter-egg", "mission",
        "first-reaction", "wow"]
LAUNCH = date(2026, 11, 19)

TEXT = {
    "funny": r"\b(ha(ha)+h?|lol|lmao|lmfao|bruh|i can'?t|i'?m crying|dying|funny|hilarious|bro what)\b|💀|😂|🤣",
    "rage": r"\b(fuck\w*|shit|bitch|stupid|hate this|are you serious|why would|bro stop|i'?m so mad|trash)\b",
    "fail": r"\b(fail\w*|wasted|i died|we died|dead|nooo+|why did i|bro what|oops|my bad|missed|ruined|crashed it)\b",
    "physics": r"\b(physics|ragdoll|flew|flying|launched|yeet|bounc\w*|flipped|how did (he|i|that)|newton)\b",
    "glitch": r"\b(glitch\w*|bug|bugs|bugged|broken|clipping|clipped|stuck|t-?pose|floating|under the map|lagging)\b",
    "chase": r"\b(cops?|police|chase|chasing|wanted|stars?|pursuit|helicopter|heli|on my tail|pull over|they'?re behind)\b",
    "crash": r"\b(crash\w*|wreck\w*|smash\w*|total(l)?ed|explod\w*|exploded|boom|hit (me|him|her|us|it)|ran (him |her )?over|splat)\b",
    "stunt": r"\b(stunt|flip|front ?flip|backflip|jump(ed)?|ramp|air ?time|send it|full send|landed it|parkour)\b",
    "escape": r"\b(lost them|got away|escap\w*|we'?re clear|evad\w*|outran|no one'?s behind|hide|hiding|ditch)\b",
    "easter-egg": r"\b(easter egg|secret|hidden|ufo|alien|bigfoot|ghost|mystery|did you know|reference)\b",
    "mission": r"\b(mission|objective|heist|vault|robbery|rob(bing)?|bank|hostage|loot|safe|getaway driver|failed the)\b",
    "first-reaction": r"\b(vice city|first time|finally|gta ?(6|vi|six)|graphics|so real|looks? (so )?(good|real|insane)|beautiful|day (one|1))\b",
    "wow": r"\b(wow|insane|crazy|no way|oh my god|omg|holy|what the|clutch|let'?s go|yo+)\b",
}
LAUGH = r"\b(ha(ha)+h?|lol|lmao|lmfao|i'?m crying|dying)\b|💀|😂"

def audio_mono(src, sr=16000):
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", str(sr),
                          "-f", "s16le", "-"], capture_output=True, timeout=300).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0

def siren_score(x, sr=16000):
    """
    0..1: share of the moment with a police siren - a strong pure tone in 500-1800 Hz whose pitch
    sweeps up and down (wail / yelp), not a flat engine hum or speech.
    """
    n, hop = 2048, 512
    if x.size < sr:
        return 0.0
    frames = np.lib.stride_tricks.sliding_window_view(x, n)[::hop] * np.hanning(n)
    spec = np.abs(np.fft.rfft(frames, axis=1))
    freqs = np.fft.rfftfreq(n, 1 / sr)
    band = (freqs >= 500) & (freqs <= 1800)
    sb = spec[:, band]
    peak = sb.max(axis=1)
    # strict (17. 11. test: speech, music and gunfire passed a loose version): a very pure tone that
    # dominates the band in almost every frame, sweeping smoothly over >= 250 Hz, 2+ seconds in a row
    tonal = (peak / (sb.mean(axis=1) + 1e-9) > 15) & (sb.sum(axis=1) / (spec.sum(axis=1) + 1e-9) > 0.35)
    f0 = freqs[band][sb.argmax(axis=1)]
    win = int(1.0 * sr / hop)
    ok = []
    for i in range(0, len(f0) - win, win):
        t = tonal[i: i + win]
        f = f0[i: i + win][t]
        ok.append(bool(t.mean() > 0.8 and f.size > 5 and np.ptp(f) >= 250 and np.abs(np.diff(f)).mean() < 60))
    runs = sum(1 for j in range(1, len(ok)) if ok[j] and ok[j - 1])
    return round(min(1.0, runs / max(1, len(ok) - 1) * 2), 2)

def transient(x, sr=16000, peak=None):
    """Crash / explosion: the sharpest jump in loudness (10 ms) vs the median jump; bigger near the payoff."""
    hop = sr // 100
    if x.size < sr:
        return 0.0
    e = np.sqrt(np.convolve(x * x, np.ones(hop) / hop, mode="valid")[::hop] + 1e-12)
    d = np.maximum(0, np.diff(e))
    if peak is not None:
        k = int(peak * 100)
        d = d[max(0, k - 300): k + 150] if d.size > k else d
    return round(float(d.max() / (np.median(d[d > 0]) + 1e-6)) if d.size and (d > 0).any() else 0.0, 1)

def motion_series(src, fps=4):
    """Frame difference (tiny grey frames, game picture) at 4 fps."""
    out, prev = [], None
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(src), "-vf", f"fps={fps},scale=160:90,format=gray",
           "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True, timeout=300).stdout
    fr = np.frombuffer(raw, np.uint8)
    n = fr.size // (160 * 90)
    fr = fr[: n * 160 * 90].reshape(n, 90, 160).astype(np.float32)
    for i in range(1, n):
        out.append(float(np.abs(fr[i] - fr[i - 1]).mean()))
    return np.array(out)

def classify(words, src, peak=None, title="", created=0, views=0, a=None, b=None):
    """
    -> {"cats": {cat: conf}, "primary": cat or None, "proven": [cats with >= 2 signals],
        "evidence": {cat: [signals]}, "fun": 0..1, "signals": raw numbers}
    `a`, `b` limit the analysis to the cut (seconds in the source).
    """
    wsel = [w for w in words if (a is None or w[0] >= a) and (b is None or w[1] <= b)]
    text = " ".join(t for _, _, t in wsel).lower()
    tl = (title or "").lower()
    x = audio_mono(src)
    sr = 16000
    if a is not None:
        x = x[int(a * sr): int(b * sr) if b else None]
    pk = (peak - (a or 0)) if peak is not None else None
    siren = siren_score(x)
    trans = transient(x, peak=pk)
    rms = np.sqrt(np.convolve(x * x, np.ones(sr // 2) / (sr // 2), mode="valid")[:: sr // 10] + 1e-12) if x.size > sr else np.zeros(1)
    loud = float(np.percentile(rms, 98) / (np.median(rms) + 1e-6))
    mot = motion_series(src)
    if a is not None and mot.size:
        mot = mot[int(a * 4): int(b * 4) if b else None]
    mot_mean = float(mot.mean()) if mot.size else 0.0
    sustained = float((mot > 12).mean()) if mot.size else 0.0
    spike = float(mot.max() / (np.median(mot) + 1.0)) if mot.size else 0.0
    n = max(len(wsel), 1)
    hits = {c: len(re.findall(rx, text, re.I)) + 2 * len(re.findall(rx, tl, re.I)) for c, rx in TEXT.items()}
    laugh = len(re.findall(LAUGH, text, re.I))
    ev = {c: [] for c in CATS}
    for c in CATS:
        if hits[c] >= 1:
            ev[c].append("text")
    if siren >= 0.3:
        for c in ("chase", "escape"):
            ev[c].append("siren")
    if trans >= 60:
        for c in ("crash", "stunt", "fail", "physics"):
            ev[c].append("impact")
    if sustained >= 0.4:
        for c in ("chase", "escape", "stunt"):
            ev[c].append("motion")
    if spike >= 6:
        for c in ("crash", "stunt", "physics", "glitch"):
            ev[c].append("motion-spike")
    if loud >= 5:
        for c in ("rage", "funny", "wow", "fail"):
            ev[c].append("loud")
    if laugh >= 2:
        ev["funny"].append("laughter")
    if created and abs((datetime.fromtimestamp(created, tz=timezone.utc).date() - LAUNCH).days) <= 2:
        ev["first-reaction"].append("launch-days")
    for c in CATS:                                  # audio + motion of the same kind count once
        kinds = {"text" if s == "text" else "audio" if s in ("siren", "impact", "loud", "laughter")
                 else "motion" if s.startswith("motion") else s for s in ev[c]}
        ev[c] = sorted(set(ev[c]))
        ev[c + "#"] = len(kinds)
    cats = {}
    for c in CATS:
        k = ev.pop(c + "#")
        conf = min(1.0, 0.35 * min(hits[c], 3) / max(1.0, n / 60) ** 0.5 + 0.25 * (k - ("text" in ev[c])))
        if k >= 2:
            conf = max(conf, 0.6)
        cats[c] = round(conf, 2)
    # PROVEN = 2 independent kinds AND one of them is what was said / the viewer's title: sound and
    # picture alone fooled us on 17. 11. (a shootout looked like a chase, two people talking like physics)
    proven = [c for c in CATS if "text" in ev[c] and len({("text" if s == "text" else "audio" if s in
              ("siren", "impact", "loud", "laughter") else "motion" if s.startswith("motion") else s)
              for s in ev[c]}) >= 2]
    primary = max(cats, key=cats.get) if max(cats.values()) >= 0.3 else None
    # entertainment: loud payoff, laughter/hype, motion, views (log); quiet + still = 0
    fun = (0.35 * min(1.0, (loud - 2) / 6) + 0.2 * min(1.0, (laugh + hits["wow"] + hits["rage"]) / 4)
           + 0.25 * min(1.0, mot_mean / 18) + 0.2 * min(1.0, math.log10(max(views, 1)) / 5))
    if loud < 3 and mot_mean < 5:
        fun = min(fun, 0.15)
    static = static_share(src, a, b)
    if static is not None and static > STATIC_MAX:
        fun = 0.0                                   # not gameplay: never in a video
    import podobnost
    letterbox = podobnost.letterbox_share(src, a, b)
    if letterbox > 0.5:
        fun = 0.0                                   # cutscene: identical for everybody, and a spoiler risk
    if podobnost.canned_words(text + " " + tl):
        fun *= 0.7                                  # barber / shop / outfit / menu: soft penalty
    luma, dark = darkness(src, a, b)
    if dark > 0.5:
        fun = 0.0                                   # too dark to watch even after the lift: never in a video
    return {"cats": cats, "primary": primary, "proven": proven, "evidence": {c: ev[c] for c in CATS if ev[c]},
            "fun": round(max(0.0, fun), 2),
            "signals": {"siren": siren, "impact": trans, "loud": round(loud, 1), "motion": round(mot_mean, 1),
                        "sustained": round(sustained, 2), "spike": round(spike, 1), "laugh": laugh,
                        "static": static, "letterbox": letterbox, "luma": luma, "dark": dark}}

def primary_for(row, brand):
    """Never 'unsorted': no category -> funny (bananas) / wow (fanpage)."""
    return row.get("primary") or ("funny" if brand == "bananas" else "wow")

# vision yes/no per topic (asked on 2 frames side by side: before the payoff + the payoff)
QUESTION = {
    "chase": "Is a police car, police officer or police helicopter chasing someone in these frames?",
    "escape": "Is someone escaping from police (police cars or officers visible behind them) in these frames?",
    "crash": "Is a vehicle crashing, flipping, exploding or wrecked in these frames?",
    "stunt": "Is a vehicle or character doing a stunt (jump, flip, airborne) in these frames?",
    "glitch": "Does the game look glitched or broken (objects floating, clipping, impossible physics) in these frames?",
    "physics": "Is a character or vehicle flying, flipping or bouncing in an absurd, physics-defying way in these frames?",
    "mission": "Is a mission, heist, robbery, objective text or story cutscene visible in these frames?",
    "easter-egg": "Is something unusual or secret visible (a UFO, ghost, strange creature or hidden object) in these frames?",
    "fail": "Is someone failing badly (crashing, dying, falling, a plan going wrong) in these frames?",
    "first-reaction": "Is this a scenic or new-looking city view or character in a modern open-world game?",
}

def verify_frames(src, peak, out_jpg, seg=None):
    """
    4 frames in a 2x2 grid (1024x576): top = 1.5 s before the payoff and the payoff (topic check),
    bottom = start and 3/4 of the cut (is MOST of the moment gameplay, not a browser / award show).
    """
    def grab(t):
        raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{max(0.0, t):.2f}", "-i", str(src),
                              "-frames:v", "1", "-vf", "scale=512:288", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                             capture_output=True, timeout=60).stdout
        return np.frombuffer(raw, np.uint8).reshape(288, 512, 3) if len(raw) >= 512 * 288 * 3 else None
    p = peak if peak is not None else 2.0
    s0, s1 = seg if seg else (max(0.0, p - 8), p + 2)
    fr = [grab(p - 1.5), grab(p), grab(s0 + 0.5), grab(s0 + 0.75 * (s1 - s0))]
    if any(f is None for f in fr):
        return None
    cv2.imwrite(str(out_jpg), np.vstack([np.hstack(fr[:2]), np.hstack(fr[2:])]), [cv2.IMWRITE_JPEG_QUALITY, 88])
    return out_jpg

# ---------------------------------------------------------------------------
# multi-POV: the same mission / place seen by several streamers (series "Same Mission, N Streamers").
# NPC lines are identical for everybody, a streamer's own talk is not: moments of DIFFERENT streamers
# that share >= 2 word 5-grams of their transcripts are the same scene. Kicks in with GTA 6 data.

def _grams(words, n=5):
    toks = [re.sub(r"[^a-z']", "", t.lower()) for _, _, t in words]
    toks = [t for t in toks if t]
    return {" ".join(toks[i: i + n]) for i in range(len(toks) - n + 1)}

def same_mission(rows, min_shared=2, min_streamers=3):
    """rows with "words" (or out/_pool/<id>/meta.json) -> groups of moment ids, biggest first."""
    import json
    from pathlib import Path as _P
    pool_dir = _P(__file__).resolve().parent / "out" / "_pool"
    grams = {}
    for r in rows:
        w = r.get("words")
        if w is None:
            mf = pool_dir / r["id"] / "meta.json"
            w = json.loads(mf.read_text(encoding="utf-8")).get("words") if mf.exists() else []
        g = _grams([tuple(x) for x in w or []])
        if g:
            grams[r["id"]] = (r["channel"], g)
    parent = {k: k for k in grams}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k
    ids = list(grams)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if grams[a][0] != grams[b][0] and len(grams[a][1] & grams[b][1]) >= min_shared:
                parent[find(a)] = find(b)
    groups = {}
    for k in ids:
        groups.setdefault(find(k), set()).add(k)
    out = [g for g in groups.values() if len({grams[k][0] for k in g}) >= min_streamers]
    return sorted(out, key=len, reverse=True)

# ---------------------------------------------------------------------------
# gameplay pre-filter (no vision): a webpage, an award-show stream, "SIGNAL LOST", a menu or a loading
# screen is mostly STATIC; gameplay moves almost everywhere except the HUD, chat and facecam.
STATIC_MAX = 0.4                  # 17. 11.: gameplay ~0.05, a browser over the game 0.44

def static_share(src, a=None, b=None):
    """Share of the picture that does not change over the moment (1 fps, tiny grey frames)."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error"] + (["-ss", f"{a:.2f}", "-t", f"{(b or 0) - a:.2f}"] if a is not None and b else []) \
        + ["-i", str(src), "-vf", "fps=1,scale=160:90,format=gray", "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True, timeout=300).stdout
    n = len(raw) // (160 * 90)
    if n < 3:
        return None
    fr = np.frombuffer(raw[: n * 160 * 90], np.uint8).reshape(n, 90, 160).astype(np.float32)
    return round(float((fr.std(axis=0) < 4).mean()), 2)

GAMEPLAY_Q = ("Do ALL four frames show someone playing a 3D video game (not a menu, loading or black screen, "
              "webpage, browser window, award show or real-life camera)?")

# ---------------------------------------------------------------------------
# darkness (17. 11.: a night rooftop went almost black for 40 s under the cinematic grade)
DARK_LUMA = 35                    # mean luma (0-255) under which a frame counts as too dark ...
LIFT_GAMMA = 1.35                 # ... even after our shadow lift (eq gamma) for dark footage
DIM_LUMA = 70                     # below this mean the story grade lifts instead of darkening

def luma_series(src, a=None, b=None):
    """Mean luma (0-255) per frame at 1 fps."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error"] + (["-ss", f"{a:.2f}", "-t", f"{(b or 0) - a:.2f}"] if a is not None and b else []) \
        + ["-i", str(src), "-vf", "fps=1,scale=64:36,format=gray", "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True, timeout=300).stdout
    n = len(raw) // (64 * 36)
    return np.frombuffer(raw[: n * 64 * 36], np.uint8).reshape(n, 36, 64).mean(axis=(1, 2)) if n else np.zeros(0)

def lifted(l):
    return 255.0 * (np.asarray(l, np.float64) / 255.0) ** (1 / LIFT_GAMMA) + 6

def darkness(src, a=None, b=None):
    """(mean luma, share of frames still too dark after the lift)."""
    s = luma_series(src, a, b)
    if not s.size:
        return None, 0.0
    return round(float(s.mean()), 1), round(float((lifted(s) < DARK_LUMA).mean()), 2)
