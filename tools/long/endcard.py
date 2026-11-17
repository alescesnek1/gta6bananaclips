"""
endcard.py - the animated end screen of the long-form video (1920x1080, 30 fps), one per brand.
It leaves clean, empty zones for YouTube's end-screen elements (two videos + the subscribe circle);
only quiet frames mark them, nothing pretends to be a button. The only text is the channel name.

How kompilace.py calls it (instead of render_end):

    sys.path.insert(0, str(HERE / "tools" / "long")); import endcard

    endcard.bananas(out_path)                 # 8.0 s: yellow paper, halftone, profile picture, name, our banana
    endcard.fanpage(out_path)                 # assets/outro/fanpage_outro_16x9.mp4 exists -> the model's landscape
                                              # outro full frame (its length, its sound) + name + zones on the
                                              # side away from her face; otherwise the 8.0 s neon night card
    endcard.render(z, out_path)               # z = "bananas" | "fanpage" -> the same, returns the info dict

  Both return {"path", "dur", "zones": {"video": [(x0, y0, x1, y1), (..)], "subscribe": (cx, cy, r)}, "side"}.
  The file is ready for concat: yuv420p, timescale 90000, AAC 48 kHz stereo, -16 LUFS, true peak <= -1.5 dB.
  Options: music=<path>|False (bed under our sounds; default = first track of the brand, False = none),
  mstart=<s> (where in the track), venc=[...] (kompilace's encoder arguments), dur=8.0.
  Prebuilt copies: assets/long/end_bananas.mp4, assets/long/end_fanpage.mp4 (python tools/long/endcard.py).

  python tools/long/endcard.py [bananas|fanpage]   # -> assets/long/end_<brand>.mp4 + out/_ukazka/ preview + sheet

Where the YouTube elements go (Studio -> Editor -> End screen, template "2 videos, 1 subscribe"):
  bananas            videos (110,120)-(750,480) and (1170,120)-(1810,480), subscribe circle centre (960,300) r 125
  fanpage, no model  the same
  fanpage, model     one column (2 videos + the circle under them) on the side she never enters during the
                     whole outro, see the returned "zones"; the profile picture + name pop up on the other
                     side once she has walked away from it

Everything is drawn here (Pillow + numpy, fonts from fonts/), sounds are synthesized. The film is a pure
function of time: frame(t) draws the whole picture, motion = closed-form springs, no randomness at run time.
"""
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parents[2]
W, H, FPS, SR = 1920, 1080, 30, 48000
S = 2                                          # supersampling of sprites
BANANA_KEY = "0x1BD02A"                        # the greenscreen of assets/banan (same as klipuj.BANANA_KEY)
OUTRO_16x9 = HERE / "assets" / "outro" / "fanpage_outro_16x9.mp4"
OUTRO_SKIP = 1.0                               # s cut from the start of the model's outro (Aleš 17. 11.)
LUFS = -16.0

BRANDS = {
    "bananas": {"name": "gta6bananaclips", "pfp": "assets/brand/pfp_bananas.webp", "font": "fonts/cap/montserrat-900.ttf",
                "music": "assets/hudba/The_Heavy_Toll.mp3", "mstart": 24.0},
    "fanpage": {"name": "gta6fanpageclip", "pfp": "assets/brand/pfp_fanpage.webp", "font": "fonts/cap/anton.ttf",
                "music": "assets/hudba/fanpage/Asphalt_Swagger.mp3", "mstart": 20.0},
}
PINK, CYAN = (255, 79, 216), (64, 224, 230)
YELLOW, AMBER, INK, SLATE, SLATE_EDGE = (255, 225, 53), (246, 176, 28), (31, 27, 20), (46, 49, 59), (84, 89, 106)

# the standard zones (bananas and the fanpage card without the model)
ZONES = {"video": [(110, 120, 750, 480), (1170, 120, 1810, 480)], "subscribe": (960, 300, 125)}


# ---------------------------------------------------------------------------
# small tools

def spring(t, freq=3.2, damp=0.45):
    """
    0 -> 1, critically damped: fast first, then slow, and it never overshoots - nothing scales up and
    back down or wobbles (motion rule: direction never reverses, no bounces). `damp` is kept for the
    callers and ignored; `freq` still sets the speed.
    """
    if t <= 0:
        return 0.0
    w = 2 * math.pi * freq * 1.25
    return 1 - math.exp(-w * t) * (1 + w * t)


def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))


def ease(x):
    x = clamp(x)
    return 1 - (1 - x) ** 3


def font(path, px):
    return ImageFont.truetype(str(HERE / path), int(px * S))


def paste(canvas, im, x, y, alpha=1.0):
    """RGBA `im` onto RGBA canvas at (x, y), clipped to the canvas."""
    x, y = round(x), round(y)
    x0, y0, x1, y1 = max(0, x), max(0, y), min(canvas.width, x + im.width), min(canvas.height, y + im.height)
    if x1 <= x0 or y1 <= y0 or alpha <= 0.003:
        return
    if (x1 - x0, y1 - y0) != im.size:
        im = im.crop((x0 - x, y0 - y, x1 - x, y1 - y))
    if alpha < 0.997:
        im = im.copy()
        im.putalpha(im.getchannel("A").point(lambda v: int(v * alpha)))
    canvas.alpha_composite(im, (x0, y0))


_cache = {}


def put(canvas, sprite, cx, cy, scale=1.0, alpha=1.0, rot=0.0):
    """Sprite drawn at S x onto the 1x canvas, centre at (cx, cy)."""
    if alpha <= 0.003 or scale <= 0.01:
        return
    w, h = max(1, round(sprite.width * scale / S)), max(1, round(sprite.height * scale / S))
    key = (id(sprite), w, h)
    im = _cache.get(key)
    if im is None:
        im = sprite.resize((w, h), Image.LANCZOS)
        if abs(scale - 1) < 1e-6:                      # settled sprites are resized once
            _cache[key] = im
    if abs(rot) > 0.05:
        im = im.rotate(rot, resample=Image.BICUBIC, expand=True)
    paste(canvas, im, cx - im.width / 2, cy - im.height / 2, alpha)


def with_shadow(shape, blur, off, strength, margin):
    """RGBA sprite with a soft shadow under it; `margin` px of room on every side (all at S x)."""
    out = Image.new("RGBA", (shape.width + 2 * margin, shape.height + 2 * margin), (0, 0, 0, 0))
    sh = Image.new("L", out.size, 0)
    sh.paste(shape.getchannel("A").point(lambda v: int(v * strength)), (margin + off[0], margin + off[1]))
    out.putalpha(sh.filter(ImageFilter.GaussianBlur(blur)))
    out.alpha_composite(shape, (margin, margin))
    return out


