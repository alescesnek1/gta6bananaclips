"""
follow.py - the 'Follow' end card of every short: a small profile card (round picture, name,
@handle, Follow button), a cursor clicks, the button turns into 'Following' with a tick and a
soft pop. No network's logo: the same card runs on all 6 networks.

  python tools/follow/follow.py              # -> assets/follow/follow_<brand>.webm (VP9 + alpha, Opus)
  python tools/follow/follow.py fanpage      # one brand only

Needs Pillow + numpy (any Python, not the project venv) and ffmpeg. Everything is drawn here
(fonts from fonts/, OFL); the click is synthesized. The film is a pure function of time:
frame(t) draws the whole picture, motion = closed-form springs.
"""
import math
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parents[2]
DEST = HERE / "assets" / "follow"
W, H, FPS, DUR, SR = 1080, 1920, 30, 2.2, 48000
S = 2                                    # supersampling of every sprite

BRANDS = {
    "fanpage": {"name": "GTA 6 Fanpage Clip", "handle": "@gta6fanpageclip", "pfp": "assets/brand/pfp_fanpage.webp",
                "accent": (255, 79, 216), "on_accent": (255, 255, 255), "font": "fonts/cap/anton.ttf", "name_px": 58},
    "bananas": {"name": "GTA 6 Banana Clips", "handle": "@gta6bananaclips", "pfp": "assets/brand/pfp_bananas.webp",
                "accent": (255, 225, 53), "on_accent": (20, 20, 26), "font": "fonts/cap/montserrat-900.ttf", "name_px": 46},
}
UI_FONT = "fonts/montserrat-600.ttf"

# layout in the 1080x1920 short: the card sits in the lower third, above the streamer's
# watermark (y 1600) and the caption bar of TikTok / Reels
CARD_W, CARD_H, CARD_CY = 1000, 220, 1400
PFP_D, PAD = 150, 34
BTN_W, BTN_H = 262, 92
CARD_BG, CARD_EDGE, TEXT, MUTED, BTN_OFF = (17, 17, 23), (58, 58, 70), (255, 255, 255), (165, 165, 180), (38, 38, 48)

# timeline (s)
T_BTN, T_CUR, T_PRESS, T_SWITCH = 0.22, 0.42, 0.98, 1.08


def spring(t, freq=3.2, damp=0.45):
    """0 -> 1 with overshoot: step response of an underdamped spring, closed form."""
    if t <= 0:
        return 0.0
    w = 2 * math.pi * freq
    wd = w * math.sqrt(1 - damp * damp)
    return 1 - math.exp(-damp * w * t) * (math.cos(wd * t) + damp * w / wd * math.sin(wd * t))


def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))


def ease(x):
    x = clamp(x)
    return 1 - (1 - x) ** 3


def font(path, px):
    return ImageFont.truetype(str(HERE / path), int(px * S))


def fit(path, text, px, width):
    """Font at px, smaller when the text would not fit `width`."""
    while px > 20 and font(path, px).getlength(text) > width * S:
        px -= 1
    return font(path, px)


def text_sprite(text, f, col):
    l, t, r, b = f.getbbox(text)
    im = Image.new("RGBA", (r - l + 8, b - t + 8), (0, 0, 0, 0))
    ImageDraw.Draw(im).text((4 - l, 4 - t), text, font=f, fill=col)
    return im


def rounded(w, h, r, fill, edge=None, edge_w=0):
    im = Image.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
    ImageDraw.Draw(im).rounded_rectangle((0, 0, w * S - 1, h * S - 1), r * S, fill=fill, outline=edge, width=edge_w * S)
    return im


