"""
phonk.py - our own phonk beats for gta6bananaclips (no samples, no licences, no Content ID).

  python tools/hudba/phonk.py            # -> assets/hudba/phonk_<name>.wav, loopable 8 bars

Everything is synthesized with numpy: distorted 808 bass with slides, the signature melodic
cowbell riff, kick, clap, 16th hats with swing, a short room reverb. Three styles:
  drift   130 BPM, fast cowbell riff, the TikTok drift-phonk sound
  brazil  130 BPM, funk 'tamborzao' kick pattern, bouncy 808
  dark    90 BPM, slowed + reverb, sparse cowbell, deep 808
The riffs are short, repetitive and in a minor pentatonic so they stick and fit any clip.
"""
import sys
import wave
from pathlib import Path

import numpy as np

SR = 48000
DEST = Path(__file__).resolve().parents[2] / "assets" / "hudba"
N = {"F#2": 92.5, "A2": 110.0, "B2": 123.5, "C#3": 138.6, "D3": 146.8, "E3": 164.8, "F#3": 185.0,
     "F#4": 370.0, "A4": 440.0, "B4": 493.9, "C#5": 554.4, "D5": 587.3, "E5": 659.3, "F#5": 740.0, "A5": 880.0}

def env(n, decay, attack=0.002):
    t = np.arange(n) / SR
    return np.exp(-t / decay) * np.minimum(1, t / attack)

def lowpass(x, k):
    return np.convolve(x, np.ones(k) / k, mode="same")

def highpass(x, k):
    return x - lowpass(x, k)

def kick(vol=1.0):
    n = int(SR * 0.35)
    t = np.arange(n) / SR
    f = 45 + 110 * np.exp(-t / 0.03)
    s = np.sin(2 * np.pi * np.cumsum(f) / SR) * env(n, 0.12)
    s[:200] += np.random.default_rng(1).normal(0, 0.3, 200) * np.linspace(1, 0, 200)
    return vol * np.tanh(2.2 * s)

def bass808(f0, f1, dur, vol=0.8, drive=3.0):
    """808 with a slide from f0 to f1 (phonk's sliding bass), saturated."""
    n = int(SR * dur)
    t = np.arange(n) / SR
    f = f1 + (f0 - f1) * np.exp(-t / 0.06)
    s = np.sin(2 * np.pi * np.cumsum(f) / SR) * env(n, dur * 0.7, 0.004)
    return vol * np.tanh(drive * s) / np.tanh(drive)

def cowbell(f, dur=0.22, vol=0.35):
    """Melodic phonk cowbell: two detuned squares (ratio 1.48), band-limited, a bit of bite."""
    n = int(SR * dur)
    t = np.arange(n) / SR
    sq = np.sign(np.sin(2 * np.pi * f * t)) + 0.8 * np.sign(np.sin(2 * np.pi * f * 1.48 * t))
    s = highpass(lowpass(sq, 6), 40) * env(n, dur * 0.35, 0.001)
    return vol * np.tanh(1.8 * s)

def clap(vol=0.45, rnd=np.random.default_rng(2)):
    n = int(SR * 0.18)
    noise = highpass(lowpass(rnd.normal(0, 1, n), 4), 30)
    e = env(n, 0.05)
    for d in (0.0, 0.011, 0.022):                     # three quick hits = clap
        i = int(d * SR)
        e[i:] += 0.6 * env(n - i, 0.012)
    return vol * noise * e / 2.5 + 0.15 * vol * np.sin(2 * np.pi * 190 * np.arange(n) / SR) * env(n, 0.04)

def hat(vol=0.12, open_=False, rnd=np.random.default_rng(3)):
    n = int(SR * (0.18 if open_ else 0.04))
    return vol * highpass(rnd.normal(0, 1, n), 3) * env(n, 0.06 if open_ else 0.012)

def reverb(x, mix=0.18, length=0.7, seed=4):
    rnd = np.random.default_rng(seed)
    n = int(SR * length)
    ir = lowpass(rnd.normal(0, 1, n), 10) * np.exp(-np.arange(n) / (SR * length / 5))
    ir /= np.abs(ir).sum() / 4
    m = x.size + ir.size                               # FFT convolution (direct would take minutes)
    wet = np.fft.irfft(np.fft.rfft(x, m) * np.fft.rfft(ir, m), m)[: x.size]
    return (1 - mix) * x + mix * wet

