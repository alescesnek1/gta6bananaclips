"""
sfx.py - our own transition sound (no samples, no licences).

  python tools/hudba/sfx.py      # -> assets/sfx/flash.wav

'flash': an airy whoosh that swells for 0.45 s into a soft cinematic impact (low boom + a
little air), for the hook -> start-of-clip white flash. The impact lands at 0.45 s.
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

def main():
    DEST.mkdir(parents=True, exist_ok=True)
    pcm = (flash() * 32767).astype(np.int16)
    with wave.open(str(DEST / "flash.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    print("flash.wav")

if __name__ == "__main__":
    main()
