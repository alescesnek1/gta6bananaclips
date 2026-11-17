"""
sestav.py - sound + final files for the banana score cards rendered by banany.py.

  python tools/rating/sestav.py <render_dir> [score_10_gold ...]   # -> assets/rating/<name>.webm (VP9 + alpha, Opus)

Sound is synthesized (numpy, no samples, no licences) and kept SOFT (Aleš: not disturbing,
it sits under the clip's own audio): every banana that lands = a muted low 'tup', the score =
a gentle two-to-four note chime whose mood follows the score (down for 1-3, up for 7-10),
8+ a faint sparkle. The rare GOLDEN 10/10 (banany.py --gold -> score_10_gold.json) gets its own
sparkle: a fast run of high glass bells climbing two octaves over a warm shimmer.
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

# ---------------------------------------------------------------- alternative sound styles

def knock(vol=0.8):
    """
    The landing 'tup' that stays audible over music: a short mid-range body (420 -> 190 Hz,
    above the beat's bass) plus a woody 1.6 kHz click, both gone in ~0.1 s.
    """
    body = tone(420, 190, 0.12, decay=30, vol=vol)
    n = int(SR * 0.03)
    t = np.arange(n) / SR
    click = 0.35 * vol * np.sin(2 * np.pi * 1600 * t) * np.exp(-t / 0.006)
    return soft(add(body, click), 3)

def add(*sigs):
    """Sum signals of different lengths."""
    out = np.zeros(max(x.size for x in sigs))
    for x in sigs:
        out[: x.size] += x
    return out

PENTA = [392, 440, 523, 587, 659, 784, 880, 1047, 1175, 1319]     # landings climb a pentatonic scale

def marimba(f, vol=0.22):
    """Wooden mallet: fundamental + a 4x overtone that dies fast."""
    return soft(add(tone(f, f, 0.35, decay=14, vol=vol), tone(4 * f, 4 * f, 0.08, decay=60, vol=vol * 0.25)), 4)

def bubble(f, vol=0.2):
    """A soft bubble 'blup': quick upward sweep."""
    return soft(tone(f, f * 2.2, 0.07, decay=35, vol=vol), 6)

def cushion(rnd, vol=0.3):
    """Pillow thump: low sine + heavily muffled noise."""
    n = int(SR * 0.18)
    noise = soft(rnd.normal(0, 1, n), 260) * np.exp(-np.arange(n) / (SR * 0.04))
    return add(tone(75, 55, 0.18, decay=18, vol=vol), vol * 2.5 * noise)

def whoosh(rnd, dur=0.22, vol=0.05):
    """Air before a landing: filtered noise swelling up."""
    n = int(SR * dur)
    return vol * soft(rnd.normal(0, 1, n), 40) * np.linspace(0, 1, n) ** 2

def tri(f, dur, vol=0.12):
    t = np.arange(int(SR * dur)) / SR
    w = 2 * np.abs(2 * ((f * t) % 1) - 1) - 1
    return soft(vol * w * np.exp(-8 * t) * np.minimum(1, t * 300), 6)

def bell(f, vol=0.12):
    """Small glass bell: fundamental + inharmonic partials, long soft tail."""
    return add(tone(f, f, 1.2, decay=3, vol=vol), tone(f * 2.76, f * 2.76, 0.6, decay=6, vol=vol * 0.35),
               tone(f * 5.4, f * 5.4, 0.3, decay=12, vol=vol * 0.12))

def score_tune(style, score):
    up = score >= 7
    if style == "marimba":
        notes = [784, 1047, 1319] if score == 10 else [659, 784] if up else [523, 440] if score <= 3 else [523, 587]
        out = np.zeros(int(SR * 0.9))
        for i, f in enumerate(notes):
            put(out, marimba(f, 0.2), i * 0.11)
        return out
    if style == "bubliny":
        return add(bell(1760 if up else 1320 if score > 3 else 990, 0.1), bubble(700, 0.12))
    if style == "plys":
        return bell(880 if up else 660 if score > 3 else 523, 0.13)
    if style == "arkada":
        notes = [988, 1319] if up else [784, 659] if score <= 3 else [784, 880]
        out = np.zeros(int(SR * 0.6))
        for i, f in enumerate(notes):
            put(out, tri(f, 0.25 if i else 0.09), i * 0.09)
        return out
    return tune(score)

def gold_sparkle(rnd):
    """The golden 10/10's own sound: 9 high bells climbing fast (pentatonic, two octaves), a warm
    low shimmer under them and fine glitter noise - unlike any other card, still soft."""
    out = np.zeros(int(SR * 1.8))
    for i, f in enumerate([1047, 1175, 1319, 1568, 1760, 2093, 2349, 2637, 3136]):
        put(out, bell(f, 0.07), i * 0.055)
    t = np.arange(int(SR * 1.6)) / SR
    shimmer = 0.05 * (np.sin(2 * np.pi * 262 * t) + 0.6 * np.sin(2 * np.pi * 392 * t)) * np.exp(-2.2 * t) * np.minimum(1, t * 20)
    put(out, shimmer * (1 + 0.3 * np.sin(2 * np.pi * 7 * t)), 0.0)
    put(out, crackle(rnd, 1.4) * 0.8, 0.1)
    return out

def sound(meta, style="jemne"):
    """jemne = current default; marimba, bubliny, plys, arkada = alternatives to choose from."""
    rnd = np.random.default_rng(meta["score"])
    buf = np.zeros(int(SR * meta["dur"]) + SR)
    k = 0
    for t, kind in meta["pops"]:
        if kind == "thud":
            if style == "stejny":                      # every banana lands with the very same sound
                put(buf, knock(), t)
            elif style == "marimba":
                put(buf, marimba(PENTA[min(k, 9)]), t)
            elif style == "bubliny":
                put(buf, bubble(PENTA[min(k, 9)] * 0.6), t)
            elif style == "plys":
                put(buf, whoosh(rnd), max(0.0, t - 0.22))
                put(buf, cushion(rnd), t)
            elif style == "arkada":
                put(buf, tri(PENTA[min(k, 9)] * 0.5, 0.08, 0.1), t)
            else:
                put(buf, bonk(rnd), t)
            k += 1
        elif kind == "num":
            put(buf, score_tune(style, meta["score"]), t)
            if meta.get("gold"):
                put(buf, gold_sparkle(rnd), t + 0.05)
        elif kind == "burst":
            put(buf, crackle(rnd), t)
    buf = buf[: int(SR * meta["dur"])]
    master = 0.85 if style == "stejny" else 0.45       # the landing tup must cut through the music
    buf *= master / max(1e-6, np.abs(buf).max())
    return buf

STYLE = "stejny"                                      # chosen by Aleš 15. 11.: every banana the same audible tup

def main():
    src = Path(sys.argv[1])
    DEST.mkdir(parents=True, exist_ok=True)
    for js in sorted(src.glob("score_*.json")):
        meta = json.loads(js.read_text())
        n = meta["score"]
        name = f"score_{n}_gold" if meta.get("gold") else f"score_{n}"
        if len(sys.argv) > 2 and name not in sys.argv[2:]:
            continue                                     # only the names asked for (e.g. score_10_gold)
        wav = src / f"{name}.wav"
        pcm = (sound(meta, STYLE) * 32767).astype(np.int16)
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm.tobytes())
        out = DEST / f"{name}.webm"
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-framerate", "30", "-i", str(src / name / "%04d.png"),
                        "-i", str(wav), "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0", "-crf", "34",
                        "-row-mt", "1", "-c:a", "libopus", "-b:a", "96k", "-shortest", str(out)], check=True)
        print(f"{out.name}: {out.stat().st_size / 1e6:.1f} MB")

if __name__ == "__main__":
    main()
