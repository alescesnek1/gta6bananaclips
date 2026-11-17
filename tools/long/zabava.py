"""
zabava.py - "entertained every second" rules of the long-form video (Aleš 17. 11.).

  dedupe(rows)            the same moment never twice: same clip id, same VOD with overlapping ranges (or the
                          same scene: within 3 min in the VOD and the same place on screen), the
                          same sound (two viewer clips / two POVs of one moment), same streamer + same picture.
                          rows best first -> (kept, {dropped id: (kept id, why)})
  low_action(m)           low motion + low loudness + no laughter / scream, or a standing picture -> True (out)
  tight_cut(...)          pieces of the moment to keep: K.dead_air jump cuts + every stretch > 2 s where nobody
                          speaks, nothing is loud and the picture stands still is cut out; None = a dead
                          stretch sits on the payoff and cannot be cut -> drop the moment
  snap_segment(...)       start on a sentence 9-12 s before the payoff, end on the end of the line after it
  order_countdown(rows)   weakest -> strongest, never two moments of the same streamer or the same
                          category back to back when another order exists; #1 stays last
"""
import subprocess
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE))

DEAD_MAX = 2.0                    # s of nothing (no speech, no loud sound, no motion) a viewer will sit through
SCENE_GAP = 180.0                 # s between two cuts in one VOD under which they may be one scene ...
SCENE_SHARE = 0.15                # ... when this share of their game frames is the same picture (same place)
AUDIO_SAME = 0.60                 # normalised correlation of the loudness envelopes over >= 3 s = the same sound
HEAD, HEAD_MAX, TAIL, TAIL_MAX = 10.0, 12.5, 2.2, 3.6
STILL = 4.0                       # mean picture change under this = nothing moves (gameplay is 10+)
MIN_LEN = 8.3                     # moment + the 1.9 s placement = a >= 10 s YouTube chapter


# ---------------------------------------------------------------------------
# the same moment twice

def audio_env(src, a, b, pad=3.0):
    """Loudness envelope 50 Hz (log energy, differentiated, zero-mean) of [a - pad, b + pad]."""
    a0 = max(0.0, a - pad)
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{a0:.2f}", "-t", f"{b + pad - a0:.2f}", "-i", str(src),
                          "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"], capture_output=True, timeout=300).stdout
    x = np.frombuffer(raw, np.int16).astype(np.float32)
    n = x.size // 160
    if n < 100:
        return np.zeros(0, np.float32)
    e = np.log1p((x[: n * 160].reshape(n, 160) ** 2).mean(axis=1))
    d = np.diff(e)
    return (d - d.mean()).astype(np.float32)


def audio_match(ea, eb, min_overlap=150):
    """Best normalised correlation of two envelopes over any alignment with >= 3 s overlap (1.0 = identical)."""
    if ea.size < min_overlap or eb.size < min_overlap:
        return 0.0
    full = np.correlate(ea, eb, mode="full")                   # lag index k: eb shifted by k - (len(eb) - 1)
    ca, cb = np.concatenate([[0], np.cumsum(ea * ea)]), np.concatenate([[0], np.cumsum(eb * eb)])
    best = 0.0
    for k in range(full.size):
        sh = k - (eb.size - 1)                                 # ea[i + sh] pairs with eb[i]
        i0, i1 = max(0, -sh), min(eb.size, ea.size - sh)
        if i1 - i0 < min_overlap:
            continue
        na, nb = ca[i1 + sh] - ca[i0 + sh], cb[i1] - cb[i0]
        if na > 0 and nb > 0:
            best = max(best, float(full[k] / np.sqrt(na * nb)))
    return best


def vod_range(m):
    """(video_id, start, end) of the moment's cut inside the stream's VOD, or None."""
    if not m.get("video_id") or m.get("vod_offset") is None:
        return None
    s, e = m.get("seg") or (0.0, m.get("dur") or 30.0)
    return str(m["video_id"]), m["vod_offset"] + s, m["vod_offset"] + e


