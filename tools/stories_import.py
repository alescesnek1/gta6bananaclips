"""
stories_import.py - generated story images (ChatGPT, docs/stories-prompty.md) -> assets/stories/<brand>/.

Each image becomes a 1080x1920 JPEG (cover crop, kept a little above centre so faces stay in), without
EXIF / metadata, named <brand>_NN.jpg. The pool holds 20 images per brand; stories.py rotates them
(never the same image within 5 days). Then commit assets/stories/ so GitHub Actions has them.

  .venv/Scripts/python tools/stories_import.py bananas C:/Users/Ales/Downloads/banan*.png
  .venv/Scripts/python tools/stories_import.py fanpage img1.png img2.webp --nahradit 07   # replace fanpage_07
"""
import argparse
import sys
from pathlib import Path

from PIL import Image, ImageOps

HERE = Path(__file__).resolve().parents[1]
W, H, MAX = 1080, 1920, 20


def normalise(src, dst):
    im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    s = max(W / im.width, H / im.height)
    im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
    left = (im.width - W) // 2
    top = int((im.height - H) * 0.4)                  # a bit above centre: heads are rarely at the bottom
    im.crop((left, top, left + W, top + H)).save(dst, "JPEG", quality=88, optimize=True)


def main():
    ap = argparse.ArgumentParser(description="import story images into the pool")
    ap.add_argument("znacka", choices=["bananas", "fanpage"])
    ap.add_argument("files", nargs="+")
    ap.add_argument("--nahradit", help="replace this number (01-20) instead of filling the next free one")
    a = ap.parse_args()
    out = HERE / "assets" / "stories" / a.znacka
    out.mkdir(parents=True, exist_ok=True)
    taken = {p.stem.rsplit("_", 1)[-1] for p in out.glob(f"{a.znacka}_*.jpg")}
    free = [a.nahradit.zfill(2)] if a.nahradit else [f"{i:02d}" for i in range(1, MAX + 1) if f"{i:02d}" not in taken]
    if len(a.files) > len(free):
        sys.exit(f"pool {a.znacka}: only {len(free)} free places of {MAX} (use --nahradit NN)")
    for src, n in zip(a.files, free):
        dst = out / f"{a.znacka}_{n}.jpg"
        normalise(src, dst)
        print(f"{src} -> {dst.relative_to(HERE)}")


if __name__ == "__main__":
    main()