def sprites(b):
    acc = b["accent"]
    sp = {}
    # card: dark, thin edge, soft shadow under it (readable on any gameplay)
    m = 60
    card = Image.new("RGBA", ((CARD_W + 2 * m) * S, (CARD_H + 2 * m) * S), (0, 0, 0, 0))
    sh = Image.new("RGBA", card.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle((m * S, (m + 14) * S, (m + CARD_W) * S, (m + 14 + CARD_H) * S), 56 * S, fill=(0, 0, 0, 150))
    card.alpha_composite(sh.filter(ImageFilter.GaussianBlur(22 * S)))
    card.alpha_composite(rounded(CARD_W, CARD_H, 56, CARD_BG + (238,), CARD_EDGE + (255,), 2), (m * S, m * S))
    sp["card"] = card
    # profile picture in a circle with an accent ring
    d = PFP_D * S
    pic = Image.open(HERE / b["pfp"]).convert("RGB")
    side = min(pic.size)
    pic = pic.crop(((pic.width - side) // 2, (pic.height - side) // 2, (pic.width + side) // 2, (pic.height + side) // 2))
    inner = d - 16 * S
    mask = Image.new("L", (inner, inner), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, inner - 1, inner - 1), fill=255)
    pfp = Image.new("RGBA", (d, d), (0, 0, 0, 0))
    ImageDraw.Draw(pfp).ellipse((0, 0, d - 1, d - 1), outline=acc + (255,), width=5 * S)
    pfp.paste(pic.resize((inner, inner), Image.LANCZOS), (8 * S, 8 * S), mask)
    sp["pfp"] = pfp
    text_w = CARD_W - PAD - PFP_D - 26 - 24 - BTN_W - PAD
    sp["name"] = text_sprite(b["name"], fit(b["font"], b["name"], b["name_px"], text_w), TEXT + (255,))
    sp["handle"] = text_sprite(b["handle"], fit(UI_FONT, b["handle"], 34, text_w), MUTED + (255,))
    # buttons
    f = font(UI_FONT, 38)
    on = rounded(BTN_W, BTN_H, BTN_H // 2, acc + (255,))
    t = text_sprite("Follow", f, b["on_accent"] + (255,))
    on.alpha_composite(t, ((on.width - t.width) // 2, (on.height - t.height) // 2))
    sp["follow"] = on
    off = rounded(BTN_W, BTN_H, BTN_H // 2, BTN_OFF + (255,), acc + (255,), 3)
    f2 = font(UI_FONT, 31)
    t = text_sprite("Following", f2, TEXT + (255,))
    tick = 30 * S
    x0 = (off.width - t.width - tick - 10 * S) // 2
    off.alpha_composite(t, (x0, (off.height - t.height) // 2))
    cx, cy = x0 + t.width + 10 * S, off.height // 2
    ImageDraw.Draw(off).line([(cx, cy), (cx + tick * 0.36, cy + tick * 0.34), (cx + tick, cy - tick * 0.38)],
                             fill=acc + (255,), width=6 * S, joint="curve")
    sp["following"] = off
    # cursor: a plain arrow pointer, white with a dark edge (tip at the sprite's top-left corner)
    c = 64 * S
    cur = Image.new("RGBA", (c + 16 * S, c + 16 * S), (0, 0, 0, 0))
    pts = [(0, 0), (0, 0.80), (0.21, 0.61), (0.36, 0.95), (0.50, 0.89), (0.35, 0.56), (0.60, 0.56)]
    pts = [(4 * S + x * c, 4 * S + y * c) for x, y in pts]
    shd = Image.new("RGBA", cur.size, (0, 0, 0, 0))
    ImageDraw.Draw(shd).polygon([(x + 3 * S, y + 5 * S) for x, y in pts], fill=(0, 0, 0, 130))
    cur.alpha_composite(shd.filter(ImageFilter.GaussianBlur(4 * S)))
    dr = ImageDraw.Draw(cur)
    dr.polygon(pts, fill=(255, 255, 255, 255))
    dr.line(pts + [pts[0]], fill=(20, 20, 26, 255), width=3 * S, joint="curve")
    sp["cursor"] = cur
    return sp


def put(canvas, sprite, cx, cy, scale=1.0, alpha=1.0, anchor="c"):
    """Sprite (drawn at S x) onto the 1x canvas: centre (or top-left corner) at cx, cy."""
    if alpha <= 0.003 or scale <= 0.01:
        return
    w, h = max(1, round(sprite.width * scale / S)), max(1, round(sprite.height * scale / S))
    im = sprite.resize((w, h), Image.LANCZOS)
    if alpha < 0.997:
        im.putalpha(im.getchannel("A").point(lambda v: int(v * alpha)))
    x, y = (round(cx - w / 2), round(cy - h / 2)) if anchor == "c" else (round(cx), round(cy))
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    layer.paste(im, (x, y))
    canvas.alpha_composite(layer)


def frame(t, sp, b):
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    k = spring(t, 2.6, 0.55)                                  # the card rises 90 px and settles
    a = clamp(t / 0.16)
    cx, cy = W / 2, CARD_CY + (1 - k) * 90
    left = cx - CARD_W / 2
    put(im, sp["card"], cx, cy, 0.9 + 0.1 * k, a)
    put(im, sp["pfp"], left + PAD + PFP_D / 2, cy, spring(t - 0.08, 3.0, 0.42), clamp((t - 0.08) / 0.1))
    tx = left + PAD + PFP_D + 26
    for name, dy, t0 in (("name", -27, 0.14), ("handle", 33, 0.20)):
        e = ease((t - t0) / 0.28)
        put(im, sp[name], tx + (1 - e) * 36, cy + dy - sp[name].height / S / 2, 1.0, e, anchor="tl")
    bx, by = left + CARD_W - PAD - BTN_W / 2, cy
    press = clamp((t - T_PRESS) / 0.05) * (t < T_SWITCH)
    if t < T_SWITCH:
        put(im, sp["follow"], bx, by, spring(t - T_BTN, 3.0, 0.45) * (1 - 0.09 * press), clamp((t - T_BTN) / 0.1))
    else:
        u = t - T_SWITCH
        put(im, sp["following"], bx, by, 0.9 + 0.1 * spring(u, 4.2, 0.3))     # the pop
        r = ease(u / 0.42)                                    # one soft ring from the button
        if r < 1:
            ring = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            gw, gh = BTN_W / 2 + 6 + 46 * r, BTN_H / 2 + 6 + 46 * r
            ImageDraw.Draw(ring).rounded_rectangle((bx - gw, by - gh, bx + gw, by + gh), gh, outline=b["accent"] + (int(200 * (1 - r)),),
                                                   width=max(1, round(7 * (1 - r))))
            im.alpha_composite(ring)
    # cursor: glides in from the lower right, presses, drifts off
    e = ease((t - T_CUR) / (T_PRESS - T_CUR - 0.04))
    out = ease((t - T_SWITCH - 0.25) / 0.45)
    px = bx + 46 + (1 - e) * 150 + out * 70
    py = by + 8 + (1 - e) * 230 + out * 150
    put(im, sp["cursor"], px - 4, py - 4, 1 - 0.14 * press, clamp((t - T_CUR) / 0.12) * (1 - out), anchor="tl")
    return im


def click_sound():
    """Soft and short: a muted press 'tk', then a small rising 'pop' on the switch."""
    buf = np.zeros(int(SR * DUR))

    def add(sig, at):
        i = int(at * SR)
        buf[i: i + sig.size] += sig[: buf.size - i]

    n = int(SR * 0.03)
    t = np.arange(n) / SR
    add(0.30 * np.sin(2 * np.pi * 1900 * t) * np.exp(-t / 0.004) + 0.25 * np.sin(2 * np.pi * 240 * t) * np.exp(-t / 0.010), T_PRESS)
    n = int(SR * 0.16)
    t = np.arange(n) / SR
    f = 520 + 520 * np.minimum(1, t / 0.05)
    pop = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.035) * np.minimum(1, t * 600)
    add(0.5 * pop + 0.12 * np.sin(2 * np.pi * 1568 * t) * np.exp(-t / 0.05) * np.minimum(1, t * 600), T_SWITCH)
    return np.convolve(buf, np.ones(4) / 4, mode="same") * 0.8


def build(key):
    b = BRANDS[key]
    sp = sprites(b)
    DEST.mkdir(parents=True, exist_ok=True)
    out = DEST / f"follow_{key}.webm"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for i in range(round(DUR * FPS)):
            frame(i / FPS, sp, b).save(tmp / f"{i:04d}.png")
        with wave.open(str(tmp / "click.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes((click_sound() * 32767).astype(np.int16).tobytes())
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-framerate", str(FPS), "-i", str(tmp / "%04d.png"),
                        "-i", str(tmp / "click.wav"), "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0", "-crf", "30",
                        "-row-mt", "1", "-c:a", "libopus", "-b:a", "96k", str(out)], check=True)
    print(f"{out.name}: {out.stat().st_size / 1e3:.0f} kB")


if __name__ == "__main__":
    for k in sys.argv[1:] or BRANDS:
        build(k)
