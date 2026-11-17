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
LAND = 0.55                                   # s: the thumbnail touches its slot (the 'tok')


# ---------------------------------------------------------------------------
# layout

def geometry(size=(1920, 1080)):
    """Board rectangle and row metrics for a frame size (all in px)."""
    w, h = size
    k = h / 1080
    mx, top, gap = round(70 * k), round(72 * k), max(2, round(4 * k))
    row_h = int((h - top - round(52 * k) - gap * (len(TIERS) - 1)) / len(TIERS))
    return {"w": w, "h": h, "k": k, "mx": mx, "top": top, "gap": gap, "row_h": row_h,
            "items_x": mx + row_h + gap, "items_w": w - mx - (mx + row_h + gap)}


def capacity(tiers):
    """['S', 'B', 'B', ...] of the whole video -> {'S': 1, 'B': 2, ...}: the final size of every row."""
    return {t: sum(1 for x in tiers if x == t) for t in TIERS}


def slots(entries, size=(1920, 1080), cap=None):
    """(x, y, side) of every entry, in order. `cap` (see capacity) fixes the thumb size of a row ahead."""
    g = geometry(size)
    seen, out = {t: 0 for t in TIERS}, []
    count = {t: sum(1 for e in entries if e["tier"] == t) for t in TIERS}
    for e in entries:
        t = e["tier"]
        n = max(PER_ROW, count[t], (cap or {}).get(t, 0))
        side = min(g["row_h"], g["items_w"] // n)
        y = g["top"] + TIERS.index(t) * (g["row_h"] + g["gap"]) + (g["row_h"] - side) // 2
        out.append((g["items_x"] + seen[t] * side, y, side))
        seen[t] += 1
    return out


# ---------------------------------------------------------------------------
# drawing

def _font(path, px):
    return ImageFont.truetype(str(path), max(8, int(px)))


def thumb_sprite(entry, side):
    """Square thumbnail `side` px: the frame centre-cropped, a hairline gap, the @label on a dark strip."""
    src = entry.get("thumb")
    if src is not None and not isinstance(src, Image.Image):
        src = Image.open(src)
    im = Image.new("RGB", (side, side), BG)
    inner = side - 2                                           # 1 px of board between neighbours
    if src is None:
        pic = Image.new("RGB", (inner, inner), (58, 58, 54))
    else:
        src = src.convert("RGB")
        s = min(src.size)
        box = ((src.width - s) // 2, (src.height - s) // 2, (src.width + s) // 2, (src.height + s) // 2)
        pic = src.crop(box).resize((inner, inner), Image.LANCZOS)
    label = (entry.get("label") or "").strip()
    if label:
        px = max(11, round(side * 0.118))
        f = _font(FONT_LABEL, px)
        while px > 10 and f.getlength(label) > inner - 10:
            px -= 1
            f = _font(FONT_LABEL, px)
        while f.getlength(label) > inner - 10 and len(label) > 4:    # still too long: cut, keep it honest
            label = label[:-2] + "…"
        strip_h = round(px * 1.75)
        strip = Image.new("RGBA", (inner, strip_h), (0, 0, 0, 205))
        pic = pic.convert("RGBA")
        pic.alpha_composite(strip, (0, inner - strip_h))
        d = ImageDraw.Draw(pic)
        d.text((inner / 2, inner - strip_h / 2), label, font=f, fill=(255, 255, 255, 255), anchor="mm")
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
    for e, (x, y, side) in zip(entries, slots(entries, size, cap)):
        im.paste(thumb_sprite(e, side), (x, y))
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


def fly_in_frames(entries_before, new_entry, fps=30, dur=1.5, size=(1920, 1080), brand="bananas", cap=None,
                  land=LAND, overshoot=34):
    """
    Generator of PIL frames: `new_entry` flies in from the right edge, springs into its slot (touches it at
    `land` s, overshoots by `overshoot` px and settles), the row flashes once on landing. The rest is still.
    """
    entries = list(entries_before) + [new_entry]
    all_slots = slots(entries, size, cap)
    base = _base(size, brand)
    for e, (x, y, side) in zip(entries[:-1], all_slots[:-1]):
        base.paste(thumb_sprite(e, side), (x, y))
    g = geometry(size)
    sx, sy, side = all_slots[-1]
    big = thumb_sprite(new_entry, side * 2).convert("RGBA")    # drawn at 2x: sharp while it is still large
    t0 = 0.06
    x_from = g["w"] + side * 0.35                              # centre starts outside the right edge
    cx_to, cy_to = sx + side / 2, sy + side / 2
    dist = x_from - cx_to
    freq, damp = _flight_spring(dist, land - t0, overshoot * g["k"])
    row_y = g["top"] + TIERS.index(new_entry["tier"]) * (g["row_h"] + g["gap"])
    flash_row = Image.new("RGB", (g["items_w"], g["row_h"]), (255, 255, 255))
    flash_lab = Image.new("RGB", (g["row_h"], g["row_h"]), (255, 255, 255))
    for i in range(round(dur * fps)):
        t = i / fps
        im = base.copy()
        u = t - land
        if u >= 0:                                             # one soft flash of the row, gone in ~0.4 s
            a = math.exp(-u / 0.11)
            if a > 0.02:
                box = (g["items_x"], row_y, g["items_x"] + g["items_w"], row_y + g["row_h"])
                im.paste(Image.blend(im.crop(box), flash_row, 0.20 * a), box)
                box = (g["mx"], row_y, g["mx"] + g["row_h"], row_y + g["row_h"])
                im.paste(Image.blend(im.crop(box), flash_lab, 0.42 * a), box)
        s = spring(t - t0, freq, damp)
        p = min(1.0, max(0.0, (t - t0) / (land - t0)))        # 0..1 until the landing
        cx = x_from + (cx_to - x_from) * s
        cy = cy_to - 54 * g["k"] * math.sin(math.pi * p) * (1 - p)        # a small arc on the way in
        scale = 1 + 0.5 * (1 - spring(t - t0, freq, min(0.9, damp + 0.06)))   # near -> lands with a tiny squash
        rot = 8.0 * (1 - spring(t - t0, freq * 1.05, max(0.3, damp - 0.12)))  # tilt wobbles out
        if t >= t0:
            w = max(2, round(side * scale))
            spr = big.resize((w, w), Image.LANCZOS)
            if abs(rot) > 0.05:
                spr = spr.rotate(rot, resample=Image.BICUBIC, expand=True)
            lift = max(0.0, scale - 1) / 0.5                   # 1 in the air, 0 on the board
            if lift > 0.02:                                    # the shadow it throws while above the board
                m = round(40 * g["k"])
                sh = Image.new("L", (spr.width + 2 * m, spr.height + 2 * m), 0)
                sh.paste(spr.getchannel("A").point(lambda v: int(v * 0.55 * lift)), (m, m))
                sh = sh.filter(ImageFilter.GaussianBlur(14 * g["k"]))
                off = round(22 * g["k"] * lift)
                im.paste((0, 0, 0), (round(cx - sh.width / 2) + off, round(cy - sh.height / 2) + off), sh)
            im.paste(spr, (round(cx - spr.width / 2), round(cy - spr.height / 2)), spr)
        yield im


# ---------------------------------------------------------------------------
# sound (ours, synthesized) + encoding

def fly_sound(dur=1.5, land=LAND):
    """Soft airy whoosh that swells towards the landing, then a short wooden 'tok'. Mono float array."""
    rnd = np.random.default_rng(5)
    out = np.zeros(int(SR * dur))
    n = int(SR * land)
    ramp = np.linspace(0, 1, n)
    noise = rnd.normal(0, 1, n)
    dark = np.convolve(noise, np.ones(48) / 48, mode="same")
    bright = np.convolve(noise, np.ones(8) / 8, mode="same")
    out[:n] += 0.5 * ((1 - ramp) * dark + ramp * bright) * ramp ** 2.4
    m = int(SR * 0.22)
    t = np.arange(m) / SR
    f = 190 + 230 * np.exp(-t / 0.018)
    tok = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.05)
    click = np.convolve(rnd.normal(0, 1, m), np.ones(5) / 5, mode="same") * np.exp(-t / 0.006)
    out[n: n + m] += (0.75 * tok + 0.22 * click)[: out.size - n]
    return out / max(1e-9, np.abs(out).max()) * 0.5


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


def render_fly_in(entries_before, new_entry, out, fps=30, dur=1.5, size=(1920, 1080), brand="bananas", cap=None,
                  capacity=None, audio=True, venc=None, land=LAND):
    """The landing of `new_entry` as a video segment -> path of the mp4."""
    cap = cap or capacity
    frames = fly_in_frames(entries_before, new_entry, fps, dur, size, brand, cap, land)
    return encode(frames, out, fps, size, fly_sound(dur, land) if audio else None, venc)


def render_hold(entries, out, dur=2.0, fps=30, size=(1920, 1080), brand="bananas", cap=None, capacity=None, venc=None):
    """A still board (empty at the start, full at the end) as a silent video segment -> path."""
    im = board(entries, size, brand, cap or capacity)
    return encode((im for _ in range(round(dur * fps))), out, fps, size, None, venc)


# ---------------------------------------------------------------------------
# preview

def _demo():
    dest = HERE / "out" / "_ukazka"
    vids = sorted(p for p in (HERE / "out").rglob("*.mp4") if "_ukazka" not in p.parts and p.stat().st_size > 2e6)
    if not vids:
        vids = sorted((HERE / "out" / "_ukazka").glob("long_*.mp4"))
    names = ["@kaicenat", "@xqc", "@ishowspeed", "@caseoh_", "@jynxzi"]
    order = ["B", "S", "C", "A", "B"]
    entries = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, (name, t) in enumerate(zip(names, order)):
            v = vids[i % len(vids)]
            dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(v)],
                                       capture_output=True, text=True).stdout.strip() or 10)
            jpg = Path(tmp) / f"{i}.jpg"
            subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", f"{dur * (0.15 + 0.16 * i):.2f}", "-i", str(v),
                            "-frames:v", "1", "-vf", "scale=-2:540", str(jpg)], check=True)
            entries.append({"tier": t, "thumb": Image.open(jpg).copy(), "label": name})
        cap = capacity(order)
        fps, size = 30, (1920, 1080)

        def film():
            empty = board([], size, "bananas", cap)
            for _ in range(round(1.0 * fps)):
                yield empty
            for i, e in enumerate(entries):
                yield from fly_in_frames(entries[:i], e, fps, 1.5, size, "bananas", cap)
            full = board(entries, size, "bananas", cap)
            for _ in range(round(1.5 * fps)):
                yield full

        snd = np.concatenate([np.zeros(SR)] + [fly_sound(1.5)] * len(entries) + [np.zeros(int(SR * 1.5))])
        out = encode(film(), dest / "tier_ukazka.mp4", fps, size, snd)
        # contact sheet: the flight of the 4th thumbnail + the full board
        shots = [im.copy() for im in fly_in_frames(entries[:3], entries[3], fps, 1.5, size, "bananas", cap)]
        picks = [shots[i] for i in (3, 9, 14, 16, 18, 24)] + [board([], size, "bananas", cap), board(entries, size, "bananas", cap)]
        sheet = Image.new("RGB", (4 * 960, 2 * 540), (40, 40, 40))
        for i, im in enumerate(picks):
            sheet.paste(im.resize((960, 540), Image.LANCZOS), ((i % 4) * 960, (i // 4) * 540))
        sheet.save(dest / "tier_ukazka_sheet.jpg", quality=88)
    print(out)


if __name__ == "__main__":
    _demo()