def place(buf, sig, t):
    i = int(t * SR) % buf.size                         # wraps around: the loop stays seamless
    j = i + sig.size
    if j <= buf.size:
        buf[i:j] += sig
    else:
        k = buf.size - i
        buf[i:] += sig[:k]
        buf[: j - buf.size] += sig[k:]

STYLES = {
    "drift": dict(bpm=130, swing=0.08,
                  kick="x.......x.x.....x.......x...x...",
                  clap="....x.......x.......x.......x...",
                  # 2-bar cowbell call + answer (16ths), '-' = rest
                  bell=["F#5", "-", "F#5", "-", "E5", "-", "C#5", "-", "F#5", "-", "F#5", "-", "A5", "-", "E5", "-",
                        "F#5", "-", "F#5", "-", "E5", "-", "C#5", "-", "B4", "-", "C#5", "-", "A4", "-", "-", "-"],
                  bass=[(0, "F#2", 1.4), (6, "A2", 0.6), (8, "F#2", 1.2), (12, "E3", 0.5), (14, "C#3", 0.6),
                        (16, "F#2", 1.4), (22, "A2", 0.6), (24, "B2", 1.0), (28, "A2", 0.5), (30, "E3", 0.5)]),
    "brazil": dict(bpm=130, swing=0.0,
                   kick="x..x..x...x..x..x..x..x...x.x...",
                   clap="....x..x....x.......x..x....x...",
                   bell=["F#5", "-", "-", "F#5", "-", "-", "E5", "-", "F#5", "-", "-", "A5", "-", "-", "F#5", "-",
                         "C#5", "-", "-", "C#5", "-", "-", "B4", "-", "C#5", "-", "-", "E5", "-", "-", "C#5", "-"],
                   bass=[(0, "F#2", 0.4), (3, "F#2", 0.4), (6, "A2", 0.5), (10, "F#2", 0.4), (13, "E3", 0.3),
                         (16, "C#3", 0.4), (19, "C#3", 0.4), (22, "B2", 0.5), (26, "C#3", 0.4), (28, "E3", 0.4)]),
    "dark": dict(bpm=90, swing=0.12,
                 kick="x.........x.....x.....x...x.....",
                 clap="........x...............x.......",
                 bell=["F#5", "-", "-", "-", "E5", "-", "C#5", "-", "-", "-", "B4", "-", "C#5", "-", "-", "-",
                       "F#5", "-", "-", "-", "A5", "-", "F#5", "-", "-", "-", "E5", "-", "C#5", "-", "-", "-"],
                 bass=[(0, "F#2", 2.2), (10, "D3", 1.0), (16, "B2", 2.0), (24, "C#3", 1.4), (30, "F#3", 0.4)]),
}

def render(name):
    st = STYLES[name]
    step = 60 / st["bpm"] / 4                          # one 16th
    bars, steps = 8, 32                                # 8 bars = the 2-bar pattern x4
    total = bars * 16 * step
    buf = np.zeros(int(total * SR))
    drums = np.zeros_like(buf)
    rnd = np.random.default_rng(len(name))
    for rep in range(bars // 2):
        base = rep * steps * step
        for i in range(steps):
            t = base + i * step + (st["swing"] * step if i % 2 else 0)
            if st["kick"][i] == "x":
                place(drums, kick(), t)
            if st["clap"][i] == "x":
                place(drums, clap(), t)
            if name != "dark" or i % 2 == 0:            # hats: 16ths (8ths when slowed)
                place(drums, hat(0.07 + 0.05 * (i % 4 == 2) + rnd.uniform(0, 0.02), open_=(i % 8 == 6)), t)
            note = st["bell"][i]
            if note != "-":
                f = N[note] * (0.5 if name == "dark" else 1)
                place(buf, cowbell(f, 0.3 if name == "dark" else 0.2), t)
        for s, note, dur in st["bass"]:
            f = N[note]
            prev = f * 1.5                              # slide down into every note
            place(buf, bass808(prev, f, dur * step * 4), base + s * step)
    mix = reverb(buf, 0.3 if name == "dark" else 0.15) + drums
    mix = np.tanh(1.3 * mix)                            # glue / loudness
    return mix / np.abs(mix).max() * 0.89

def main():
    DEST.mkdir(parents=True, exist_ok=True)
    for name in sys.argv[1:] or list(STYLES):
        pcm = (render(name) * 32767).astype(np.int16)
        out = DEST / f"phonk_{name}.wav"
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm.tobytes())
        print(f"{out.name}: {pcm.size / SR:.1f} s")

if __name__ == "__main__":
    main()
