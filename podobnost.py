"""
podobnost.py - is a "shared moment" really a moment? (reusable for long-form and shorts)

Aleš 17. 11.: a shared place is not a shared interesting moment. Canned animations and scenes
(barber, shop, clothing store, tattoo, sitting down, entering a building, cutscenes, menus, loading)
are identical for every player, NPC lines included - matching on shared lines finds them first.

  canned(a, b)            the GAME picture (facecam masked) of two moments is near-identical -> canned
  letterbox_share(src)    share of the moment with cinematic black bars -> cutscene (and spoiler risk)
  canned_words(text)      barber, haircut, clothes, outfit, tattoo, shop, store, menu, loading, cutscene
  outcome_differs(a, b)   after the shared anchor the two moments go different ways (picture + motion)
  refine_groups(groups, rows)  multi-POV groups -> only those with >= 3 streamers whose shared anchor
                          leads to DIFFERENT outcomes, each with a strong reaction; canned pairs out
A moment = dict with "source", "seg" [a, b] (s), "cam" (facecam box in %, optional), "channel",
"words" (optional), "fun" (optional).
"""

import re
import subprocess

import numpy as np

CANNED_RE = re.compile(r"\b(barber|haircut|hair ?cut|clothes|clothing|outfit|tattoo|shop|store|menu|loading|cutscene)\b", re.I)
SIM_CANNED = 0.86                 # mean pHash similarity of aligned game frames above this = the same canned scene
SIM_DIFFERENT = 0.70              # after the anchor below this = the outcome differs
FW, FH = 64, 36