def circle_pic(path, d, ring, ring_w, ring2=None, ring2_w=0):
    """Profile picture in a circle with one or two rings (drawn at S x)."""
    d *= S
    pic = Image.open(HERE / path).convert("RGB")
    s = min(pic.size)
    pic = pic.crop(((pic.width - s) // 2, (pic.height - s) // 2, (pic.width + s) // 2, (pic.height + s) // 2))
    im = Image.new("RGBA", (d, d), (0, 0, 0, 0))
    dr = ImageDraw.Draw(im)
    pad = 0
    if ring2:
        dr.ellipse((0, 0, d - 1, d - 1), fill=ring2 + (255,))
        pad = ring2_w * S
    dr.ellipse((pad, pad, d - 1 - pad, d - 1 - pad), fill=ring + (255,))
    pad += ring_w * S
    inner = d - 2 * pad
    mask = Image.new("L", (inner, inner), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, inner - 1, inner - 1), fill=255)
    im.paste(pic.resize((inner, inner), Image.LANCZOS), (pad, pad), mask)
    return im


def letters(text, f, col):
    """One sprite per letter + its x offset (px at 1x) so the word can be typed in."""
    asc, desc = f.getmetrics()
    out = []
    for i, ch in enumerate(text):
        x = f.getlength(text[:i])
        wch = max(1, math.ceil(f.getlength(ch)))
        im = Image.new("RGBA", (wch + 16, asc + desc + 16), (0, 0, 0, 0))
        ImageDraw.Draw(im).text((8, 8), ch, font=f, fill=col + (255,))
        out.append((im, (x + wch / 2) / S))
    return out, f.getlength(text) / S, (asc + desc) / S


def fit_px(path, text, px, width):
    while px > 20 and font(path, px).getlength(text) > width * S:
        px -= 2
    return px


def scribble(width, thick, col, wobble=5.0):
    """A hand-drawn underline (slightly wavy, round caps), drawn at S x."""
    hgt = int((thick + 2 * wobble + 8) * S)
    im = Image.new("RGBA", (int(width * S), hgt), (0, 0, 0, 0))
    pts = []
    for i in range(61):
        u = i / 60
        y = hgt / 2 + S * wobble * (0.6 * math.sin(u * 7.3 + 0.4) + 0.4 * math.sin(u * 17.1)) * (0.4 + 0.6 * u)
        pts.append((thick * S + u * (width - 2 * thick) * S, y))
    d = ImageDraw.Draw(im)
    d.line(pts, fill=col + (255,), width=int(thick * S), joint="curve")
    for x, y in (pts[0], pts[-1]):
        d.ellipse((x - thick * S / 2, y - thick * S / 2, x + thick * S / 2, y + thick * S / 2), fill=col + (255,))
    return im


def reveal(canvas, sprite, x, y, k):
    """Left part of a sprite (drawn at S x), k = 0..1 of its width; top-left at (x, y)."""
    if k <= 0.01:
        return
    key = (id(sprite), "full")
    im = _cache.get(key)
    if im is None:
        im = _cache[key] = sprite.resize((sprite.width // S, sprite.height // S), Image.LANCZOS)
    paste(canvas, im.crop((0, 0, max(1, round(im.width * clamp(k))), im.height)), x, y)


def sheen(canvas, mask, x, y, p, col=(255, 255, 255), strength=0.11):
    """One soft diagonal band of light crossing a zone (mask = L image of its shape at 1x), p = 0..1."""
    if not 0 < p < 1:
        return
    w, h = mask.size
    band = Image.new("L", (w, h), 0)
    bx = -0.5 * w + p * 2.0 * w
    ImageDraw.Draw(band).polygon([(bx, 0), (bx + 0.22 * w, 0), (bx + 0.22 * w - 0.45 * h, h), (bx - 0.45 * h, h)],
                                 fill=int(255 * strength))
    band = ImageChops.multiply(band.filter(ImageFilter.GaussianBlur(18)), mask)
    lay = Image.new("RGBA", (w, h), col + (0,))
    lay.putalpha(band)
    paste(canvas, lay, x, y)


def halftone_setup(pitch, angle):
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    u, v = xx * c + yy * s, -xx * s + yy * c
    return {"u": u, "dv": (v % pitch) - pitch / 2, "xx": xx, "yy": yy, "pitch": pitch}


def halftone(ht, t, field, drift=12.0):
    """Alpha (0..1) of a dot screen whose dot size follows `field` (0..1) and breathes slowly."""
    p = ht["pitch"]
    du = ((ht["u"] + t * drift) % p) - p / 2
    d = np.sqrt(du * du + ht["dv"] * ht["dv"])
    r = field * (0.80 + 0.20 * np.sin(ht["u"] / 170.0 - t * 0.9)) * p * 0.47
    return np.clip(r - d + 0.5, 0, 1)


# ---------------------------------------------------------------------------
# bananas: yellow paper, amber halftone, cut-out windows, sticker banana

def banana_sprite(px, fill, line, shadow=True):
    """A small paper banana (filled crescent with an outline), px wide at 1x; drawn at S x."""
    n = px * S * 2
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    pts = []
    for i in range(29):
        a = math.radians(205 - i * 185 / 28)
        pts.append((n * (0.5 + 0.44 * math.cos(a)), n * (0.24 + 0.50 * math.sin(a))))
    for i in range(29):
        a = math.radians(28 + i * 168 / 28)
        pts.append((n * (0.5 + 0.37 * math.cos(a)), n * (0.07 + 0.27 * math.sin(a))))
    d = ImageDraw.Draw(im)
    d.polygon(pts, fill=fill + (255,))
    d.line(pts + [pts[0]], fill=line + (255,), width=max(2, round(n * 0.045)), joint="curve")
    x, y = pts[0]
    d.line([(x + n * 0.01, y), (x - n * 0.04, y - n * 0.08)], fill=line + (255,), width=max(2, round(n * 0.07)))
    x, y = pts[28]
    d.ellipse((x - n * 0.03, y - n * 0.03, x + n * 0.03, y + n * 0.03), fill=line + (255,))
    im = im.resize((px * S, px * S), Image.LANCZOS)
    return with_shadow(im, 3 * S, (2 * S, 3 * S), 0.30, 8 * S) if shadow else im


def sticker(im, edge=7, col=(255, 255, 255)):
    """Die-cut sticker: a solid edge around the alpha shape (hides the key fringe) + a soft shadow."""
    m = edge + 22
    a = Image.new("L", (im.width + 2 * m, im.height + 2 * m), 0)
    a.paste(im.getchannel("A"), (m, m))
    grown = a.filter(ImageFilter.GaussianBlur(edge * 0.55)).point(lambda v: 255 if v > 28 else 0).filter(ImageFilter.GaussianBlur(1.1))
    out = Image.new("RGBA", a.size, (0, 0, 0, 0))
    out.putalpha(grown.point(lambda v: int(v * 0.34)).filter(ImageFilter.GaussianBlur(9)))
    sh = Image.new("RGBA", a.size, (0, 0, 0, 0))
    sh.alpha_composite(out, (0, 0))
    out = Image.new("RGBA", a.size, (0, 0, 0, 0))
    out.paste(sh, (6, 10), sh)
    edge_l = Image.new("RGBA", a.size, col + (0,))
    edge_l.putalpha(grown)
    out.alpha_composite(edge_l)
    out.alpha_composite(im, (m, m))
    return out, m


def mascot_frames(tmp, height=720):
    """Our banana (reakce2: clap -> points at the viewer -> smirk), keyed, 30 fps -> list of png paths."""
    src = HERE / "assets" / "banan" / "reakce2.mp4"
    if not src.exists():
        return []
    d = Path(tmp) / "mascot"
    d.mkdir(exist_ok=True)
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", "4.9", "-t", "5.05", "-i", str(src),
                    "-vf", f"fps={FPS},chromakey={BANANA_KEY}:0.16:0.06,scale=-2:{height}:flags=lanczos,format=rgba",
                    str(d / "%04d.png")], check=True)
    return sorted(d.glob("*.png"))


def setup_bananas(tmp):
    b = BRANDS["bananas"]
    A = {"brand": "bananas", "dur": 8.0, "zones": ZONES, "side": None}
    rnd = np.random.default_rng(7)
    # paper: brand yellow, grain, slow mottling, a few banana freckles, a little darker towards the edges
    paper = np.empty((H, W, 3), np.float32)
    paper[:] = YELLOW
    grain = rnd.normal(0, 4.5, (H, W)).astype(np.float32)
    small = Image.fromarray(rnd.normal(128, 60, (H // 24, W // 24)).clip(0, 255).astype(np.uint8))
    mott = np.asarray(small.resize((W, H), Image.BICUBIC).filter(ImageFilter.GaussianBlur(20)), np.float32) - 128
    ht = halftone_setup(26, 28)
    xx, yy = ht["xx"] / W, ht["yy"] / H
    vig = 1.03 - 0.16 * ((xx - 0.5) ** 2 + (yy - 0.5) ** 2) * 2
    paper = (paper + (grain + mott * 0.06)[..., None] * np.array([1.0, 0.95, 0.55], np.float32)) * vig[..., None]
    fr = Image.new("L", (W, H), 0)
    dfr = ImageDraw.Draw(fr)
    for _ in range(110):
        x, y, r = rnd.uniform(0, W), rnd.uniform(0, H), rnd.uniform(1.5, 4.5)
        dfr.ellipse((x - r, y - r, x + r, y + r), fill=int(rnd.uniform(70, 150)))
    fr = np.asarray(fr.filter(ImageFilter.GaussianBlur(0.8)), np.float32)[..., None] / 255
    A["paper"] = paper * (1 - fr) + np.array((150, 96, 30), np.float32) * fr
    A["ht"] = ht
    # dots live in the bottom-left and top-right corners, the middle stays calm
    A["field"] = np.clip(np.abs(xx - yy) * 1.5 - 0.42, 0, 1) ** 0.8
    # zones: dark cut-out windows with a thin lighter edge and a soft shadow
    A["boxes"] = []
    for x0, y0, x1, y1 in ZONES["video"]:
        im = Image.new("RGBA", ((x1 - x0) * S, (y1 - y0) * S), (0, 0, 0, 0))
        ImageDraw.Draw(im).rounded_rectangle((0, 0, im.width - 1, im.height - 1), 26 * S, fill=SLATE + (255,),
                                             outline=SLATE_EDGE + (255,), width=3 * S)
        m = Image.new("L", (x1 - x0, y1 - y0), 0)
        ImageDraw.Draw(m).rounded_rectangle((0, 0, m.width - 1, m.height - 1), 26, fill=255)
        A["boxes"].append((with_shadow(im, 16 * S, (0, 12 * S), 0.42, 50 * S), m, (x0, y0, x1, y1)))
    cx, cy, r = ZONES["subscribe"]
    im = Image.new("RGBA", (2 * r * S, 2 * r * S), (0, 0, 0, 0))
    ImageDraw.Draw(im).ellipse((0, 0, im.width - 1, im.height - 1), fill=SLATE + (255,), outline=SLATE_EDGE + (255,), width=3 * S)
    m = Image.new("L", (2 * r, 2 * r), 0)
    ImageDraw.Draw(m).ellipse((0, 0, 2 * r - 1, 2 * r - 1), fill=255)
    A["circle"] = (with_shadow(im, 16 * S, (0, 12 * S), 0.42, 50 * S), m)
    # lockup: profile picture as a sticker + the name typed in ink + a hand-drawn underline
    pfp = circle_pic(b["pfp"], 216, (255, 255, 255), 9, INK, 5)
    A["pfp"] = with_shadow(pfp, 12 * S, (0, 10 * S), 0.40, 40 * S)
    A["pfp_c"] = (800, 800)
    x0 = 940
    px = fit_px(b["font"], b["name"], 92, 1810 - x0)
    A["letters"], A["name_w"], A["name_h"] = letters(b["name"], font(b["font"], px), INK)
    A["name_x"], A["name_cy"] = x0, 780
    A["line"] = scribble(A["name_w"] + 10, 9, INK)
    # confetti: paper bananas of three sizes; slow fall behind everything + one burst from the picture
    A["ban"] = [banana_sprite(s, (255, 244, 168), (96, 60, 20)) for s in (50, 66, 86)]
    A["fall"] = [(rnd.uniform(0, W), rnd.uniform(0, H + 200), rnd.uniform(46, 96), rnd.uniform(0, 6.28),
                  rnd.uniform(-70, 70), int(rnd.integers(0, 3)), rnd.uniform(0.4, 1.1)) for _ in range(13)]
    A["burst"] = [(rnd.uniform(-1, 1) * 760, -rnd.uniform(420, 1150), rnd.uniform(-420, 420), int(rnd.integers(0, 3)),
                   rnd.uniform(0, 360)) for _ in range(16)]
    A["mascot"] = mascot_frames(tmp)
    return A


T_BOX, T_PFP, T_NAME, T_MAS, T_LINE = (0.10, 0.24, 0.38), 0.55, 0.95, 1.25, 1.75
TYPE_DT = 0.045
SHEENS = ((3.1, 0), (3.35, 2), (3.6, 1), (6.2, 0), (6.45, 2), (6.7, 1))     # (start, zone: 0 left, 2 circle, 1 right)


def backdrop_bananas(A, t):
    a = halftone(A["ht"], t, A["field"])[..., None] * 0.9
    arr = A["paper"] + (np.array(AMBER, np.float32) - A["paper"]) * a
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).convert("RGBA")


def draw_zones(im, A, t, col=(255, 255, 255), strength=0.11):
    zones = A["boxes"] + [A["circle"]]
    cx, cy, r = A["zones"]["subscribe"]
    for i, z in enumerate(zones):
        t0 = T_BOX[i]
        k = spring(t - t0, 2.6, 0.5)
        a = clamp((t - t0) / 0.12)
        if i < 2:
            x0, y0, x1, y1 = z[2]
            put(im, z[0], (x0 + x1) / 2, (y0 + y1) / 2 + (1 - k) * 40, 0.86 + 0.14 * k, a)
        else:
            put(im, z[0], cx, cy + (1 - k) * 40, 0.80 + 0.20 * k, a)
    for t0, i in SHEENS:
        p = (t - t0) / 0.75
        if 0 < p < 1 and t0 < A["dur"] - 0.9:
            if i < 2:
                x0, y0, _, _ = A["boxes"][i][2]
                sheen(im, A["boxes"][i][1], x0, y0, p, col, strength)
            else:
                sheen(im, A["circle"][1], cx - r, cy - r, p, col, strength)


def draw_name(im, A, t, t_name=T_NAME, lift=18):
    """The name as ONE word: it fades up from blurred to sharp with a small rise (no typing, no scale pop)."""
    u = (t - t_name) / 0.5
    if u <= 0:
        return
    if "name_spr" not in A:                                    # the letters joined into one sprite (at S x)
        spr = Image.new("RGBA", (int((A["name_w"] + 40) * S), int((A["name_h"] + 40) * S)), (0, 0, 0, 0))
        for ls, dx in A["letters"]:
            spr.alpha_composite(ls, (int(20 * S + dx * S - ls.width / 2), int(20 * S + A["name_h"] * S / 2 - ls.height / 2)))
        A["name_spr"] = spr
    e = clamp(u) ** 2 * (3 - 2 * clamp(u))
    spr = A["name_spr"]
    if e < 1:
        spr = spr.filter(ImageFilter.GaussianBlur(9 * S * (1 - e)))
    put(im, spr, A["name_x"] + A["name_w"] / 2, A["name_cy"] + (1 - e) * lift, 1.0, e)


def frame_bananas(A, t):
    im = backdrop_bananas(A, t)
    # slow paper bananas (behind everything)
    for x, y, v, ph, rv, si, sway in A["fall"]:
        yy = (y + v * t) % (H + 200) - 100
        put(im, A["ban"][si], x + 26 * sway * math.sin(ph + t * 0.9), yy, 1.0, 0.85, math.degrees(ph) + rv * t)
    # the burst when the profile picture lands
    u = t - T_PFP - 0.10
    if 0 < u < 2.2:
        px, py = A["pfp_c"]
        for vx, vy, rv, si, r0 in A["burst"]:
            put(im, A["ban"][si], px + vx * u, py + vy * u + 900 * u * u, 1.0, clamp(u / 0.05) * clamp((2.0 - u) / 0.5), r0 + rv * u)
    draw_zones(im, A, t)
    # our banana rises from below, claps, points at the viewer
    if A["mascot"] and t >= T_MAS:
        n = len(A["mascot"])
        i = round((t - T_MAS) * FPS)
        if i >= n:                                            # past the end: breathe back and forth in the last second
            j = (i - n) % (2 * FPS)
            i = n - 1 - (j if j < FPS else 2 * FPS - j)
        key = ("mascot", i)
        spr = _cache.get(key)
        if spr is None:
            _cache.clear() if len(_cache) > 60 else None
            spr = _cache[key] = sticker(Image.open(A["mascot"][max(0, min(n - 1, i))]).convert("RGBA"))[0]
        k = spring(t - T_MAS, 1.9, 0.52)
        paste(im, spr, 400 - spr.width / 2, 440 + (1 - k) * 700)
    # profile picture, name, underline
    put(im, A["pfp"], A["pfp_c"][0], A["pfp_c"][1], spring(t - T_PFP, 2.8, 0.42), clamp((t - T_PFP) / 0.1),
        rot=-7 * (1 - spring(t - T_PFP, 2.2, 0.5)))
    draw_name(im, A, t)
    reveal(im, A["line"], A["name_x"] - 5, A["name_cy"] + A["name_h"] * 0.36, ease((t - T_LINE) / 0.35))
    return im


# ---------------------------------------------------------------------------
# fanpage: the model's landscape outro full frame (when it exists) or the neon night card

def fan_zones(side, free=None, top=None):
    """
    One column of zones on the side away from her: the name on top, two 480x270 videos stacked, the
    subscribe circle under them. `free` = (x0, x1) px she never enters during the WHOLE outro; the column
    hugs the outer edge and shrinks (16:9 kept) when the free strip is narrower.
    """
    x0, x1 = free or ((1000, W) if side == "right" else (0, 920))
    w = int(max(240, min(480, x1 - x0 - 100)))                # never wider than the strip she leaves free
    h = round(w * 9 / 16)
    bx = (W - 60 - w) if side == "right" else 60
    r = 96
    y = top if top is not None else (H - (2 * h + 28 + 22 + 2 * r)) // 2
    return {"video": [(bx, y, bx + w, y + h), (bx, y + h + 28, bx + w, y + 2 * h + 28)],
            "subscribe": (bx + w // 2, y + 2 * h + 28 + 22 + r, r)}


def person_extent(src, samples=24, skip=0.0):
    """
    Where she is over the WHOLE outro (she walks and the camera pushes in, one frame says nothing):
    (side for the zones, (x0, x1) px of the strip she never enters, track [(t, x0, x1)] of her body in px
    over time, t counted from `skip`). Face (Haar) on `samples` frames, the body taken as 1.45 face widths
    to each side of it (shoulders in a close-up); the union of all frames is hers. Default: right.
    """
    track = []
    try:
        import cv2
        cap = cv2.VideoCapture(str(src))
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        first = int(skip * fps)
        det = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        for i in range(samples):
            f = first + int((n - first) * (i + 0.5) / samples)
            cap.set(cv2.CAP_PROP_POS_FRAMES, f)
            ok, fr = cap.read()
            if not ok:
                continue
            g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
            faces = det.detectMultiScale(g, 1.1, 6, minSize=(g.shape[0] // 14, g.shape[0] // 14))
            if len(faces):
                x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
                cx, k = x + w / 2, W / g.shape[1]
                track.append((f / fps - skip, max(0.0, (cx - 1.45 * w) * k), min(float(W), (cx + 1.45 * w) * k)))
        cap.release()
        if track:
            lo, hi = min(t[1] for t in track), max(t[2] for t in track)
            if W - hi >= lo:                                   # more room on the right
                return "right", (int(hi) + 30, W), track
            return "left", (0, int(lo) - 30), track
    except Exception as e:                                    # no cv2 / unreadable file: the default
        print(f"endcard: person detection skipped ({e})", file=sys.stderr)
    return "right", None, track


def free_spot(side, track, dur, need=300):
    """
    The area she LEAVES: she starts on one side and walks to the middle, so the side opposite the zones is
    empty from some moment on. -> (t, x0, x1): from t s the strip x0..x1 px stays clear of her until the
    end, or None (never wide enough, or less than 3 s of the outro left).
    """
    for i, (t, lo, hi) in enumerate(track):
        if side == "right":                                    # zones right -> the spot is on the left
            x0, x1 = 40, min(tr[1] for tr in track[i:]) - 30
        else:
            x0, x1 = max(tr[2] for tr in track[i:]) + 30, W - 40
        if x1 - x0 >= need and dur - t >= 3.0:
            return t + 0.3, int(x0), int(x1)
    return None


def neon_frame(w, h, r, circle=False, fill_a=110):
    """Zone frame for the night look: dim glass + a thin pink line with a restrained glow (S x, with margin)."""
    m = 26 * S
    shape = Image.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(shape)
    box = (0, 0, w * S - 1, h * S - 1)
    if circle:
        d.ellipse(box, fill=(12, 6, 22, fill_a))
    else:
        d.rounded_rectangle(box, r * S, fill=(12, 6, 22, fill_a))
    line = Image.new("RGBA", shape.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(line)
    if circle:
        d.ellipse(box, outline=PINK + (255,), width=3 * S)
    else:
        d.rounded_rectangle(box, r * S, outline=PINK + (255,), width=3 * S)
    out = Image.new("RGBA", (w * S + 2 * m, h * S + 2 * m), (0, 0, 0, 0))
    glow = Image.new("RGBA", out.size, (0, 0, 0, 0))
    glow.paste(line, (m, m))
    glow = glow.filter(ImageFilter.GaussianBlur(7 * S))
    glow.putalpha(glow.getchannel("A").point(lambda v: int(min(255, v * 1.1))))
    out.alpha_composite(glow)
    out.alpha_composite(shape, (m, m))
    out.alpha_composite(line, (m, m))
    mask = Image.new("L", (w, h), 0)
    if circle:
        ImageDraw.Draw(mask).ellipse((0, 0, w - 1, h - 1), fill=255)
    else:
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), r, fill=255)
    return out, mask


def palm(d, x, base, height, lean, col):
    """A palm silhouette: a leaning trunk and drooping fronds."""
    pts = [(x + lean * (i / 20) ** 1.7, base - height * i / 20) for i in range(21)]
    for i in range(20):
        d.line([pts[i], pts[i + 1]], fill=col, width=max(3, round(height * 0.035 * (1.25 - i / 26))))
    tx, ty = pts[-1]
    for k in range(9):
        a = math.radians(-168 + k * 39 + (9 if k % 2 else 0))
        ln = height * (0.44 + 0.08 * math.sin(k * 2.1))
        prev = (tx, ty)
        for j in range(1, 13):
            u = j / 12
            p = (tx + math.cos(a) * ln * u, ty + math.sin(a) * ln * u * 0.62 + ln * 0.58 * u * u)
            d.line([prev, p], fill=col, width=max(2, round(height * 0.052 * (1 - u) + 2)))
            nx, ny = -(p[1] - prev[1]), p[0] - prev[0]
            nl = math.hypot(nx, ny) or 1
            for sgn in (-1, 1):                                # leaflets
                q = (p[0] + sgn * nx / nl * ln * 0.13 * (1 - u * 0.6) + math.cos(a) * 6, p[1] + sgn * ny / nl * ln * 0.13 * (1 - u * 0.6) + ln * 0.05)
                d.line([p, q], fill=col, width=max(1, round(height * 0.012)))
            prev = p


def setup_fanpage(tmp, src=None):
    b = BRANDS["fanpage"]
    src = Path(src) if src else OUTRO_16x9
    A = {"brand": "fanpage", "video": src if src.exists() else None}
    rnd = np.random.default_rng(19)
    if A["video"]:
        A["dur"] = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                         str(src)], capture_output=True, text=True).stdout.strip())
        A["dur"] -= OUTRO_SKIP
        A["side"], A["free"], track = person_extent(src, skip=OUTRO_SKIP)
        A["spot"] = free_spot(A["side"], track, A["dur"])     # where the profile picture + name pop up
        A["zones"] = fan_zones(A["side"], A["free"], None if A["spot"] else 262)
        # light scrim only on the zones' side so the name and the frames read on any picture
        xx = np.mgrid[0:H, 0:W][1].astype(np.float32) / W
        g = np.clip((xx - 0.40) / 0.45, 0, 1) if A["side"] == "right" else np.clip((0.60 - xx) / 0.45, 0, 1)
        scrim = Image.new("RGBA", (W, H), (8, 3, 16, 0))
        scrim.putalpha(Image.fromarray((g ** 1.4 * 150).astype(np.uint8)))
        A["scrim"] = scrim
    else:
        A["dur"], A["side"], A["zones"] = 8.0, None, ZONES
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        HOR = 800.0                                           # horizon
        A["hor"] = HOR
        u = np.clip(yy / HOR, 0, 1)[..., None]
        top, mid, low = np.array((9, 5, 24), np.float32), np.array((44, 14, 66), np.float32), np.array((150, 36, 120), np.float32)
        sky = np.where(u < 0.6, top + (mid - top) * (u / 0.6), mid + (low - mid) * np.clip((u - 0.6) / 0.4, 0, 1) ** 1.6)
        water = np.array((16, 7, 30), np.float32) + np.array((34, 8, 40), np.float32) * np.clip(1 - (yy - HOR) / 280, 0, 1)[..., None] ** 2
        bg = np.where((yy < HOR)[..., None], sky, water)
        bg += rnd.normal(0, 3.0, (H, W, 1)).astype(np.float32)
        A["bg"] = bg
        A["ht"] = halftone_setup(22, 24)
        A["field"] = np.clip(1.25 - np.hypot(xx / 900, (yy - 1080) / 420), 0, 1) ** 1.3 * (yy > 806)
        A["xx"], A["yy"] = xx, yy
        A["stars"] = [(rnd.uniform(0, W), rnd.uniform(0, HOR - 190), rnd.uniform(0.8, 2.0), rnd.uniform(0, 6.28),
                       rnd.uniform(0.6, 2.2)) for _ in range(90)]
        # skyline + palms: one dark silhouette layer in front of the sun
        sil = Image.new("RGBA", (W * S, H * S), (0, 0, 0, 0))
        d = ImageDraw.Draw(sil)
        dark = (10, 4, 20, 255)
        x = 0
        wins = []
        while x < W:
            bw, bh = rnd.uniform(34, 110), rnd.uniform(24, 150) * (1.5 if 1150 < x < 1600 else 1.0)
            d.rectangle((x * S, (HOR - bh) * S, (x + bw) * S, (HOR + 2) * S), fill=dark)
            for _ in range(int(bw * bh / 900)):
                wins.append((x + rnd.uniform(5, bw - 5), HOR - rnd.uniform(6, bh - 6), rnd.uniform(0, 6.28), int(rnd.integers(0, 3))))
            x += bw + rnd.uniform(-4, 22)
        for px, hgt, lean in ((120, 400, 70), (300, 300, -50), (1760, 430, -80), (1600, 290, 46)):
            palm(d, px * S, (HOR + 4) * S, hgt * S, lean * S, dark)
        A["sil"] = sil.resize((W, H), Image.LANCZOS)
        A["wins"] = wins
        A["sun_c"], A["sun_r"] = (700, HOR - 6), 235
        r = A["sun_r"]
        glow = Image.new("L", (6 * r, 6 * r), 0)
        ImageDraw.Draw(glow).ellipse((2 * r, 2 * r, 4 * r, 4 * r), fill=120)
        gl = Image.new("RGBA", glow.size, (255, 70, 190, 0))
        gl.putalpha(glow.filter(ImageFilter.GaussianBlur(r * 0.5)))
        A["sun_glow"] = gl
    A["boxes"] = []
    for x0, y0, x1, y1 in A["zones"]["video"]:
        spr, m = neon_frame(x1 - x0, y1 - y0, 22, fill_a=120 if A["video"] else 150)
        A["boxes"].append((spr, m, (x0, y0, x1, y1)))
    r = A["zones"]["subscribe"][2]
    A["circle"] = neon_frame(2 * r, 2 * r, r, circle=True, fill_a=120 if A["video"] else 150)
    # the name: white Anton, typed; a pink line draws under it
    if A["video"] and A["spot"]:                               # profile picture + name in the area she left
        t0, sx0, sx1 = A["spot"]
        d = int(min(210, sx1 - sx0 - 50))
        px = fit_px(b["font"], b["name"], 110, sx1 - sx0 - 10)
        x0 = (sx0 + sx1) / 2 - font(b["font"], px).getlength(b["name"]) / S / 2
        A["pfp"] = with_shadow(circle_pic(b["pfp"], d, PINK, 6, (255, 255, 255), 2), 12 * S, (0, 8 * S), 0.55, 34 * S)
        glow = Image.new("L", ((d + 120) * S, (d + 120) * S), 0)
        ImageDraw.Draw(glow).ellipse((48 * S, 48 * S, (d + 72) * S, (d + 72) * S), outline=255, width=14 * S)
        gl = Image.new("RGBA", glow.size, PINK + (0,))
        gl.putalpha(glow.filter(ImageFilter.GaussianBlur(12 * S)))
        A["pfp_glow"] = gl                                     # the neon ring's glow
        A["pfp_c"], A["pfp_t"] = ((sx0 + sx1) / 2, 420), t0
        A["name_cy"] = 420 + d / 2 + 62
    elif A["video"]:
        free = A["free"] or ((1000, W) if A["side"] == "right" else (0, 920))
        px = fit_px(b["font"], b["name"], 150, min(760, free[1] - free[0] - 70))
        x0 = 100 if A["side"] == "left" else 1820 - font(b["font"], px).getlength(b["name"]) / S
        A["name_cy"] = 170
    else:
        x0, px = 1120, fit_px(b["font"], b["name"], 140, 700)
        A["name_cy"] = 940
    A["letters"], A["name_w"], A["name_h"] = letters(b["name"], font(b["font"], px), (255, 255, 255))
    A["name_x"] = x0
    sh = Image.new("RGBA", (int(A["name_w"] + 80) * S, int(A["name_h"] + 80) * S), (0, 0, 0, 0))
    ImageDraw.Draw(sh).text((40 * S, 40 * S + 8), b["name"], font=font(b["font"], px), fill=(0, 0, 0, 170))
    A["name_sh"] = sh.filter(ImageFilter.GaussianBlur(9 * S))
    ln = Image.new("RGBA", (int(A["name_w"]) * S, 26 * S), (0, 0, 0, 0))
    ImageDraw.Draw(ln).rounded_rectangle((0, 9 * S, ln.width - 1, 16 * S), 4 * S, fill=PINK + (255,))
    glow = ln.filter(ImageFilter.GaussianBlur(5 * S))
    glow.alpha_composite(ln)
    A["line"] = glow
    if not A["video"]:
        A["pfp"] = with_shadow(circle_pic(b["pfp"], 150, PINK, 5), 10 * S, (0, 8 * S), 0.5, 30 * S)
    return A


def backdrop_fanpage(A, t):
    HOR = A["hor"]
    arr = A["bg"].copy()
    a = halftone(A["ht"], t, A["field"], drift=9.0)[..., None] * 0.34
    arr += (np.array(PINK, np.float32) - arr) * a
    im = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).convert("RGBA")
    d = ImageDraw.Draw(im)
    for x, y, r, ph, sp in A["stars"]:
        k = 0.5 + 0.5 * math.sin(ph + t * sp)
        d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 235, 250, int(60 + 150 * k)))
    # the striped sun: rises a little, its cut-out stripes slide down
    cx, cy = A["sun_c"]
    r = A["sun_r"]
    rise = (1 - ease(t / 1.6)) * 90 - 10 * math.sin(t * 0.25)
    paste(im, A["sun_glow"], cx - 3 * r, cy - 3 * r + rise - r * 0.25, 0.85)
    yy, xx = np.mgrid[0:2 * r, 0:2 * r].astype(np.float32)
    dist = np.hypot(xx - r, yy - r)
    v = (yy / (2 * r))[..., None]
    col = np.array((255, 214, 102), np.float32) * (1 - v) + np.array(PINK, np.float32) * v
    ph = (yy + t * 16) % 44
    cut = (ph < 5 + 20 * np.clip((yy - 0.42 * 2 * r) / (1.2 * r), 0, 1)) & (yy > 0.42 * 2 * r)
    alpha = np.clip(r - dist + 0.5, 0, 1) * (~cut)
    sun = Image.fromarray(np.dstack([col, alpha[..., None] * 255]).clip(0, 255).astype(np.uint8), "RGBA")
    top = cy - r * 1.25 + rise
    vis = int(max(0, min(2 * r, HOR - top)))                  # only the part above the horizon
    paste(im, sun.crop((0, 0, 2 * r, vis)), cx - r, top)
    im.alpha_composite(A["sil"])
    d = ImageDraw.Draw(im)
    for x, y, ph, c in A["wins"]:                             # a few windows, slowly blinking
        k = 0.5 + 0.5 * math.sin(ph + t * 0.7)
        if k > 0.35:
            d.rectangle((x, y, x + 3, y + 4), fill=((255, 190, 120), PINK, CYAN)[c] + (int(230 * k),))
    for i in range(26):                                       # reflection: short streaks under the sun, drifting
        y = HOR + 10 + i * 10.5
        wd = (r * 0.95) * (1 - i / 34) * (0.55 + 0.45 * math.sin(i * 1.9 + t * 1.3))
        ox = 22 * math.sin(i * 0.8 + t * 0.9)
        d.rounded_rectangle((cx - wd + ox, y, cx + wd + ox, y + 3), 2, fill=(255, 120, 200, int(150 * (1 - i / 30))))
    return im


