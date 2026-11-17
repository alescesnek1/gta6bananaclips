"""
hudba.py - the music of a long-form video: one continuous bed under the WHOLE video.

Aleš 17. 11.: music must never stop, it changes every block of 4-5 moments, it is ducked under
speech but never silent, the energy rises towards #1, and the tier placements have their own sting.

  plan = hudba.Plan(brand, seed, n_blocks)      # tracks of the video, weakest energy first, strongest last
  plan.track(i)                                 # {"path", "start", "gain", "bpm", ...} of block i
  plan.beats(i, t_block)                        # (period, first beat in VIDEO time) of block i (cuts on the beat)
  hudba.build_bed(plan, total, block_starts, out_wav, base=0.22, boosts=[(a, b, x)], stings=[(a, land)],
                  stop_at=None)                 # numpy mix -> stereo 48 kHz wav, gains baked in (not the ducking)
  hudba.mix(video, bed_wav, out)                # ducking with a floor (never silent) + two-pass gain to -16 LUFS

The database is assets/hudba/long/sources.json (tools/long/hudba_db.py: Openverse CC0 + our own tracks),
every track is levelled to -16 LUFS by its measured loudness, so crossfades have no level jumps.
"""
import json
import random
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parents[2]
DB = HERE / "assets" / "hudba" / "long" / "sources.json"
SR = 48000
XFADE = 1.6                                   # s, equal-power crossfade between blocks
BLOCK = 5                                     # moments per music block (4-5)


def tracks(brand):
    """Tracks of the brand that exist on disk, weakest energy first."""
    rows = [t for t in (json.loads(DB.read_text(encoding="utf-8")).get("tracks") if DB.exists() else []) or []
            if t.get("brand") == brand and (HERE / t["file"]).exists()]
    return sorted(rows, key=lambda t: t.get("energy") or 0)


def _window(path):
    sys.path.insert(0, str(HERE))
    import klipuj as K
    return K.track_window(Path(path))