def game_frames(src, a=None, b=None, cam=None, fps=2):
    """Tiny grey frames of the GAME picture: the facecam box (percent) is blanked so faces don't count."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error"]
    if a is not None and b:
        cmd += ["-ss", f"{a:.2f}", "-t", f"{max(0.5, b - a):.2f}"]
    cmd += ["-i", str(src), "-vf", f"fps={fps},scale={FW}:{FH},format=gray", "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True, timeout=300).stdout
    n = len(raw) // (FW * FH)
    fr = np.frombuffer(raw[: n * FW * FH], np.uint8).reshape(n, FH, FW).astype(np.float32).copy()
    if cam and n:
        x, y, w, h = [int(round(v / 100 * d)) for v, d in zip(cam, (FW, FH, FW, FH))]
        fr[:, max(0, y - 1): y + h + 1, max(0, x - 1): x + w + 1] = 0
    return fr

def phash(img):
    """64-bit perceptual hash (DCT of a 32x32 resample, top-left 8x8 vs its median)."""
    import cv2
    d = cv2.dct(cv2.resize(img, (32, 32), interpolation=cv2.INTER_AREA))[:8, :8].flatten()
    return d > np.median(d[1:])

def seq_similarity(fa, fb):
    """Each frame of `fa` matched to its best frame in `fb` (order-free alignment) -> mean similarity 0..1."""
    if not len(fa) or not len(fb):
        return 0.0
    ha, hb = [phash(f) for f in fa], [phash(f) for f in fb]
    best = [max(1 - np.count_nonzero(x != y) / 64 for y in hb) for x in ha]
    return float(np.mean(best))

def _frames(m, a=None, b=None):
    s0, s1 = m.get("seg") or (None, None)
    return game_frames(m["source"], a if a is not None else s0, b if b is not None else s1, m.get("cam"))

def canned(ma, mb):
    """(is canned, similarity): the game parts of two moments of DIFFERENT streamers look the same."""
    sim = seq_similarity(_frames(ma), _frames(mb))
    return sim >= SIM_CANNED, round(sim, 3)

def letterbox_share(src, a=None, b=None):
    """Share of frames with black bars over >= 9 % at the top AND the bottom (cinematic cutscene)."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error"] + (["-ss", f"{a:.2f}", "-t", f"{b - a:.2f}"] if a is not None and b else []) \
        + ["-i", str(src), "-vf", "fps=1,scale=160:90,format=gray", "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True, timeout=300).stdout
    n = len(raw) // (160 * 90)
    if not n:
        return 0.0
    fr = np.frombuffer(raw[: n * 160 * 90], np.uint8).reshape(n, 90, 160).astype(np.float32)
    k = 8                                              # 9 % of 90 rows
    top, bot = fr[:, :k, :].mean(axis=(1, 2)), fr[:, -k:, :].mean(axis=(1, 2))
    mid = fr[:, k:-k, :].mean(axis=(1, 2))
    bars = (top < 14) & (bot < 14) & (mid > 30)
    return round(float(bars.mean()), 2)

def canned_words(text):
    return len(CANNED_RE.findall(text or ""))

def anchor_time(ma, mb):
    """Time (s, in each source) of the first shared word 5-gram - the shared anchor (NPC line)."""
    def grams(m):
        w = [(t0, re.sub(r"[^a-z']", "", t.lower())) for t0, _, t in (m.get("words") or [])]
        w = [(t0, t) for t0, t in w if t]
        return {" ".join(t for _, t in w[i: i + 5]): w[i][0] for i in range(len(w) - 4)}
    ga, gb = grams(ma), grams(mb)
    common = set(ga) & set(gb)
    if not common:
        return None
    g = min(common, key=lambda x: ga[x])
    return ga[g], gb[g]

def outcome_differs(ma, mb):
    """After the shared anchor the pictures diverge (low similarity) and the motion differs."""
    at = anchor_time(ma, mb)
    if at is None:
        return False
    (ta, tb) = at
    ea, eb = (ma.get("seg") or (0, ta + 8))[1], (mb.get("seg") or (0, tb + 8))[1]
    fa, fb = _frames(ma, ta + 1.0, max(ta + 3, ea)), _frames(mb, tb + 1.0, max(tb + 3, eb))
    if len(fa) < 3 or len(fb) < 3:
        return False
    sim = seq_similarity(fa, fb)
    mot = lambda f: float(np.abs(np.diff(f, axis=0)).mean())
    ma_, mb_ = mot(fa), mot(fb)
    return sim < SIM_DIFFERENT and (abs(ma_ - mb_) / max(ma_, mb_, 1e-6) > 0.25 or sim < 0.55)

def refine_groups(groups, rows, min_streamers=3, min_fun=0.45):
    """
    Multi-POV groups (same anchor) -> only real "same mission, different outcome" groups: members whose
    game picture is canned-identical to another member are out (barber, shop, cutscene, menu), so are
    cutscenes (letterbox), canned words + visual identity and weak reactions; a group needs >= 3
    streamers whose outcomes differ from each other.
    """
    by_id = {r["id"]: r for r in rows}
    out = []
    for g in groups:
        ms = [by_id[i] for i in g if i in by_id]
        keep = []
        for m in ms:
            a, b = m.get("seg") or (None, None)
            if letterbox_share(m["source"], a, b) > 0.5:
                continue                               # cutscene (and a spoiler risk)
            if (m.get("fun") or 1) < min_fun:
                continue                               # no strong reaction
            keep.append(m)
        bad = set()
        for i, x in enumerate(keep):
            for y in keep[i + 1:]:
                if x["channel"] == y["channel"]:
                    continue
                is_c, _ = canned(x, y)
                text = " ".join(t for _, _, t in (x.get("words") or [])) + " " + (x.get("title") or "")
                if is_c or (canned_words(text) and seq_similarity(_frames(x), _frames(y)) > SIM_CANNED - 0.08):
                    bad |= {x["id"], y["id"]}
                elif not outcome_differs(x, y):
                    bad |= {y["id"]}                   # same outcome: keep only one POV of it
        ok = [m for m in keep if m["id"] not in bad]
        if len({m["channel"] for m in ok}) >= min_streamers:
            out.append({m["id"] for m in ok})
    return sorted(out, key=len, reverse=True)
