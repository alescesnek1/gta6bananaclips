"""
klicuj.py - key the green background out of the fanpage model's reaction stills.

  python tools/model/klicuj.py            # assets/model_reakce/*.png|jpg|webp -> transparent *.png
  python tools/model/klicuj.py --mrizka assets/model_reakce/mrizka.png   # split the 3x2 grid first

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

def main():
    DIR.mkdir(parents=True, exist_ok=True)
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
