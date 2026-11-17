"""
tier.py - the tier list of the long-form video, in the look everyone knows: black board, rows
divided by thin dark gaps, a pastel label square on the left (S #FF7F7F, A #FFBF7F, B #FFDF7F,
C #FFFF7F, D #BFFF7F) with a dark letter, the moments as square thumbnails sitting in the row.
Everything is drawn here (Pillow + numpy, fonts from fonts/); nobody's image is used.

How kompilace.py calls it (instead of board_bg / board_graph / render_board):

    sys.path.insert(0, str(HERE / "tools" / "long")); import tier

    # once per video: how many moments each row will hold in the end, so thumbs keep one size
    cap = tier.capacity([m["tier"] for m in ranked])
    def entry(m): return {"tier": m["tier"], "thumb": str(tier_thumb(m, tmp)), "label": "@" + m["channel"]}

    tier.render_hold([], tmp / "board_empty.mp4", dur=2.0, capacity=cap, venc=venc)            # empty board
    tier.render_fly_in([entry(p) for p in placed], entry(m), tmp / f"tier{i:02d}.mp4",          # 1.5 s flight,
                       dur=TIER_ANIM, capacity=cap, venc=venc)                                  # lands at 0.55 s
    tier.render_hold([entry(p) for p in placed], tmp / "board_full.mp4", dur=3.0, capacity=cap, venc=venc)

  entries are in the order they were placed (a row fills left to right). "thumb" = PIL.Image or a path
  (any aspect, centre-cropped to a square). Segments are 1920x1080, 30 fps, yuv420p, timescale 90000,
  AAC 48 kHz stereo (= VOUT + AOUT of kompilace.py); the flight carries its own soft whoosh + landing
  'tok' (audio=False -> silence). A teaser caption can still be drawn over the segment with drawtext
  (the strip under the board, y 1030+, is free).

  python tools/long/tier.py        # preview -> out/_ukazka/tier_ukazka.mp4 + tier_ukazka_sheet.jpg

Pure functions: board() draws a state, fly_in_frames() is frame(t) with closed-form springs, no
randomness, no motion blur.
"""
import math
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parents[2]
FONT_LETTER = HERE / "fonts" / "montserrat-600.ttf"
FONT_LABEL = HERE / "fonts" / "montserrat-600.ttf"
SR = 48000

TIERS = ["S", "A", "B", "C", "D"]
TIER_RGB = {"S": (255, 127, 127), "A": (255, 191, 127), "B": (255, 223, 127), "C": (255, 255, 127), "D": (191, 255, 127)}
BG, ROW, INK = (0, 0, 0), (26, 26, 23), (24, 24, 24)
ACCENT = {"bananas": (255, 225, 53), "fanpage": (255, 79, 216)}
PER_ROW = 8                                   # up to 8 thumbs keep the full row height, more shrink
LAND_FR = 0.58                                # the thumbnail touches its slot at 0.58 of the segment (impact)
LAND = LAND_FR * 1.9


# ---------------------------------------------------------------------------
# layout

def geometry(size=(1920, 1080)):
    """Board rectangle and row metrics for a frame size (all in px)."""
    w, h = size
    if h > w:                                 # 9:16 board (experiment short): rows in the middle 56 %, clear of
        k = w / 1080                          # the app's top bar and caption area, 3 big thumbs per row
        mx, top, gap = round(48 * k), round(h * 0.22), max(2, round(4 * k))
        row_h = int((round(h * 0.56) - gap * (len(TIERS) - 1)) / len(TIERS))
        return {"w": w, "h": h, "k": k, "mx": mx, "top": top, "gap": gap, "row_h": row_h, "per_row": 3,
                "items_x": mx + row_h + gap, "items_w": w - mx - (mx + row_h + gap)}
    k = h / 1080
    mx, top, gap = round(70 * k), round(72 * k), max(2, round(4 * k))
    row_h = int((h - top - round(52 * k) - gap * (len(TIERS) - 1)) / len(TIERS))
    return {"w": w, "h": h, "k": k, "mx": mx, "top": top, "gap": gap, "row_h": row_h, "per_row": PER_ROW,
            "items_x": mx + row_h + gap, "items_w": w - mx - (mx + row_h + gap)}


def capacity(tiers):
    """['S', 'B', 'B', ...] of the whole video -> {'S': 1, 'B': 2, ...}: the final size of every row."""
    return {t: sum(1 for x in tiers if x == t) for t in TIERS}


