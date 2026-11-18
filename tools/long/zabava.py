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
  snap_segment(...)       start on a sentence 9-12.5 s (at most 15 s) before the payoff, end on a sentence end
                          2-3.6 s (at most 8 s) after it; None = no sentence-safe cut -> drop the moment
  Cutter                  every cut point of a long-form moment (start, end, jump cuts, cold open, beat cut):
                          klipuj's sentence rules + the source edges + the loudness check (speech Whisper dropped)
  order_countdown(rows)   weakest -> strongest, never two moments of the same streamer or the same
                          category back to back when another order exists; #1 stays last
"""
import math
import re
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


# ---------------------------------------------------------------------------
# sentence-safe cut points (Aleš 18. 11.: "never cut in the middle of a sentence", for long-form too)

_FINE = {}


def rms_fine(src):
    """Loudness every 25 ms (50 ms windows) -> (times, rms); empty arrays without audio. Cached per file."""
    key = str(src)
    if key not in _FINE:
        try:
            raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", key, "-vn", "-ac", "1", "-ar", "8000",
                                  "-f", "s16le", "-"], capture_output=True, timeout=300).stdout
        except (subprocess.TimeoutExpired, OSError):
            raw = b""
        a = np.frombuffer(raw, np.int16).astype(np.float64)
        if a.size < 800:
            _FINE[key] = (np.zeros(0), np.zeros(0))
        else:
            c = np.concatenate([[0.0], np.cumsum(a * a)])
            rms = np.sqrt((c[400:] - c[:-400]) / 400)[::200]
            _FINE[key] = (np.arange(len(rms)) * 0.025 + 0.025, rms)
    return _FINE[key]


def _cap_start(text):
    """Whisper writes a new sentence with a capital letter ("I" alone says nothing)."""
    t = text.strip().lstrip("\"'([")
    return bool(t) and t[0].isupper() and not re.match(r"^I('|$|\b)", t)


class Cutter:
    """
    Where a long-form moment may be cut. A point is safe when klipuj says so (never inside a word, a start
    before a sentence start, an end after a sentence end) AND:
      - the source's own edges: a viewer clip that starts or ends while the streamer talks is mid-sentence
        even when Whisper's first / last word looks like a whole sentence (start needs >= 0.35 s of air
        before the first word, end needs . ? ! or >= 0.5 s of air after the last word)
      - the loudness right at the point is not speech level (Whisper drops doubtful / sung segments, a cut
        must not land in talking it did not write down): <= max(0.6 x the speech level, 2 x the floor)
    words = [(t0, t1, text)], rt / rms = rms_fine of the source (empty = no loudness check).
    """

    def __init__(self, words, dur, rt=None, rms=None):
        import klipuj as K
        self.K = K
        self.w = [tuple(x) for x in (words or [])]
        self.dur = float(dur)
        self.rt = rt if rt is not None else np.zeros(0)
        self.rms = rms if rms is not None else np.zeros(0)
        self.ref = None
        self.strict = True            # False: the loudness check is off (a loud payoff with no quiet point)

    @classmethod
    def of(cls, words, src, dur):
        import klipuj as K
        rt, rms = rms_fine(src) if src and K.has_audio(src) else (None, None)
        return cls(words, dur, rt, rms)

    def window(self, a, b):
        """Speech level and floor of [a, b] (the loudness check is relative to the moment itself)."""
        self.ref = None
        if not self.rms.size:
            return self
        m = (self.rt >= a) & (self.rt <= b)
        if not m.any():
            return self
        floor = float(np.percentile(self.rms[m], 20))
        sp = [self.rms[(self.rt >= w0) & (self.rt <= w1)] for w0, w1, _ in self.w if a <= w0 and w1 <= b]
        sp = np.concatenate(sp) if sp else np.zeros(0)
        self.ref = (float(np.median(sp)) if sp.size >= 4 else None, floor)
        return self

    def quiet(self, t, win=0.06):
        if not self.strict or self.ref is None or not self.rms.size:
            return True
        m = (self.rt >= t - win) & (self.rt <= t + win)
        if not m.any():
            return True
        speech, floor = self.ref
        lim = max(0.6 * speech, 2.0 * floor) if speech else 2.5 * floor
        return float(self.rms[m].mean()) <= lim + 1e-9

    def ok_start(self, t):
        K, w = self.K, self.w
        if t < -0.01 or t > self.dur or not K.ok_start(w, t):
            return False
        nxt = next((x for x in w if x[0] >= t - K.WORD_TOL), None)
        if nxt is not None and nxt is w[0] and w[0][0] < K.START_GAP + 0.05:
            return False                                       # the clip itself starts while he talks: the
        return self.quiet(t)                                   # first sentence is a fragment, never a start

    def ok_end(self, t):
        K, w = self.K, self.w
        if t > self.dur + 0.01 or t < 0 or not K.ok_end(w, t):
            return False
        last = next((x for x in reversed(w) if x[1] <= t + K.WORD_TOL), None)
        if (last is not None and last is w[-1] and not K._punct_end(last[2])
                and self.dur - last[1] < K.END_GAP):
            return False                                       # the clip itself ends while he talks: his
        return self.quiet(t)                                   # last sentence is a fragment, never an end

    def ok_jump(self, x, y):
        """A jump cut from x to y (x < y): nothing said is cut away, x ends a sentence, y starts one.
        Strict (never two halves of one sentence): the last word before x ends with . ? ! or the first
        word after y starts with a capital letter (Whisper's new sentence)."""
        K, w = self.K, self.w
        if any(a0 < y - K.WORD_TOL and a1 > x + K.WORD_TOL for a0, a1, _ in w):
            return False
        if not (self.ok_end(x) and self.ok_start(y)):
            return False
        prev = next((a for a in reversed(w) if a[1] <= x + K.WORD_TOL), None)
        nxt = next((a for a in w if a[0] >= y - K.WORD_TOL), None)
        if prev is None or nxt is None:
            return True
        return K._punct_end(prev[2]) or _cap_start(nxt[2])

    def starts(self, lo, hi, step=0.25):
        """Safe start points in [lo, hi]: just before every sentence start + a grid through the pauses."""
        K, w = self.K, self.w
        lo, hi = max(0.0, lo), min(self.dur, hi)
        if hi < lo:
            return []
        pts = set(np.round(np.arange(lo, hi + 1e-6, step), 3).tolist()) | {round(lo, 3), round(hi, 3)}
        for i, x in enumerate(w):
            if K.sent_start(w, i):
                prev = w[i - 1][1] if i else -1e9
                p = x[0] - 0.12 if x[0] - prev >= 0.3 else (prev + x[0]) / 2
                if lo <= p <= hi:
                    pts.add(round(p, 3))
        tight = lambda t: any(0.0 <= x[0] - t < 0.08 for x in w)      # a word's attack right at the cut
        return sorted(t for t in pts if not tight(t) and self.ok_start(t))

    def ends(self, lo, hi, step=0.25):
        """Safe end points in [lo, hi]: just after every sentence end + a grid through the pauses."""
        K, w = self.K, self.w
        lo, hi = max(0.0, lo), min(self.dur, hi)
        if hi < lo:
            return []
        pts = set(np.round(np.arange(lo, hi + 1e-6, step), 3).tolist()) | {round(lo, 3), round(hi, 3)}
        for i, x in enumerate(w):
            if K.sent_end(w, i):
                p = K._end_point(w, i, self.dur)
                if lo <= p <= hi:
                    pts.add(round(p, 3))
        tight = lambda t: any(0.0 <= t - x[1] < 0.08 for x in w)      # a word's decay right at the cut
        return sorted(t for t in pts if not tight(t) and self.ok_end(t))

    def safe_pieces(self, pieces):
        """Jump cuts of `pieces` that are not ok_jump are closed (the pieces around them joined)."""
        out = []
        for x, y in pieces:
            if out and not self.ok_jump(out[-1][1], x):
                out[-1] = (out[-1][0], y)
            else:
                out.append((x, y))
        return out

    def report(self, pieces):
        """Cut check of a moment's kept pieces -> problems (empty = every boundary sentence-safe). The
        loudness test is a preference when choosing (snap_segment falls back without it), so the check is
        the sentence / word / source-edge rules."""
        strict, self.strict = self.strict, False
        try:
            return self._report(pieces)
        finally:
            self.strict = strict

    def _report(self, pieces):
        probs = []
        a, b = pieces[0][0], pieces[-1][1]
        if not self.ok_start(a):
            probs.append(f"start {a:.2f} s not on a sentence start")
        if not self.ok_end(b):
            probs.append(f"end {b:.2f} s not on a sentence end")
        for (_, x), (y, _) in zip(pieces, pieces[1:]):
            if not self.ok_jump(x, y):
                probs.append(f"jump cut {x:.2f}-{y:.2f} s inside a sentence")
        return probs

    def edge_words(self, a, b, n=4):
        """First and last few words of [a, b] (the log / QA shows what the viewer hears at the cuts)."""
        inside = [x[2] for x in self.w if a - 0.02 <= x[0] and x[1] <= b + 0.02]
        return " ".join(inside[:n]), " ".join(inside[-n:])


def tight_cut(words, src, s, e, peak, min_len=MIN_LEN, cutter=None):
    """
    Pieces [(x, y)] of [s, e] to keep, or None when a dead stretch > DEAD_MAX s sits around the payoff.
    Dead air between words goes first (klipuj.dead_air), then every stretch > 2 s with no speech, no loud
    sound and a still picture is cut down to 0.7 s. Every jump cut must be sentence-safe (Cutter.ok_jump:
    only after a finished sentence, never two halves of one sentence, not in talking Whisper dropped);
    an unsafe one is simply not made. Too short after the cuts -> the plain dead_air cut.
    """
    import klipuj as K
    c = cutter or Cutter.of(words, src, K.duration(src))
    c.window(s, e)
    keep = c.safe_pieces(K.dead_air(words, src, s, e, peak) if words else [(s, e)])
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
        if c.ok_jump(r0 + 0.35, r1 - 0.35):
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
    out = c.safe_pieces(out) if out else [(s, e)]
    if sum(y - x for x, y in out) < min_len:
        if runs and e - s >= min_len:                          # the dead stretch cannot be cut: too little is left
            return None
        return keep if sum(y - x for x, y in keep) >= min_len else [(s, e)]
    return out


def snap_segment(words, cut, peak, dur, cutter=None):
    """
    Tighter HPC cut (average moment 12-16 s), sentence-safe or nothing:
      start  a safe start (Cutter) 7-12.5 s before the payoff (nearest to 10 s), else up to 15 s before,
             else 4.5-7 s before; starts at / after the cut's own start (klipuj.trim_points, already on a
             sentence) are preferred
      end    a safe end 1.5-3.6 s after the payoff (nearest to 2.2 s), else up to 8 s after, else 0.8-1.5 s
    Returns (s, e) or None (no sentence-safe cut: the moment is dropped, never a blind cut).
    """
    c = cutter or Cutter(words, dur)
    r = _snap_segment(c, cut, peak, dur)
    if r is None and c.rms.size and c.w:                     # a loud payoff with no quiet END point: the end may
        r = _snap_segment(c, cut, peak, dur, loose_end=True) # follow the sentence rules only (starts stay strict;
    return r                                                 # no transcript at all = no loose retry)


def _snap_segment(c, cut, peak, dur, loose_end=False):
    a, b = cut
    if peak is None or not 0.0 < peak < dur:                  # no payoff: the cut itself, made safe
        c.window(a, min(dur, a + 25.0))
        s = next(iter(c.starts(a, a + 3.0)), None)
        if s is None:
            return None
        hi = min(dur, b, s + 18.0)
        ends = c.ends(s + MIN_LEN + 0.4, hi) or c.ends(s + 4.0, hi)
        return (round(s, 2), round(ends[-1], 2)) if ends else None
    c.window(max(0.0, peak - 15.0), min(dur, peak + 8.0))
    s = None
    for lo, hi in ((peak - HEAD_MAX, peak - 7.0), (peak - 15.0, peak - 7.0), (peak - 7.0, peak - 4.5)):
        cands = c.starts(lo, hi)
        if cands:
            s = min(cands, key=lambda t: (t < a - 0.01, abs(t - (peak - HEAD))))
            break
    if s is None:
        return None
    e = None
    c.strict = not loose_end
    try:
        for lo, hi, tgt in ((peak + 1.5, peak + TAIL_MAX, peak + TAIL), (peak + TAIL_MAX, peak + 8.0, peak + TAIL_MAX),
                            (peak + 0.8, peak + 1.5, peak + 1.5)):
            cands = c.ends(lo, hi)
            if cands:
                e = min(cands, key=lambda t: abs(t - tgt))
                break
        later = c.ends(s + MIN_LEN + 0.4, peak + 8.0) if e is not None and e - s < MIN_LEN + 0.4 else []
    finally:
        c.strict = True
    if e is None:
        return None
    need = MIN_LEN + 0.4
    if e - s < need:                                           # short source: an earlier start, else a later end
        earlier = [t for t in c.starts(peak - 15.0, s) if e - t >= need]
        if earlier:
            s = max(earlier)
        elif later:
            e = min(later)
    return round(s, 2), round(e, 2)


def cold_shot(cutter, peak, shot=2.5):
    """
    One cold open shot (<= `shot` s): the build-up into the payoff. Start on a sentence start 1.9-4 s before
    the payoff (the latest one), end on a word boundary just after the payoff (klipuj.in_word). A start that
    would make it longer than `shot` moves the end earlier to a word boundary, but never before the payoff
    - 0.3 s. Else a sentence start 1.2-1.9 s before the payoff. Returns (s, e) or None (no clean shot).
    """
    K, w, c = cutter.K, cutter.w, cutter
    c.window(max(0.0, peak - 6.0), min(c.dur, peak + 3.0))
    e = min(c.dur, peak + 0.3)
    wd = K.in_word(w, e)
    if wd:
        e = min(c.dur, wd[1] + 0.02)

    def edge_before(t):
        x = K.in_word(w, t)
        return x[0] - 0.02 if x else t

    for s in sorted(c.starts(peak - 4.0, peak - 1.9), reverse=True):
        ee = e if e - s <= shot else edge_before(s + shot)
        if ee >= peak - 0.3 and ee - s >= 1.2 and not K.in_word(w, ee):
            return round(s, 2), round(ee, 2)
    for s in sorted(c.starts(e - shot, peak - 1.2), reverse=True):   # shorter build-up, still a sentence start
        if e - s >= 1.2:
            return round(s, 2), round(e, 2)
    return None


def beat_fit(cutter, keep, T, ph, t, peak=None, min_len=6.0, ext=4.0, shrink=4.0):
    """
    After Dark: the moment's end on a beat of the music (video time t = where the moment starts, beat
    period T, a beat at ph). Never into a sentence or a word: the moment is EXTENDED to a later beat when
    the source goes on and that point is sentence-safe; only when no later beat works, it ends on an
    earlier beat that is a sentence end (never before the payoff + 1.5 s). Returns the new pieces or None.
    """
    natural = sum(y - x for x, y in keep)
    k0 = math.ceil((t + natural - ph) / T)
    cands = [ph + k * T - t for k in range(k0 - 40, k0 + 40)]
    cands = [L for L in cands if max(min_len, natural - shrink) <= L <= natural + ext]
    cands.sort(key=lambda L: (L < natural - 1e-6, abs(L - natural)))
    cutter.window(keep[0][0], keep[-1][1] + ext)
    for L in cands:
        if L >= natural:
            end = keep[-1][1] + (L - natural)
            if end > cutter.dur - 0.02:
                continue
            new = keep[:-1] + [(keep[-1][0], end)]
        else:
            new, acc = [], 0.0
            for x, y in keep:
                if acc + (y - x) >= L:
                    new.append((x, x + (L - acc)))
                    break
                new.append((x, y))
                acc += y - x
            end = new[-1][1]
            if peak is not None and end < peak + 1.5:
                continue
        if new[-1][1] - new[-1][0] > 0.3 and cutter.ok_end(end):
            return [(round(x, 3), round(y, 3)) for x, y in new]
    return None


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