def frame_fanpage(A, t, base=None):
    if A["video"]:
        im = base.convert("RGBA")
        im.alpha_composite(A["scrim"]) if t > 0.3 else paste(im, A["scrim"], 0, 0, t / 0.3)
        tn = 0.55
        if A.get("spot"):                                      # the profile picture pops where she was
            tp = A["pfp_t"]
            tn = tp + 0.22
            if t > tp:
                k = spring(t - tp, 2.8, 0.42)
                put(im, A["pfp_glow"], A["pfp_c"][0], A["pfp_c"][1], k,
                    clamp((t - tp) / 0.25) * (0.75 + 0.25 * math.sin(2.2 * (t - tp))))
                put(im, A["pfp"], A["pfp_c"][0], A["pfp_c"][1], k, clamp((t - tp) / 0.1), rot=-6 * (1 - spring(t - tp, 2.2, 0.5)))
    else:
        im = backdrop_fanpage(A, t)
        tn = T_NAME
    draw_zones(im, A, t, PINK, 0.16)
    put(im, A["name_sh"], A["name_x"] + A["name_w"] / 2, A["name_cy"] + 4, 1.0, ease((t - tn) / 0.8) * 0.9)
    draw_name(im, A, t, tn, lift=26)
    reveal(im, A["line"], A["name_x"], A["name_cy"] + A["name_h"] * 0.40, ease((t - tn - 0.75) / 0.4))
    if not A["video"]:
        k = spring(t - T_PFP, 2.8, 0.42)
        put(im, A["pfp"], A["name_x"] - 105, A["name_cy"], k, clamp((t - T_PFP) / 0.1))
    return im


