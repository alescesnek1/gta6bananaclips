"""
klicuj.py - key the green background out of the fanpage model's reaction stills.

  python tools/model/klicuj.py            # assets/model_reakce/*.png|jpg|webp -> transparent *.png
  python tools/model/klicuj.py --mrizka assets/model_reakce/mrizka.png   # split the 3x2 grid first
  python tools/model/klicuj.py --video    # grimasy*.mp4 (CapCut, green screen) -> stills + alpha .webm

Input: stills generated from docs/model-reakce-prompt.md on a pure green (#00FF00) background.
Output: same name .png with alpha, trimmed to the subject, green spill removed. Already
transparent PNGs are left alone, so it is safe to run again.
"""
import sys
from pathlib import Path

import numpy as np
from PIL import Image

DIR = Path(__file__).resolve().parents[2] / "assets" / "model_reakce"

def key(img):
    a = np.asarray(img.convert("RGB")).astype(np.float32)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    green = g - np.maximum(r, b)                              # how much greener than the other channels
    alpha = np.clip(1.0 - (green - 25) / 60, 0, 1)            # soft edge between 25 and 85
    # spill: pull leftover green down to the other channels on the edges
    a[..., 1] = np.minimum(g, np.maximum(r, b) + 8)
    out = np.dstack([a, alpha * 255]).astype(np.uint8)
    ys, xs = np.where(alpha > 0.1)
    if len(xs):
        out = out[max(0, ys.min() - 6): ys.max() + 6, max(0, xs.min() - 6): xs.max() + 6]
    return Image.fromarray(out, "RGBA")

NAMES = ["gasp", "laugh", "wink", "shocked", "smirk", "eyeroll"]   # grid order: left to right, top row first

def split_grid(path, cols=3, rows=2):
    """One generated 3x2 grid (docs/model-reakce-prompt.md) -> six stills named by reaction."""
    img = Image.open(path).convert("RGB")
    w, h = img.size
    cw, ch = w // cols, h // rows
    pad = max(2, min(cw, ch) // 60)                       # drop the white gaps between panels
    for i, name in enumerate(NAMES[: cols * rows]):
        c, r = i % cols, i // cols
        cell = img.crop((c * cw + pad, r * ch + pad, (c + 1) * cw - pad, (r + 1) * ch - pad))
        cell.save(DIR / f"{name}.png")
        print(f"grid -> {name}.png")
    if Path(path).resolve().parent == DIR.resolve():
        Path(path).unlink()                                # the grid itself is not a sticker

def frames(path, fps=10, w=270):
    """Small grey frames of a video (numpy) + the fps used."""
    import subprocess
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip().split(",")
    vw, vh = int(probe[0]), int(probe[1])
    h = int(round(w * vh / vw / 2) * 2)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"fps={fps},scale={w}:{h},format=gray",
                          "-f", "rawvideo", "-"], capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, h, w).astype(np.float32), fps

def split_video(path, names):
    """
    A CapCut grimace video (docs/model-reakce-prompt.md, step 3) -> one still + one short transparent
    clip per reaction. The video is cut into len(names) equal parts; in each part the frame that differs
    most from the neutral first frame is the peak of the reaction.
    """
    import subprocess
    fr, fps = frames(path)
    diff = np.abs(fr - fr[0]).mean(axis=(1, 2))
    diff = np.convolve(diff, np.ones(5) / 5, mode="same")          # smooth over 0.5 s
    # peaks: the strongest frames at least 1.6 s apart (neutral pauses separate the reactions)
    gap, picked = int(1.6 * fps), []
    for k in np.argsort(diff)[::-1]:
        if k < fps * 0.4 or k > len(fr) - 2:
            continue
        if all(abs(k - p) >= gap for p in picked):
            picked.append(int(k))
        if len(picked) == len(names):
            break
    picked.sort()
    for name, k in zip(names, picked):
        t = k / fps
        # Aleš 17. 11.: the whole waist-up shot (face and belly), like the banana; 1100 px tall
        crop = "scale=-2:1100"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
                        "-vf", crop, str(DIR / f"{name}.png")], check=True)
        t0 = max(0.0, t - 0.9)                            # ~1.8 s clip: build-up, peak, settle; green keyed to alpha
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t0:.2f}", "-t", "1.8", "-i", str(path),
                        "-vf", f"{crop},chromakey=0x00FF00:0.18:0.08,despill=type=green,format=yuva420p", "-an",
                        "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "32", "-auto-alt-ref", "0",
                        str(DIR / f"{name}.webm")], check=True)
        print(f"{Path(path).name} {t:.1f}s -> {name}.png + {name}.webm")

def main():
    DIR.mkdir(parents=True, exist_ok=True)
    if "--video" in sys.argv:
        vids = sorted(DIR.glob("grimasy*.mp4"))
        per = len(NAMES) // max(1, len(vids))
        for j, v in enumerate(vids):
            split_video(v, NAMES[j * per: (j + 1) * per] if j < len(vids) - 1 else NAMES[j * per:])
    if "--mrizka" in sys.argv:
        split_grid(sys.argv[sys.argv.index("--mrizka") + 1])
    for f in sorted(DIR.iterdir()):
        if f.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            continue
        img = Image.open(f)
        if img.mode == "RGBA" and np.asarray(img)[..., 3].min() < 250:
            print(f"{f.name}: already transparent")
            continue
        out = f.with_suffix(".png")
        key(img).save(out)
        if out != f:
            f.unlink()
        print(f"{f.name} -> {out.name}")

if __name__ == "__main__":
    main()