def same_moment(a, b, env, frames):
    """Why `b` is the same moment as `a` (None = it is not)."""
    if a["id"] == b["id"]:
        return "same clip"
    ra, rb = vod_range(a), vod_range(b)
    if ra and rb and ra[0] == rb[0] and ra[1] - 6 < rb[2] and rb[1] - 6 < ra[2]:
        return "same VOD, overlapping"
    near = ra and rb and ra[0] == rb[0] and min(abs(ra[1] - rb[2]), abs(rb[1] - ra[2])) < SCENE_GAP
    am = audio_match(env(a), env(b))
    if am >= AUDIO_SAME:
        return f"same sound {am:.2f}"
    if near or (a["channel"] == b["channel"] and abs((a.get("created") or 0) - (b.get("created") or 0)) < 900):
        import podobnost
        fa, fb = frames(a), frames(b)
        if len(fa) and len(fb):
            ha, hb = [podobnost.phash(f) for f in fa], [podobnost.phash(f) for f in fb]
            share = float(np.mean([max(1 - np.count_nonzero(x != y) / 64 for y in hb) >= 0.92 for x in ha]))
            if near and share >= SCENE_SHARE:                  # minutes apart in one VOD AND the same place on
                return f"same scene {share:.2f}"               # screen: two clips of one scene read as a repeat
            if share >= 0.4:
                return f"same picture {share:.2f}"
    return None


def dedupe(rows):
    """rows best first (each with source + seg) -> (kept, {dropped id: (kept id, why)})."""
    import podobnost
    envs, frs = {}, {}

    def env(m):
        if m["id"] not in envs:
            try:
                envs[m["id"]] = audio_env(m["source"], *m["seg"])
            except Exception:
                envs[m["id"]] = np.zeros(0, np.float32)
        return envs[m["id"]]

    def frames(m):
        if m["id"] not in frs:
            try:
                frs[m["id"]] = podobnost.game_frames(m["source"], m["seg"][0], m["seg"][1], m.get("cam"))
            except Exception:
                frs[m["id"]] = []
        return frs[m["id"]]

    kept, dropped, seen = [], {}, set()
    for m in rows:
        if m["id"] in seen:
            dropped[m["id"]] = (m["id"], "same clip")
            continue
        why = None
        for k in kept:
            why = same_moment(k, m, env, frames)
            if why:
                dropped[m["id"]] = (k["id"], why)
                break
        if not why:
            kept.append(m)
            seen.add(m["id"])
    return kept, dropped


# ---------------------------------------------------------------------------
# action

def low_action(m):
    """Low motion + low loudness + no laughter / scream (kategorie.classify signals of the cut)."""
    s = m.get("signals") or {}
    if s.get("motion") is None or s.get("loud") is None:
        return False
    if s["motion"] < STILL and (s.get("laugh") or 0) < 2:      # a standing picture (a call window, a menu, a desk)
        return True
    return s["motion"] < 6.0 and s["loud"] < 3.0 and not s.get("laugh")


def activity(words, src, a, b, step=0.25):
    """Per `step` s of [a, b]: is anything going on (speech, loud sound, moving picture)? -> (times, bool)."""
    import kategorie
    import klipuj as K
    ts = np.arange(a, b, step)
    act = np.zeros(ts.size, bool)
    for w0, w1, _ in words:
        act |= (ts + step > w0 - 0.25) & (ts < w1 + 0.25)
    rt, rms = K.rms_curve(src)
    if rms.size:
        med = float(np.median(rms))
        loud = np.interp(ts + step / 2, rt, rms)
        act |= loud > 1.25 * med
    mot = kategorie.motion_series(src)                         # 4 fps
    if mot.size:
        idx = np.clip((ts * 4).astype(int), 0, mot.size - 1)
        act |= mot[idx] > 9.0
    return ts, act