# ---------------------------------------------------------------------------
# sound: ours, synthesized

def _add(buf, sig, at, vol=1.0):
    i = int(at * SR)
    if i < buf.size:
        buf[i: i + sig.size] += vol * sig[: buf.size - i]


def snd_tup(f0=420, f1=190):
    """The soft wooden 'tup' of the banana score card."""
    rnd = np.random.default_rng(3)
    t = np.arange(int(SR * 0.2)) / SR
    f = f1 + (f0 - f1) * np.exp(-t / 0.02)
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.05) + \
        0.25 * np.convolve(rnd.normal(0, 1, t.size), np.ones(5) / 5, mode="same") * np.exp(-t / 0.006)


def snd_pop():
    t = np.arange(int(SR * 0.18)) / SR
    f = 500 + 560 * np.minimum(1, t / 0.05)
    return (np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.04) + 0.25 * np.sin(2 * np.pi * 1568 * t) * np.exp(-t / 0.06)) * np.minimum(1, t * 600)


def snd_tick(f=2100):
    t = np.arange(int(SR * 0.03)) / SR
    return np.sin(2 * np.pi * f * t) * np.exp(-t / 0.004) + 0.5 * np.sin(2 * np.pi * 330 * t) * np.exp(-t / 0.008)


def snd_whoosh(dur=0.5):
    rnd = np.random.default_rng(9)
    n = int(SR * dur)
    ramp = np.linspace(0, 1, n)
    noise = rnd.normal(0, 1, n)
    out = ((1 - ramp) * np.convolve(noise, np.ones(50) / 50, mode="same") + ramp * np.convolve(noise, np.ones(9) / 9, mode="same"))
    return out * np.sin(np.pi * ramp) ** 2 / 0.25


