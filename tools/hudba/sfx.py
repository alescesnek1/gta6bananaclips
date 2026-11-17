"""
sfx.py - our own transition sound (no samples, no licences).

  python tools/hudba/sfx.py      # -> assets/sfx/flash.wav, riser.wav, hit.wav

'flash': an airy whoosh that swells for 0.45 s into a soft cinematic impact (low boom + a
little air), for the hook -> start-of-clip white flash. The impact lands at 0.45 s.
'riser': 1.2 s of quiet tension before the payoff - filtered noise opening up + a tone gliding up.
'hit': a short deep thump exactly on the payoff (starts at 0 s).
'whip': a short fast air swish for the fanpage hook's hard cut (the cut lands at 0.12 s).
"""
import wave
from pathlib import Path

import numpy as np

SR = 48000
DEST = Path(__file__).resolve().parents[2] / "assets" / "sfx"

def lowpass(x, k):
    return np.convolve(x, np.ones(k) / k, mode="same")

def flash():
    rnd = np.random.default_rng(11)
    n_w, n_i = int(SR * 0.45), int(SR * 1.1)
    out = np.zeros(n_w + n_i)
    # whoosh: noise whose brightness and level rise towards the hit
    noise = rnd.normal(0, 1, n_w)
    ramp = np.linspace(0, 1, n_w)
    dark, bright = lowpass(noise, 40), lowpass(noise, 6)
    out[:n_w] = 0.35 * ((1 - ramp) * dark + ramp * bright) * ramp ** 2.2
    # impact: low boom sliding down + short muffled air burst
    t = np.arange(n_i) / SR
    f = 40 + 55 * np.exp(-t / 0.08)
    boom = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.35)
    air = lowpass(rnd.normal(0, 1, n_i), 25) * np.exp(-t / 0.12)
    out[n_w:] += 0.9 * np.tanh(1.5 * boom) + 0.35 * air
    out[: int(SR * 0.01)] *= np.linspace(0, 1, int(SR * 0.01))
    return out / np.abs(out).max() * 0.8

def riser(dur=1.2):
    rnd = np.random.default_rng(23)
    n = int(SR * dur)
    t = np.arange(n) / SR
    ramp = t / dur
    noise = rnd.normal(0, 1, n)
    air = (1 - ramp) * lowpass(noise, 60) + ramp * lowpass(noise, 5)      # opens up towards the payoff
    f = 180 * 2 ** (2.5 * ramp)                                           # tone gliding up 2.5 octaves
    tone = np.sin(2 * np.pi * np.cumsum(f) / SR) + 0.3 * np.sin(4 * np.pi * np.cumsum(f) / SR)
    out = (0.5 * air + 0.35 * tone) * ramp ** 2
    out[-int(SR * 0.01):] *= np.linspace(1, 0, int(SR * 0.01))           # stops dead at the hit
    return out / np.abs(out).max() * 0.8

def hit():
    rnd = np.random.default_rng(31)
    n = int(SR * 0.7)
    t = np.arange(n) / SR
    f = 38 + 70 * np.exp(-t / 0.04)
    boom = np.tanh(2.0 * np.sin(2 * np.pi * np.cumsum(f) / SR)) * np.exp(-t / 0.18)
    click = lowpass(rnd.normal(0, 1, n), 4) * np.exp(-t / 0.008)
    out = boom + 0.4 * click
    out[: int(SR * 0.002)] *= np.linspace(0, 1, int(SR * 0.002))
    return out / np.abs(out).max() * 0.85

def whip():
    rnd = np.random.default_rng(41)
    n = int(SR * 0.3)
    t = np.arange(n) / SR
    noise = rnd.normal(0, 1, n)
    k = np.clip(t / 0.12, 0, 1)                                           # brighter towards the cut
    air = (1 - k) * lowpass(noise, 30) + k * lowpass(noise, 4)
    env = np.where(t < 0.12, (t / 0.12) ** 2, np.exp(-(t - 0.12) / 0.05))
    out = air * env
    out[: int(SR * 0.004)] *= np.linspace(0, 1, int(SR * 0.004))
    return out / np.abs(out).max() * 0.6

def save(name, x):
    pcm = (x * 32767).astype(np.int16)
    with wave.open(str(DEST / name), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    print(name)

def main():
    DEST.mkdir(parents=True, exist_ok=True)
    save("flash.wav", flash())
    save("riser.wav", riser())
    save("hit.wav", hit())
    save("whip.wav", whip())

if __name__ == "__main__":
    main()
