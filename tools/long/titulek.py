"""
titulek.py - a short title over running footage (the story's chapter title "HOUR 2" / "11 PM"), animated
by the motion rules we use for every graphic: each word fades up from blurred to sharp with a small rise
(~0.08 s apart) and leaves the same way, no hard reveal edge, no bounce, no scale pop; the accent line
under it grows from the centre; the whole title keeps drifting up a few pixels, so it never stands still.

  pattern, x, y = titulek.frames("HOUR 2", tmp / "ch01", font, 150, dur=1.1, accent=(255, 79, 216))
  # ffmpeg: -framerate 30 -i pattern  ->  overlay=x:y:eof_action=pass

Transparent PNG frames (RGBA) on a canvas just big enough for the text. Pure function of time, Pillow only.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


def _ease(x):
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def frames(text, out_dir, font, px, dur=1.1, accent=(255, 255, 255), fps=30, size=(1920, 1080), stagger=0.08, word_in=0.30):
    """Write the frames -> (ffmpeg pattern, x, y of the canvas in the frame)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    f = ImageFont.truetype(str(font), px)
    words = text.split()
    pad = px // 2
    widths = [f.getlength(w) for w in words]
    gap = f.getlength(" ")
    total = sum(widths) + gap * (len(words) - 1)
    cw, ch = int(total) + 2 * pad, int(px * 1.9) + 2 * pad
    sprites, x = [], pad
    for w, wl in zip(words, widths):
        im = Image.new("RGBA", (int(wl) + 2 * pad, ch), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.text((pad, ch // 2 - px // 6), w, font=f, fill=(0, 0, 0, 150), anchor="lm", stroke_width=max(3, px // 22),
               stroke_fill=(0, 0, 0, 150))
        im = im.filter(ImageFilter.GaussianBlur(px / 30))       # a soft dark edge so it reads on any footage
        ImageDraw.Draw(im).text((pad, ch // 2 - px // 6), w, font=f, fill=(255, 255, 255, 255), anchor="lm")
        sprites.append((im, x - pad))
        x += wl + gap
    n = round(dur * fps)
    t_out = dur - word_in - stagger * (len(words) - 1)
    line_y = ch // 2 + int(px * 0.56)
    for i in range(n):
        t = i / fps
        canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        drift = -6.0 * t / dur
        for j, (spr, sx) in enumerate(sprites):
            e = _ease((t - stagger * j) / word_in) * (1 - _ease((t - t_out - stagger * j) / word_in))
            if e <= 0:
                continue
            leaving = t > t_out + stagger * j
            w = spr.filter(ImageFilter.GaussianBlur(px / 12 * (1 - e))) if e < 1 else spr.copy()
            if e < 1:
                w.putalpha(w.getchannel("A").point(lambda v: int(v * e)))
            canvas.alpha_composite(w, (int(sx), int(drift + (px * 0.12) * (1 - e) * (-1 if leaving else 1))))
        k = _ease((t - 0.12) / 0.45) * (1 - _ease((t - (dur - 0.3)) / 0.3))
        if k > 0:                                               # the accent line grows from the centre
            half = (total * 0.5) * k / 2
            ImageDraw.Draw(canvas).rounded_rectangle((cw / 2 - half, line_y + drift, cw / 2 + half, line_y + drift + max(4, px // 25)),
                                                     radius=3, fill=accent + (int(245 * min(1, k * 2)),))
        canvas.save(out_dir / f"{i:03d}.png")
    return str(out_dir / "%03d.png"), (size[0] - cw) // 2, (size[1] - ch) // 2