def sfx_track(A):
    buf = np.zeros(int(SR * A["dur"]))
    if A["brand"] == "bananas":
        for i, t0 in enumerate(T_BOX):
            _add(buf, snd_tup(400 - 30 * i, 180), t0 + 0.07, 0.42)
        _add(buf, snd_pop(), T_PFP + 0.05, 0.6)
        _add(buf, snd_tup(520, 300), T_NAME + 0.05, 0.22)      # the name: one soft tonal knock (no typing sounds)
        _add(buf, snd_whoosh(0.45), T_MAS, 0.20)
        _add(buf, snd_tup(300, 150), T_MAS + 0.30, 0.38)
        _add(buf, snd_whoosh(0.3), T_LINE, 0.10)
    else:
        tn = (A["pfp_t"] + 0.22 if A.get("spot") else 0.55) if A["video"] else T_NAME
        vol = 0.5 if A["video"] else 1.0                      # under her voice: quieter
        if A["video"] and A.get("spot"):
            _add(buf, snd_pop(), A["pfp_t"] + 0.05, 0.4 * vol)
        for i, t0 in enumerate(T_BOX):
            _add(buf, snd_whoosh(0.28), t0, 0.10 * vol)
        _add(buf, snd_tup(480, 280), tn + 0.05, 0.20 * vol)     # the name: one soft tonal knock (no typing sounds)
        _add(buf, snd_whoosh(0.4), tn + 0.75, 0.14 * vol)
        if not A["video"]:
            _add(buf, snd_pop(), T_PFP + 0.05, 0.4)
    return np.convolve(buf, np.ones(3) / 3, mode="same")