def tight_cut(words, src, s, e, peak, min_len=MIN_LEN):
    """
    Pieces [(x, y)] of [s, e] to keep, or None when a dead stretch > DEAD_MAX s sits around the payoff.
    Dead air between words goes first (klipuj.dead_air), then every stretch > 2 s with no speech, no loud
    sound and a still picture is cut down to 0.7 s. Too short after the cuts -> the plain dead_air cut.
    """
    import klipuj as K
    keep = K.dead_air(words, src, s, e, peak) if words else [(s, e)]
    step = 0.25
    ts, act = activity(words, src, s, e, step)
    runs, i = [], 0
    while i < ts.size:
        if not act[i]:
            j = i
            while j < ts.size and not act[j]:
                j += 1
            if (j - i) * step > DEAD_MAX:
                runs.append((float(ts[i]), float(ts[i]) + (j - i) * step))
            i = j
        else:
            i += 1
    cuts = []
    for r0, r1 in runs:
        if peak is not None and r0 - 1.0 < peak < r1 + 1.5:
            return None                                        # nothing happens around the payoff: not a moment
        cuts.append((r0 + 0.35, r1 - 0.35))
    out = []
    for x, y in keep:
        cur = x
        for c0, c1 in cuts:
            if c1 <= cur or c0 >= y:
                continue
            if c0 - cur > 0.05:
                out.append((cur, c0))
            cur = max(cur, c1)
        if y - cur > 0.05:
            out.append((cur, y))
    if sum(y - x for x, y in out) < min_len:
        if runs and e - s >= min_len:                          # the dead stretch cannot be cut: too little is left
            return None
        return keep if sum(y - x for x, y in keep) >= min_len else [(s, e)]
    return out


def snap_segment(words, cut, peak, dur):
    """
    Tighter HPC cut (average moment 12-16 s): start on the sentence that begins 9-12.5 s before the payoff
    (else 10 s before), end 2.2 s after the payoff, moved to the end of the line being said (<= 3.6 s).
    """
    a, b = cut
    if peak is None:
        return round(a, 2), round(min(b, a + 18.0), 2)
    s = max(a, peak - HEAD)
    starts = [w[0] for i, w in enumerate(words)
              if i == 0 or w[0] - words[i - 1][1] > 0.35 or (words[i - 1][2][-1:] in ".?!")]
    ok = [t for t in starts if peak - HEAD_MAX <= t <= peak - 7.0 and t >= a]
    if ok:
        s = max(0.0, min(ok, key=lambda t: abs(t - (peak - HEAD))) - 0.12)
    e = peak + TAIL
    for w0, w1, _ in words:                                    # do not cut a word in half
        if w0 < e < w1 + 0.25 and w1 + 0.25 <= peak + TAIL_MAX:
            e = w1 + 0.25
    e = min(dur, e)
    if e - s < MIN_LEN + 0.4:                                  # short source: take what there is before the payoff
        s = max(0.0, e - (MIN_LEN + 0.4))
        e = min(dur, s + MIN_LEN + 0.4)
    return round(s, 2), round(e, 2)


# ---------------------------------------------------------------------------
# order

def order_countdown(rows, cat=lambda m: m.get("primary")):
    """
    Countdown order, first played -> last: weakest to strongest by score, the best one always last, and
    never the same streamer (or the same category) twice in a row when a neighbour within 3 places can
    be swapped in instead.
    """
    pool = sorted(rows, key=lambda m: m["score"])
    if len(pool) < 3:
        return pool
    last = pool.pop()
    out = []
    while pool:
        prev = out[-1] if out else None
        pick = 0
        if prev is not None:
            best = None
            for i, m in enumerate(pool[:4]):
                clash = (m["channel"] == prev["channel"]) * 2 + (cat(m) is not None and cat(m) == cat(prev))
                if len(pool) == 2 and i < 2:                   # two left: the other one will sit before #1
                    o = pool[1 - i]
                    clash += (o["channel"] == last["channel"]) * 2 + (cat(o) is not None and cat(o) == cat(last))
                if len(pool) - i == 1:                         # would sit right before #1
                    clash += (m["channel"] == last["channel"]) * 2 + (cat(m) is not None and cat(m) == cat(last))
                if best is None or clash < best[0]:
                    best = (clash, i)
            pick = best[1]
        out.append(pool.pop(pick))
    if len(out) >= 2 and out[-1]["channel"] == last["channel"]:   # #2 and #1 from one streamer: swap #2 down if it helps
        for i in range(len(out) - 2, max(-1, len(out) - 5), -1):
            if out[i]["channel"] != last["channel"] and (i == 0 or out[i - 1]["channel"] != out[-1]["channel"]):
                out[i], out[-1] = out[-1], out[i]
                break
    return out + [last]