def slots(entries, size=(1920, 1080), cap=None):
    """
    (x, y, w, h) of every entry, in order. `cap` (see capacity) fixes the thumb size of a row ahead.
    A short video (no row with more than 4 moments) gets wide 16:9 thumbnails - the board is not left
    empty and the game frame is readable; fuller boards use the classic squares.
    """
    g = geometry(size)
    seen, out = {t: 0 for t in TIERS}, []
    count = {t: sum(1 for e in entries if e["tier"] == t) for t in TIERS}
    fullest = max([count[t] for t in TIERS] + [(cap or {}).get(t, 0) for t in TIERS])
    wide = g["w"] > g["h"] and fullest <= 4
    for e in entries:
        t = e["tier"]
        if wide:
            h = g["row_h"]
            w = min(round(h * 16 / 9), g["items_w"] // 4)
        else:
            n = max(g["per_row"], count[t], (cap or {}).get(t, 0))
            w = h = min(g["row_h"], g["items_w"] // n)
        y = g["top"] + TIERS.index(t) * (g["row_h"] + g["gap"]) + (g["row_h"] - h) // 2
        out.append((g["items_x"] + seen[t] * w, y, w, h))
        seen[t] += 1
    return out


# ---------------------------------------------------------------------------
# drawing

def _font(path, px):
    return ImageFont.truetype(str(path), max(8, int(px)))


def thumb_sprite(entry, side, high=None):
    """Thumbnail side x high px (square by default): the frame centre-cropped, a hairline gap, the @label on a dark strip."""
    src = entry.get("thumb")
    if src is not None and not isinstance(src, Image.Image):
        src = Image.open(src)
    high = high or side
    im = Image.new("RGB", (side, high), BG)
    inner, inner_h = side - 2, high - 2                        # 1 px of board between neighbours
    if src is None:
        pic = Image.new("RGB", (inner, inner_h), (58, 58, 54))
    else:
        src = src.convert("RGB")
        k = min(src.width / inner, src.height / inner_h)
        cw, ch = round(inner * k), round(inner_h * k)
        box = ((src.width - cw) // 2, (src.height - ch) // 2, (src.width + cw) // 2, (src.height + ch) // 2)
        pic = src.crop(box).resize((inner, inner_h), Image.LANCZOS)
    label = (entry.get("label") or "").strip()
    if label:
        px = max(11, round(min(side, high) * 0.118))
        f = _font(FONT_LABEL, px)
        while px > 10 and f.getlength(label) > inner - 10:
            px -= 1
            f = _font(FONT_LABEL, px)
        while f.getlength(label) > inner - 10 and len(label) > 4:    # still too long: cut, keep it honest
            label = label[:-2] + "…"
        strip_h = round(px * 1.75)
        strip = Image.new("RGBA", (inner, strip_h), (0, 0, 0, 205))
        pic = pic.convert("RGBA")
        pic.alpha_composite(strip, (0, inner_h - strip_h))
        d = ImageDraw.Draw(pic)
        d.text((inner / 2, inner_h - strip_h / 2), label, font=f, fill=(255, 255, 255, 255), anchor="mm")
        pic = pic.convert("RGB")
    im.paste(pic, (1, 1))
    return im


def banana_outline(px, col, stroke=0.085):
    """Tiny banana, outline only (the brand's mark on the board). RGBA, px wide."""
    s = 4
    n = px * s
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    pts = []
    for i in range(25):                                        # outer belly of the crescent
        a = math.radians(205 - i * (205 - 20) / 24)
        pts.append((n * (0.5 + 0.44 * math.cos(a)), n * (0.30 - 0.50 * math.sin(a) * -1)))
    for i in range(25):                                        # inner curve back
        a = math.radians(28 + i * (196 - 28) / 24)
        pts.append((n * (0.5 + 0.40 * math.cos(a)), n * (0.16 + 0.34 * math.sin(a))))
    d = ImageDraw.Draw(im)
    d.line(pts + [pts[0]], fill=col + (255,), width=max(2, round(n * stroke)), joint="curve")
    x, y = pts[0]
    d.line([(x, y), (x - n * 0.05, y - n * 0.09)], fill=col + (255,), width=max(2, round(n * stroke * 1.3)))   # stem
    return im.resize((px, px), Image.LANCZOS)


def _base(size, brand):
    """The empty board: rows, label squares, letters, the brand's accent."""
    g = geometry(size)
    im = Image.new("RGB", size, BG)
    d = ImageDraw.Draw(im)
    f = _font(FONT_LETTER, g["row_h"] * 0.36)
    for i, t in enumerate(TIERS):
        y = g["top"] + i * (g["row_h"] + g["gap"])
        d.rectangle((g["mx"], y, g["w"] - g["mx"] - 1, y + g["row_h"] - 1), fill=ROW)
        d.rectangle((g["mx"], y, g["mx"] + g["row_h"] - 1, y + g["row_h"] - 1), fill=TIER_RGB[t])
        d.rectangle((g["mx"] + g["row_h"], y, g["mx"] + g["row_h"] + g["gap"] - 1, y + g["row_h"] - 1), fill=BG)
        d.text((g["mx"] + g["row_h"] / 2, y + g["row_h"] / 2), t, font=f, fill=INK, anchor="mm")
    acc = ACCENT.get(brand)
    if acc:                                                    # accent only: a hairline rule + a tiny banana
        y = g["top"] - round(14 * g["k"])
        d.rectangle((g["mx"], y, g["mx"] + round(96 * g["k"]), y + max(2, round(4 * g["k"])) - 1), fill=acc)
        if brand == "bananas":
            b = banana_outline(round(40 * g["k"]), acc)
            im.paste(b, (g["w"] - g["mx"] - b.width, g["top"] - b.height - round(10 * g["k"])), b)
    return im


def board(entries, size=(1920, 1080), brand="bananas", cap=None):
    """The board with `entries` ({tier, thumb, label}) sitting in their rows -> PIL.Image (RGB)."""
    im = _base(size, brand)
    for e, (x, y, w, h) in zip(entries, slots(entries, size, cap)):
        im.paste(thumb_sprite(e, w, h), (x, y))
    return im


# ---------------------------------------------------------------------------
# motion

def spring(t, freq=3.2, damp=0.45):
    """0 -> 1 with overshoot: step response of an underdamped spring, closed form."""
    if t <= 0:
        return 0.0
    w = 2 * math.pi * freq
    wd = w * math.sqrt(1 - damp * damp)
    return 1 - math.exp(-damp * w * t) * (math.cos(wd * t) + damp * w / wd * math.sin(wd * t))


def _flight_spring(dist, travel_s, overshoot_px):
    """Spring (freq, damp) that first reaches the target after travel_s and overshoots it by overshoot_px."""
    a = math.log(max(dist, overshoot_px * 1.5) / overshoot_px)
    damp = a / math.sqrt(math.pi ** 2 + a * a)
    w = (math.pi - math.acos(damp)) / (travel_s * math.sqrt(1 - damp * damp))
    return w / (2 * math.pi), damp


def _ease(x):
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def roulette(target, t0, t1):
    """[(time, row index)]: the highlight runs down the rows, wraps once and slows down onto `target`."""
    seq = list(range(len(TIERS))) + list(range(target + 1))
    n = len(seq)
    return [(t0 + (t1 - t0) * (i / n) ** 1.7, r) for i, r in enumerate(seq)]


FONT_TEASER = HERE / "fonts" / "cap" / "montserrat-900.ttf"
PUSH = 0.045                                  # the camera pushes in this much over one placement


def _cover(img, w, h):
    """img centre-cropped to the aspect of w x h and resized to it."""
    k = min(img.width / w, img.height / h)
    cw, ch = max(1, round(w * k)), max(1, round(h * k))
    box = ((img.width - cw) // 2, (img.height - ch) // 2, (img.width + cw) // 2, (img.height + ch) // 2)
    return img.crop(box).resize((w, h), Image.BILINEAR)


def _camera(im, size, z, fx, fy, dx=0.0, dy=0.0):
    """The whole frame pushed in z x towards (fx, fy), shifted by (dx, dy)."""
    if z == 1.0 and not dx and not dy:
        return im
    return im.transform(size, Image.AFFINE, (1 / z, 0, fx * (1 - 1 / z) + dx, 0, 1 / z, fy * (1 - 1 / z) + dy),
                        resample=Image.BILINEAR)


def teaser_sprites(text, k):
    """Words of the teaser line as sprites + their x offsets (drawn once; each word fades up on its own)."""
    f = _font(FONT_TEASER, 40 * k)
    words, x, out = text.split(), 0.0, []
    for wd in words:
        wpx = int(f.getlength(wd)) + 24
        im = Image.new("RGBA", (wpx, int(64 * k)), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.text((12, im.height // 2), wd, font=f, fill=(0, 0, 0, 200), anchor="lm", stroke_width=max(2, round(3 * k)),
               stroke_fill=(0, 0, 0, 200))
        d.text((12, im.height // 2), wd, font=f, fill=(255, 255, 255, 255), anchor="lm")
        out.append((im, x))
        x += f.getlength(wd + " ")
    return out, x


def draw_teaser(im, sprites, total, t, t0, k):
    """Word by word: each word fades up from blurred to sharp with a small rise (no hard reveal, no bounce)."""
    x0 = (im.width - total) / 2
    y0 = im.height - round(50 * k)
    for i, (spr, dx) in enumerate(sprites):
        u = (t - t0 - 0.075 * i) / 0.32
        if u <= 0:
            continue
        e = _ease(u)
        w = spr.filter(ImageFilter.GaussianBlur(7 * k * (1 - e))) if e < 1 else spr
        if e < 1:
            w.putalpha(w.getchannel("A").point(lambda v: int(v * e)))
        im.paste(w, (round(x0 + dx - 12), round(y0 - spr.height / 2 + 12 * k * (1 - e))), w)


def fly_in_frames(entries_before, new_entry, fps=30, dur=1.9, size=(1920, 1080), brand="bananas", cap=None,
                  land=None, overshoot=30, teaser=None):
    """
    Generator of PIL frames, the placement of one moment. The previous shot TRANSFORMS into it (no cut to a
    board, no crossfade): new_entry["from"] = the last frame of the moment (falls back to its thumbnail).
      0 .. 0.20 dur   that frame shrinks from the full screen into a card in the middle (fast, then slow);
                      the board is revealed behind it and the picture inside turns into the payoff thumbnail
      .. 0.42 dur     the card keeps easing smaller while the row highlight runs down the labels and slows
                      onto its tier (suspense)
      .. land         it flies into its slot, still shrinking (land = 0.58 dur): one continuous way down in
                      size, never up again, no rotation, no bounce
      land ..         impact: the row's glow pulses in the tier colour (twice for S), the label flashes,
                      a short shake on S only; the teaser line (if any) fades up word by word.
    The camera pushes in slowly on the target row the whole time, so nothing ever stands still.
    """
    land = land if land is not None else LAND_FR * dur
    entries = list(entries_before) + [new_entry]
    all_slots = slots(entries, size, cap)
    base = _base(size, brand)
    for e, (x, y, w_, h_) in zip(entries[:-1], all_slots[:-1]):
        base.paste(thumb_sprite(e, w_, h_), (x, y))
    g = geometry(size)
    k = g["k"]
    W_, H_ = size
    sx, sy, side, high = all_slots[-1]
    asp = high / side
    ti = TIERS.index(new_entry["tier"])
    top = new_entry["tier"] == "S"
    big_w = round(min(W_ * 0.46, H_ * 0.56 / asp))
    big_h = round(big_w * asp)
    card = thumb_sprite(new_entry, big_w, big_h).convert("RGB")           # the payoff thumbnail with the @label
    src = new_entry.get("from") or new_entry.get("thumb")
    if src is not None and not isinstance(src, Image.Image):
        src = Image.open(src)
    full = _cover(src.convert("RGB"), W_, H_) if src is not None else card.resize(size)
    cx0, cy0 = W_ / 2 + g["row_h"] / 2 * (W_ > H_), g["top"] + (len(TIERS) * (g["row_h"] + g["gap"])) / 2
    cx1, cy1 = sx + side / 2, sy + high / 2
    t_card, t_fly = 0.20 * dur, 0.42 * dur
    steps = roulette(ti, 0.10 * dur, t_fly)
    row_y = lambda i: g["top"] + i * (g["row_h"] + g["gap"])
    col = TIER_RGB[new_entry["tier"]]
    pad = round(46 * k)
    glow = Image.new("L", (W_ - 2 * g["mx"] + 2 * pad, g["row_h"] + 2 * pad), 0)
    ImageDraw.Draw(glow).rectangle((pad, pad, glow.width - pad - 1, glow.height - pad - 1), outline=255, width=max(3, round(9 * k)))
    glow = glow.filter(ImageFilter.GaussianBlur(13 * k))
    glow_rgb = Image.new("RGB", glow.size, col)
    white_lab = Image.new("RGB", (g["row_h"], g["row_h"]), (255, 255, 255))
    acc = ACCENT.get(brand) or (255, 255, 255)
    tz = teaser_sprites(teaser, k) if teaser else None
    for i in range(round(dur * fps)):
        t = i / fps
        u = t - land
        # --- the card: one continuous shrink  full screen -> centre card -> slot
        if t < t_card:
            e = 1 - (1 - t / t_card) ** 3                       # fast first, then slow
            w = W_ + (big_w - W_) * e
            h = H_ + (big_h - H_) * e
            cx, cy = W_ / 2 + (cx0 - W_ / 2) * e, H_ / 2 + (cy0 - H_ / 2) * e
            mix = _ease((e - 0.45) / 0.5)                       # the picture inside becomes the thumbnail
        elif t < t_fly:
            q = (t - t_card) / (t_fly - t_card)
            w, h = big_w * (1 - 0.06 * q), big_h * (1 - 0.06 * q)   # never still: it keeps easing smaller
            cx, cy, mix = cx0, cy0, 1.0
        elif t < land:
            q = (t - t_fly) / (land - t_fly)
            e = q * q * (3 - 2 * q)
            w = big_w * 0.94 + (side - big_w * 0.94) * e
            h = big_h * 0.94 + (high - big_h * 0.94) * e
            cx, cy, mix = cx0 + (cx1 - cx0) * e, cy0 + (cy1 - cy0) * e, 1.0
        else:
            w, h, cx, cy, mix = side, high, cx1, cy1, 1.0
        w, h = max(2, round(w)), max(2, round(h))
        if t == 0:                                              # frame 0 is exactly the previous shot
            yield full
            continue
        im = base.copy()
        d = ImageDraw.Draw(im)
        # --- roulette highlight on the labels (until the landing)
        if t < land:
            cur = None
            for ts, r in steps:
                if t >= ts:
                    cur = (ts, r)
            if cur:
                a = 0.55 * math.exp(-(t - cur[0]) / 0.09) + 0.25
                y = row_y(cur[1])
                box = (g["mx"], y, g["mx"] + g["row_h"], y + g["row_h"])
                im.paste(Image.blend(im.crop(box), white_lab, min(0.8, a)), box)
                d.rectangle((g["mx"], y, W_ - g["mx"] - 1, y + g["row_h"] - 1), outline=acc, width=max(2, round(3 * k)))
        # --- landing: glow pulse of the row (S pulses twice) + label flash
        if u >= 0:
            a = math.exp(-u / 0.22) + (0.7 * math.exp(-((u - 0.30) / 0.09) ** 2) if top else 0.0)
            if a > 0.02:
                im.paste(glow_rgb, (g["mx"] - pad, row_y(ti) - pad), glow.point(lambda v: int(min(255, v * min(1.0, a) * 1.25))))
                box = (g["mx"], row_y(ti), g["mx"] + g["row_h"], row_y(ti) + g["row_h"])
                im.paste(Image.blend(im.crop(box), white_lab, min(0.6, 0.6 * a)), box)
        if mix < 1:
            spr = Image.blend(_cover(full, w, h), card.resize((w, h), Image.BILINEAR), mix) if mix > 0 else _cover(full, w, h)
        else:
            spr = card.resize((w, h), Image.LANCZOS)
        lift = min(1.0, max(0.0, w / side - 1) / 1.5)
        if lift > 0.02:                                         # the shadow it throws while above the board
            m = round(40 * k)
            sh = Image.new("L", (w + 2 * m, h + 2 * m), 0)
            sh.paste(int(255 * 0.6 * lift), (m, m, m + w, m + h))
            sh = sh.filter(ImageFilter.GaussianBlur(16 * k))
            off = round(26 * k * lift)
            im.paste((0, 0, 0), (round(cx - sh.width / 2) + off, round(cy - sh.height / 2) + off), sh)
        x0, y0 = round(cx - w / 2), round(cy - h / 2)
        im.paste(spr, (x0, y0))
        if t < land and w < W_ * 0.9:                           # a thin accent frame while it is in the air
            ImageDraw.Draw(im).rectangle((x0, y0, x0 + w - 1, y0 + h - 1), outline=acc, width=max(2, round(4 * k)))
        # --- camera: slow push towards the target row; a short shake on S only
        z = 1 + PUSH * t / dur
        dx = dy = 0.0
        if u >= 0 and top:
            amp = 12 * k * math.exp(-u / 0.12)
            dx, dy = amp * math.sin(2 * math.pi * 17 * u), amp * math.cos(2 * math.pi * 23 * u)
        im = _camera(im, size, z, W_ / 2, row_y(ti) + g["row_h"] / 2, dx, dy)
        if tz:
            im = im.convert("RGBA") if im.mode != "RGBA" else im
            draw_teaser(im, tz[0], tz[1], t, land + 0.12, k)
            im = im.convert("RGB")
        yield im


def hold_frames(entries, dur=2.0, fps=30, size=(1920, 1080), brand="bananas", cap=None, zoom0=1.0, focus=None, sweep=True):
    """
    The board on its own (empty at the start, full at the end), never a still frame: the camera keeps
    pushing in from zoom0 (continue where the last placement stopped: zoom0 = 1 + PUSH, focus = its tier)
    and one soft band of light crosses the thumbnails.
    """
    g = geometry(size)
    im0 = board(entries, size, brand, cap)
    fy = (g["top"] + TIERS.index(focus) * (g["row_h"] + g["gap"]) + g["row_h"] / 2) if focus else size[1] / 2
    band_w = round(260 * g["k"])
    band = Image.new("L", (band_w, 1), 0)
    for x in range(band_w):
        band.putpixel((x, 0), int(70 * math.sin(math.pi * x / band_w) ** 2))
    band = band.resize((band_w, size[1] - g["top"]))
    white = Image.new("RGB", band.size, (255, 255, 255))
    n = round(dur * fps)
    for i in range(n):
        t = i / fps
        im = im0
        if sweep and entries:
            im = im0.copy()
            x = round(g["items_x"] - band_w + (g["items_w"] + 2 * band_w) * _ease((t - 0.25) / max(0.6, dur - 0.7)))
            x = max(g["items_x"] - band_w, x)
            box = (max(x, 0), g["top"], min(x + band_w, size[0]), size[1])
            if box[2] > box[0]:
                bx = box[0] - x
                im.paste(white.crop((bx, 0, bx + box[2] - box[0], band.height)), box[:2], band.crop((bx, 0, bx + box[2] - box[0], band.height)))
        yield _camera(im, size, zoom0 + 0.025 * t / max(dur, 0.1), size[0] / 2, fy)


# ---------------------------------------------------------------------------
# sound (ours, synthesized) + encoding

def _sweep(noise, f0, f1, q=1.6):
    """Band-pass whose centre glides f0 -> f1 (state variable filter): the 'doppler' of a whoosh."""
    n = noise.size
    f = 2 * np.sin(np.pi * np.geomspace(f0, f1, n) / SR)
    out = np.zeros(n)
    low = band = 0.0
    damp = 1 / q
    for i in range(n):
        low += f[i] * band
        high = noise[i] - low - damp * band
        band += f[i] * high
        out[i] = band
    return out


def _tone(freq, n, decay, harm=0.25):
    t = np.arange(n) / SR
    ph = 2 * np.pi * freq * t
    return (np.sin(ph) + harm * np.sin(2 * ph)) * np.exp(-t / decay)


def _put(out, x, at, gain=1.0):
    i = int(SR * at)
    if 0 <= i < out.size:
        x = x[: out.size - i]
        out[i: i + x.size] += gain * x


def whoosh(dur):
    """Air rushing past: noise through a rising band-pass + a quiet tone gliding up, cut dead at the end."""
    rnd = np.random.default_rng(7)
    n = int(SR * dur)
    r = np.linspace(0, 1, n)
    air = _sweep(rnd.normal(0, 1, n), 260, 4200)
    tone = np.sin(2 * np.pi * np.cumsum(190 * 2 ** (1.9 * r ** 1.5)) / SR)
    x = (air / (np.abs(air).max() + 1e-9) + 0.16 * tone) * r ** 2.3
    x[-int(SR * 0.006):] *= np.linspace(1, 0, int(SR * 0.006))
    return x / np.abs(x).max()


def impact(big=False):
    """Thud + snap with a sub: a sine dropping to 45 Hz, a short mid knock, a 3 ms bright snap."""
    rnd = np.random.default_rng(9)
    n = int(SR * (0.75 if big else 0.45))
    t = np.arange(n) / SR
    sub = np.sin(2 * np.pi * np.cumsum(45 + 75 * np.exp(-t / 0.03)) / SR) * np.exp(-t / (0.30 if big else 0.15))
    thud = np.sin(2 * np.pi * np.cumsum(150 + 260 * np.exp(-t / 0.012)) / SR) * np.exp(-t / 0.045)
    noise = rnd.normal(0, 1, n)
    snap = (noise - np.convolve(noise, np.ones(10) / 10, mode="same")) * np.exp(-t / 0.011)
    x = np.tanh(1.5 * (1.0 * sub + 0.6 * thud)) + 0.42 * snap
    x[: int(SR * 0.001)] *= np.linspace(0, 1, int(SR * 0.001))
    return x / np.abs(x).max()


def _mallet(f):
    """A soft wooden knock with a pitch (marimba-like: fundamental + 4th harmonic, gone in ~0.15 s)."""
    n = int(SR * 0.3)
    t = np.arange(n) / SR
    x = np.sin(2 * np.pi * f * t) * np.exp(-t / 0.075) + 0.22 * np.sin(2 * np.pi * 4 * f * t) * np.exp(-t / 0.02)
    return x * np.minimum(1, t / 0.002)


def stinger(tier, n):
    """What the tier sounds like after the impact (starts at the landing). Mono, length n."""
    rnd = np.random.default_rng(TIERS.index(tier) + 20)
    out = np.zeros(n)
    if tier == "S":                                            # sparkle + a short bright chord: the win
        for f, at in zip((1046.5, 1318.5, 1568.0, 2093.0), (0.0, 0.035, 0.07, 0.105)):
            _put(out, _tone(f, int(SR * 0.7), 0.20, 0.3), at, 0.20)
        for j in range(12):
            _put(out, _tone(float(rnd.choice([2637.0, 3136.0, 4186.0, 5274.0, 6272.0])), int(SR * 0.25), 0.06, 0.0),
                 0.06 + 0.05 * j + rnd.uniform(0, 0.02), 0.07 * (1 - j / 14))
    elif tier == "A":                                          # marimba-like: two soft knocks up
        _put(out, _mallet(523.3), 0.0, 0.30)
        _put(out, _mallet(659.3), 0.09, 0.30)
    elif tier == "B":
        _put(out, _mallet(440.0), 0.0, 0.28)
    elif tier == "C":
        _put(out, _mallet(329.6), 0.0, 0.28)
    else:                                                      # D: deflate (slide down with vibrato) + a boing
        m = int(SR * 0.5)
        t = np.arange(m) / SR
        f = 520 * (140 / 520) ** (t / 0.5) * (1 + 0.035 * np.sin(2 * np.pi * 9 * t))
        _put(out, np.sin(2 * np.pi * np.cumsum(f) / SR) * np.minimum(1, t / 0.02) * (1 - t / 0.5) ** 0.7, 0.03, 0.22)
        b = np.sin(2 * np.pi * 95 * t + 7 * np.exp(-t / 0.16) * np.sin(2 * np.pi * 12 * t)) * np.exp(-t / 0.2)
        _put(out, b, 0.0, 0.30)
    return out


def crowd_riser(dur):
    """S only: a crowd-like swell under the roulette (wide noise band, slow flutter), rising into the landing."""
    rnd = np.random.default_rng(13)
    n = int(SR * dur)
    r = np.linspace(0, 1, n)
    x = _sweep(rnd.normal(0, 1, n), 500, 1900, q=0.7) * (1 + 0.25 * np.sin(2 * np.pi * 6.5 * r * dur)) * r ** 2
    return x / (np.abs(x).max() + 1e-9)


_sound_cache = {}


def fly_sound(dur=1.9, land=None, tier="B"):
    """
    The placement: a soft pop when the frame appears, ticks of the roulette (rising pitch), a whoosh with a
    pitch rise into the row, the impact (thud + snap + sub) and the tier's stinger: S = big hit + crowd-like
    riser + sparkle, A/B/C = soft marimba-like knocks (lower with the tier), D = comedic deflate + boing.
    """
    land = land if land is not None else LAND_FR * dur
    key = (round(dur, 3), round(land, 3), tier)
    if key in _sound_cache:
        return _sound_cache[key]
    ti = TIERS.index(tier)
    out = np.zeros(int(SR * dur))
    t_fly = 0.42 * dur
    m = int(SR * 0.09)
    t = np.arange(m) / SR
    _put(out, np.sin(2 * np.pi * np.cumsum(300 + 500 * t / 0.09) / SR) * np.exp(-t / 0.03), 0.0, 0.22)      # pop
    steps = roulette(ti, 0.10 * dur, t_fly)
    for j, (ts, r) in enumerate(steps):
        _put(out, _tone(900 * 2 ** (j / len(steps)), int(SR * 0.04), 0.012, 0.0), ts, 0.13)
    _put(out, whoosh(land - t_fly + 0.04), t_fly - 0.04, 0.60)
    if tier == "S":
        _put(out, crowd_riser(land - 0.1), 0.1, 0.20)
    _put(out, impact(big=tier == "S"), land, 1.15 if tier == "S" else 0.95)
    _put(out, stinger(tier, out.size - int(SR * land)), land + 0.012, 1.0)
    k = int(SR * 0.03)
    out[-k:] *= np.linspace(1, 0, k)
    _sound_cache[key] = np.tanh(1.5 * out) * 0.92             # soft clip: the impact stays fat, never harsh
    return _sound_cache[key]


VENC = ["-c:v", "libx264", "-preset", "medium", "-crf", "17"]


def encode(frames, out, fps=30, size=(1920, 1080), sound=None, venc=None):
    """PIL frames -> mp4 through an ffmpeg pipe (yuv420p, timescale 90000, AAC 48 kHz stereo)."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{size[0]}x{size[1]}", "-framerate", str(fps), "-i", "pipe:0"]
        if sound is not None:
            wav = Path(tmp) / "a.wav"
            with wave.open(str(wav), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SR)
                w.writeframes((np.clip(sound, -1, 1) * 32767).astype(np.int16).tobytes())
            cmd += ["-i", str(wav)]
        else:
            cmd += ["-f", "lavfi", "-i", f"anullsrc=r={SR}:cl=stereo"]
        cmd += ["-map", "0:v", "-map", "1:a", "-shortest"] + (venc or VENC) + \
               ["-r", str(fps), "-pix_fmt", "yuv420p", "-video_track_timescale", "90000",
                "-c:a", "aac", "-b:a", "192k", "-ar", str(SR), "-ac", "2", "-movflags", "+faststart", str(out)]
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        n = 0
        for im in frames:
            p.stdin.write(im.convert("RGB").tobytes())
            n += 1
        p.stdin.close()
        if p.wait() != 0:
            raise RuntimeError(f"ffmpeg failed: {out}")
    return out


def render_fly_in(entries_before, new_entry, out, fps=30, dur=1.9, size=(1920, 1080), brand="bananas", cap=None,
                  capacity=None, audio=True, venc=None, land=None, teaser=None):
    """The placement of `new_entry` as a video segment -> path of the mp4 (teaser = a line under the board)."""
    cap = cap or capacity
    frames = fly_in_frames(entries_before, new_entry, fps, dur, size, brand, cap, land, teaser=teaser)
    return encode(frames, out, fps, size, fly_sound(dur, land, new_entry["tier"]) if audio else None, venc)


def render_hold(entries, out, dur=2.0, fps=30, size=(1920, 1080), brand="bananas", cap=None, capacity=None, venc=None,
                zoom0=1.0, focus=None):
    """The board alone (empty at the start, full at the end) as a silent segment, slowly pushing in -> path."""
    return encode(hold_frames(entries, dur, fps, size, brand, cap or capacity, zoom0, focus), out, fps, size, None, venc)


# ---------------------------------------------------------------------------
# preview

def _demo():
    dest = HERE / "out" / "_ukazka"
    vids = sorted(p for p in (HERE / "out").rglob("*.mp4") if "_ukazka" not in p.parts and p.stat().st_size > 2e6)
    if not vids:
        vids = sorted((HERE / "out" / "_ukazka").glob("long_*.mp4"))
    names = ["@kaicenat", "@xqc", "@ishowspeed", "@caseoh_", "@jynxzi"]
    order = ["B", "S", "D", "A", "C"]
    entries = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, (name, t) in enumerate(zip(names, order)):
            v = vids[i % len(vids)]
            dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(v)],
                                       capture_output=True, text=True).stdout.strip() or 10)
            jpg = Path(tmp) / f"{i}.jpg"
            subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", f"{dur * (0.15 + 0.16 * i):.2f}", "-i", str(v),
                            "-frames:v", "1", "-vf", "scale=-2:720", str(jpg)], check=True)
            entries.append({"tier": t, "thumb": Image.open(jpg).copy(), "label": name})
        cap = capacity(order)
        fps, size = 30, (1920, 1080)

        def film():
            yield from hold_frames([], 1.0, fps, size, "bananas", cap)
            for i, e in enumerate(entries):
                yield from fly_in_frames(entries[:i], e, fps, 1.9, size, "bananas", cap, teaser="#1 IS INSANE, STAY TILL THE END" if i == 3 else None)
            yield from hold_frames(entries, 1.5, fps, size, "bananas", cap, 1 + PUSH, entries[-1]["tier"])

        snd = np.concatenate([np.zeros(SR)] + [fly_sound(1.9, None, e["tier"]) for e in entries] + [np.zeros(int(SR * 1.5))])
        out = encode(film(), dest / "tier_ukazka.mp4", fps, size, snd)
        # contact sheet: the flight of the 4th thumbnail + the full board
        shots = [im.copy() for im in fly_in_frames(entries[:1], entries[1], fps, 1.9, size, "bananas", cap)]
        picks = [shots[i] for i in (1, 4, 9, 20, 28, 33)] + [board([], size, "bananas", cap), board(entries, size, "bananas", cap)]
        sheet = Image.new("RGB", (4 * 960, 2 * 540), (40, 40, 40))
        for i, im in enumerate(picks):
            sheet.paste(im.resize((960, 540), Image.LANCZOS), ((i % 4) * 960, (i // 4) * 540))
        sheet.save(dest / "tier_ukazka_sheet.jpg", quality=88)
    print(out)


if __name__ == "__main__":
    _demo()