# ---------------------------------------------------------------------------
# render

VENC = ["-c:v", "libx264", "-preset", "medium", "-crf", "17"]


def _video_frames(src, dur):
    """The outro as PIL frames (scaled to cover 1920x1080, 30 fps), from OUTRO_SKIP s of the file."""
    p = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{OUTRO_SKIP}", "-i", str(src), "-vf",
                          f"fps={FPS},scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H}",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"], stdout=subprocess.PIPE)
    n = W * H * 3
    last = None
    for _ in range(round(dur * FPS)):
        raw = p.stdout.read(n)
        if len(raw) == n:
            last = Image.frombuffer("RGB", (W, H), raw).copy()
        yield last
    p.stdout.close()
    p.wait()


def frames(A):
    """Generator of (t, PIL frame) of the whole card."""
    n = round(A["dur"] * FPS)
    if A["brand"] == "bananas":
        for i in range(n):
            yield i / FPS, frame_bananas(A, i / FPS)
    else:
        src = _video_frames(A["video"], A["dur"]) if A["video"] else None
        for i in range(n):
            yield i / FPS, frame_fanpage(A, i / FPS, next(src) if src else None)


def render(brand, out, music=None, mstart=None, venc=None, src=None, sheet=None):
    """Build the end card of `brand` -> info dict (path, dur, zones, side). See the top of the file."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    b = BRANDS[brand]
    _cache.clear()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        A = setup_bananas(tmp) if brand == "bananas" else setup_fanpage(tmp, src)
        dur = A["dur"]
        # --- audio: our sounds (+ her voice) (+ a quiet bed of our music), then one gain to -16 LUFS
        wav = tmp / "sfx.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes((np.clip(sfx_track(A), -1, 1) * 32767).astype(np.int16).tobytes())
        ins, parts, n = ["-i", str(wav)], ["[0:a]aresample=48000,aformat=channel_layouts=stereo[s0]"], 1
        has_voice = False
        if A.get("video"):
            probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                                    "-of", "csv=p=0", str(A["video"])], capture_output=True, text=True).stdout.strip()
            if probe:
                has_voice = True
                ins += ["-ss", f"{OUTRO_SKIP}", "-i", str(A["video"])]
                parts.append(f"[{n}:a]aresample=48000,aformat=channel_layouts=stereo[s{n}]")
                n += 1
        if music is None:
            music = None if has_voice else HERE / b["music"]   # the outro brings its own sound
        if music and Path(music).exists():
            ins += ["-ss", f"{b['mstart'] if mstart is None else mstart:.2f}", "-i", str(music)]
            parts.append(f"[{n}:a]atrim=0:{dur:.3f},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,"
                         f"volume=0.30,afade=t=in:d=0.6,afade=t=out:st={dur - 1.6:.2f}:d=1.6[s{n}]")
            n += 1
        mix = tmp / "mix.wav"
        graph = ";".join(parts) + ";" + "".join(f"[s{i}]" for i in range(n)) + \
            f"amix=inputs={n}:duration=longest:normalize=0,apad=whole_dur={dur:.3f},atrim=0:{dur:.3f}[a]"
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error"] + ins + ["-filter_complex", graph, "-map", "[a]",
                        "-c:a", "pcm_s16le", str(mix)], check=True)
        r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(mix), "-af", "loudnorm=print_format=json",
                            "-f", "null", "-"], capture_output=True, text=True)
        m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr)
        gain = clamp(LUFS - float(json.loads(m.group(0))["input_i"]), -30, 30) if m else 0.0
        # --- video through the pipe
        cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
               "-framerate", str(FPS), "-i", "pipe:0", "-i", str(mix),
               "-af", f"volume={gain:.2f}dB,alimiter=limit=0.82:level=0:attack=3:release=60,"
                      f"afade=t=out:st={dur - 0.5:.2f}:d=0.5",
               "-map", "0:v", "-map", "1:a"] + (venc or VENC) + \
              ["-r", str(FPS), "-pix_fmt", "yuv420p", "-video_track_timescale", "90000", "-c:a", "aac", "-b:a", "192k",
               "-ar", str(SR), "-ac", "2", "-t", f"{dur:.3f}", "-movflags", "+faststart", str(out)]
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        picks = {round(dur * FPS * k) for k in (0.04, 0.09, 0.15, 0.24, 0.42, 0.62, 0.82, 0.985)} if sheet else set()
        shots = []
        for i, (t, im) in enumerate(frames(A)):
            rgb = im.convert("RGB")
            p.stdin.write(rgb.tobytes())
            if i in picks:
                shots.append(rgb.resize((960, 540), Image.LANCZOS))
        p.stdin.close()
        if p.wait() != 0:
            raise RuntimeError(f"ffmpeg failed: {out}")
        if sheet and shots:
            sh = Image.new("RGB", (4 * 960, 2 * 540), (30, 30, 30))
            for i, s in enumerate(shots[:8]):
                sh.paste(s, ((i % 4) * 960, (i // 4) * 540))
            Path(sheet).parent.mkdir(parents=True, exist_ok=True)
            sh.save(sheet, quality=88)
    return {"path": out, "dur": dur, "zones": A["zones"], "side": A["side"], "model": bool(A.get("video"))}


def bananas(out, **kw):
    """The bananas end card (8.0 s) -> info dict."""
    return render("bananas", out, **kw)


def fanpage(out, **kw):
    """The fanpage end card: the model's 16:9 outro + light layer when it exists, else the neon night card."""
    return render("fanpage", out, **kw)


if __name__ == "__main__":
    for z in sys.argv[1:] or BRANDS:
        dst = HERE / "assets" / "long" / f"end_{z}.mp4"
        info = render(z, dst, sheet=HERE / "out" / "_ukazka" / f"end_{z}_sheet.jpg")
        shutil.copyfile(dst, HERE / "out" / "_ukazka" / f"end_{z}.mp4")
        print(f"{dst.name}: {info['dur']:.2f} s, {dst.stat().st_size / 1e6:.1f} MB, zones {info['zones']}, side {info['side']}")