def _decode(path, start, dur):
    """Stereo float32 [n, 2] of `dur` s from `start` s; the track loops when it is too short."""
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-i", str(path), "-vn",
                          "-ac", "2", "-ar", str(SR), "-f", "f32le", "-"], capture_output=True, timeout=300).stdout
    x = np.frombuffer(raw, np.float32).reshape(-1, 2)
    n = int(dur * SR)
    if x.shape[0] < SR:                                        # nothing there: start from the top
        raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-vn", "-ac", "2", "-ar", str(SR),
                              "-f", "f32le", "-"], capture_output=True, timeout=300).stdout
        x = np.frombuffer(raw, np.float32).reshape(-1, 2)
    while x.shape[0] < n:                                      # loop with a short fade over the seam
        k = min(int(0.05 * SR), x.shape[0] // 4)
        y = x.copy()
        y[:k] *= np.linspace(0, 1, k)[:, None]
        x = np.concatenate([x[:-k], x[-k:] * np.linspace(1, 0, k)[:, None] + y[:k], y[k:]])
    return x[:n].copy()


class Plan:
    """Which track plays in which block. Different tracks per episode (seed), always rising in energy."""

    def __init__(self, brand, seed, n_blocks):
        rows = tracks(brand)
        self.brand, self.rows = brand, rows
        self.blocks = []
        if not rows:
            return
        rnd = random.Random(str(seed))
        n = max(1, n_blocks)
        if len(rows) >= n:
            top = rows[-max(1, len(rows) // 3):]               # the finale: one of the strongest third
            last = rnd.choice(top)
            rest = [r for r in rows if r is not last and (r.get("energy") or 0) <= (last.get("energy") or 0)]
            rest = rest if len(rest) >= n - 1 else [r for r in rows if r is not last]
            pick = sorted(rnd.sample(rest, n - 1) + [last], key=lambda t: t.get("energy") or 0)   # always rising
        else:                                                  # more blocks than tracks: some play two blocks in
            pick = sorted((rows * (n // len(rows) + 1))[-n:], key=lambda t: t.get("energy") or 0)   # a row, never falling
        for t in pick:
            lo, hi = _window(HERE / t["file"])
            start = lo + rnd.uniform(0, max(0.0, min(25.0, hi - lo - 100)))
            self.blocks.append({**t, "path": HERE / t["file"], "start": round(start, 2),
                                "gain": 10 ** ((-16.0 - (t.get("lufs") or -16.0)) / 20)})
        used = {b["file"] for b in self.blocks}
        cand = [r for r in rows if r["file"] not in used] or rows
        s = max(cand, key=lambda t: (t.get("pulse") or 0) + (t.get("energy") or 0))
        self.sting = {**s, "path": HERE / s["file"], "gain": 10 ** ((-16.0 - (s.get("lufs") or -16.0)) / 20)}
        self._beats, self._sting_x = {}, None

    def __bool__(self):
        return bool(self.blocks)

    def track(self, i):
        return self.blocks[min(i, len(self.blocks) - 1)]

    def beats(self, i, t_block):
        """Beat period and a beat time in VIDEO time for block i that starts at t_block (None if unknown)."""
        sys.path.insert(0, str(HERE))
        import klipuj as K
        b = self.track(i)
        if i not in self._beats:
            self._beats[i] = K.beat_grid(b["path"], b["start"], 60)
        g = self._beats[i]
        return (g[0], t_block + g[1]) if g else None

    def sting_clip(self, dur, land):
        """The placement jingle: always the same excerpt, a heavy beat exactly on the landing."""
        if self._sting_x is None or self._sting_x[0] != (round(dur, 3), round(land, 3)):
            sys.path.insert(0, str(HERE))
            import klipuj as K
            lo, hi = _window(self.sting["path"])
            start = lo + min(30.0, max(0.0, (hi - lo) * 0.3))
            g = K.beat_grid(self.sting["path"], start, 12)
            if g:
                T, ph = g
                first = start + ph
                while first - land < start:
                    first += T
                start = first - land
            x = _decode(self.sting["path"], start, dur) * self.sting["gain"]
            k = int(0.07 * SR)
            x[:k] *= np.linspace(0, 1, k)[:, None]
            k = int(0.25 * SR)
            x[-k:] *= np.linspace(1, 0, k)[:, None] ** 2
            self._sting_x = ((round(dur, 3), round(land, 3)), x)
        return self._sting_x[1]

    def credits(self):
        seen, out = set(), []
        for b in self.blocks + [self.sting]:
            if b["file"] not in seen:
                seen.add(b["file"])
                out.append({k: b.get(k) for k in ("file", "title", "creator", "license", "url")})
        return out


def build_bed(plan, total, block_starts, out_wav, base=0.22, boosts=(), stings=(), sting_dur=1.9, stop_at=None,
              rise=1.3, sting_vol=0.9):
    """
    The whole music track of the video as a wav. block_starts = video times where block i begins (the first
    is 0); a new track fades in there over XFADE s while the old one fades out (equal power). boosts =
    (start, end, factor): louder under boards and cards. stings = (start, land): the placement jingle there,
    the main music dips under it. The level rises `rise`x from the start to the end (towards #1). stop_at:
    fade out there (the end card has its own sound).
    """
    n = int(total * SR)
    bed = np.zeros((n, 2), np.float32)
    xf = int(XFADE * SR)
    starts = list(block_starts) + [total]
    for i in range(len(starts) - 1):
        b = plan.track(i)
        a0 = max(0, int(starts[i] * SR) - (xf // 2 if i else 0))
        a1 = min(n, int(starts[i + 1] * SR) + (xf // 2 if i + 1 < len(starts) - 1 else 0))
        if a1 <= a0:
            continue
        lead = (int(starts[i] * SR) - a0) / SR                 # the crossfade starts before the block: so does the
        if lead > b["start"]:                                  # track, b["start"] stays on the block start (beats)
            a0, lead = int(starts[i] * SR) - int(b["start"] * SR), b["start"]
        x = _decode(b["path"], b["start"] - lead, (a1 - a0) / SR + 0.1)[: a1 - a0] * b["gain"]
        if i:                                                  # equal-power in
            k = min(xf, x.shape[0])
            x[:k] *= np.sin(np.linspace(0, np.pi / 2, k))[:, None]
        if i + 1 < len(starts) - 1:                            # equal-power out
            k = min(xf, x.shape[0])
            x[-k:] *= np.cos(np.linspace(0, np.pi / 2, k))[:, None]
        bed[a0: a0 + x.shape[0]] += x
    t = np.arange(n) / SR
    env = base * (1 + (rise - 1) * np.clip(t / max(total, 1), 0, 1))
    k = int(0.25 * SR)
    for a, b_, f in boosts:
        i0, i1 = max(0, int(a * SR)), min(n, int(b_ * SR))
        if i1 - i0 > 2 * k:
            w = np.ones(i1 - i0)
            w[:k], w[-k:] = np.linspace(0, 1, k), np.linspace(1, 0, k)
            env[i0:i1] *= 1 + (f - 1) * w
    for a, land in stings:                                     # the main music steps back under the jingle
        i0, i1 = max(0, int(a * SR)), min(n, int((a + sting_dur) * SR))
        if i1 - i0 > 2 * k:
            w = np.ones(i1 - i0)
            kk = int(0.1 * SR)
            w[:kk], w[-k:] = np.linspace(0, 1, kk), np.linspace(1, 0, k)
            env[i0:i1] *= 1 - 0.85 * w
    bed *= env[:, None].astype(np.float32)
    for a, land in stings:
        x = plan.sting_clip(sting_dur, land) * sting_vol
        i0 = int(a * SR)
        if 0 <= i0 < n:
            bed[i0: i0 + x.shape[0]] += x[: n - i0]
    if stop_at is not None:
        i0 = int(stop_at * SR)
        k = min(int(0.8 * SR), n - i0)
        if k > 0:
            bed[i0: i0 + k] *= np.linspace(1, 0, k)[:, None]
            bed[i0 + k:] = 0
    with wave.open(str(out_wav), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(bed, -1, 1) * 32767).astype(np.int16).tobytes())
    return out_wav


def mix(video, bed_wav, out, aout=("-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"), floor=0.42, target=-16.0):
    """
    Bed under the video's own audio: ducked by the stream audio (sidechain), but a `floor` share of the
    music always stays (parallel path) - quieter under speech, never silent. Video copied; -16 LUFS (two-pass).
    """
    import re
    g = ("[1:a]aresample=48000,aformat=channel_layouts=stereo,asplit=2[m1][m2];"
         "[0:a]aresample=48000,aformat=channel_layouts=stereo,asplit=2[sa][sc];"
         "[m1][sc]sidechaincompress=threshold=0.025:ratio=8:attack=12:release=420:knee=4[duck];"
         f"[duck]volume={1 - floor:.2f}[d1];[m2]volume={floor:.2f}[d2];"
         "[sa][d1][d2]amix=inputs=3:duration=first:normalize=0")
    # pass 1: the loudness of the whole mix; pass 2: one linear gain to -16 LUFS + a limiter. No dynamic
    # normaliser over the programme: every moment keeps the level it was given (they are already equal).
    p = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(video), "-i", str(bed_wav), "-filter_complex",
                        g + ",ebur128=framelog=quiet[a]", "-map", "[a]", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600)
    summ = p.stderr[p.stderr.rfind("Summary:"):] if "Summary:" in p.stderr else ""
    m = re.search(r"I:\s+(-?[\d.]+) LUFS", summ)
    gain = max(-20.0, min(20.0, target - float(m.group(1)))) if m else 0.0
    g += f",volume={gain:.2f}dB,alimiter=limit=0.82:level=0:attack=3:release=80,aresample=48000[a]"
    p = subprocess.run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", str(video), "-i", str(bed_wav),
                        "-filter_complex", g, "-map", "0:v", "-map", "[a]", "-c:v", "copy", *aout, "-movflags", "+faststart",
                        str(out)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600)
    if p.returncode:
        raise RuntimeError(p.stderr[-400:])
    return out
