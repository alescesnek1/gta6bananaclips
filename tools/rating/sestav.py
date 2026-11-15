"""
sestav.py - sound + final files for the banana score cards rendered by banany.py.

  python tools/rating/sestav.py <render_dir>       # -> assets/rating/score_N.webm (VP9 + alpha, Opus)

Sound is synthesized (numpy, no samples, no licences) and kept SOFT (Aleš: not disturbing,
it sits under the clip's own audio): every banana that lands = a muted low 'tup', the score =
a gentle two-to-four note chime whose mood follows the score (down for 1-3, up for 7-10),
8+ a faint sparkle.
"""
import json
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

SR = 48000
HERE = Path(__file__).resolve().parents[2]
DEST = HERE / "assets" / "rating"

def tone(f0, f1, dur, shape="sine", decay=6.0, vol=0.5):
    t = np.arange(int(SR * dur)) / SR
    f = np.linspace(f0, f1, t.size)
    ph = 2 * np.pi * np.cumsum(f) / SR
    w = np.sin(ph) if shape == "sine" else np.sign(np.sin(ph)) * 0.6 + np.sin(2 * ph) * 0.2
    env = np.exp(-decay * t) * np.minimum(1, t * 400)
    return vol * w * env

def soft(x, n=24):
    """Simple low-pass (moving average): no clicks, nothing sharp."""
    return np.convolve(x, np.ones(n) / n, mode="same")

def bonk(rnd):
    base = rnd.uniform(110, 160)
    s = tone(base * 1.3, base * 0.8, 0.16, decay=24, vol=0.35)             # muted low 'tup'
    return soft(s * np.minimum(1, np.arange(s.size) / (SR * 0.006)))        # 6 ms fade-in

def tune(score):
    notes = {
        "down": [(587, 0.16), (440, 0.4)],
        "meh": [(523, 0.14), (587, 0.32)],
        "up": [(523, 0.1), (659, 0.1), (784, 0.36)],
        "top": [(523, 0.09), (659, 0.09), (784, 0.09), (1047, 0.5)],
    }["down" if score <= 3 else "meh" if score <= 6 else "up" if score <= 9 else "top"]
    out = []
    for f, d in notes:                                   # sine + a soft octave: a little chime
        n = tone(f, f, d, decay=4 if d > 0.3 else 9, vol=0.16) + tone(2 * f, 2 * f, d, decay=8, vol=0.04)
        out.append(n)
    return soft(np.concatenate(out), 8)

def crackle(rnd, dur=0.9):
    s = np.zeros(int(SR * dur))
    for _ in range(70):
        i = rnd.integers(0, s.size - 400)
        s[i: i + 400] += rnd.normal(0, 0.04, 400) * np.exp(-np.arange(400) / 60)
    return soft(s, 6)

def put(buf, sig, t):
    i = int(t * SR)
    j = min(buf.size, i + sig.size)
    if i < buf.size:
        buf[i:j] += sig[: j - i]

def sound(meta):
    rnd = np.random.default_rng(meta["score"])
    buf = np.zeros(int(SR * meta["dur"]) + SR)
    for t, kind in meta["pops"]:
        if kind == "thud":
            put(buf, bonk(rnd), t)
        elif kind == "num":
            put(buf, tune(meta["score"]), t)
        elif kind == "burst":
            put(buf, crackle(rnd), t)
    buf = buf[: int(SR * meta["dur"])]
    buf *= 0.45 / max(1e-6, np.abs(buf).max())         # quiet master; the clip's audio stays on top
    return buf

def main():
    src = Path(sys.argv[1])
    DEST.mkdir(parents=True, exist_ok=True)
    for js in sorted(src.glob("score_*.json")):
        meta = json.loads(js.read_text())
        n = meta["score"]
        wav = src / f"score_{n}.wav"
        pcm = (sound(meta) * 32767).astype(np.int16)
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm.tobytes())
        out = DEST / f"score_{n}.webm"
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-framerate", "30", "-i", str(src / f"score_{n}" / "%04d.png"),
                        "-i", str(wav), "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0", "-crf", "34",
                        "-row-mt", "1", "-c:a", "libopus", "-b:a", "96k", "-shortest", str(out)], check=True)
        print(f"{out.name}: {out.stat().st_size / 1e6:.1f} MB")

if __name__ == "__main__":
    main()
