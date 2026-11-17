"""
nahled.py - INPUTS for the YouTube thumbnail of a long-form compilation (or a short).

Aleš 17. 11.: thumbnails are made by an AI image generator, not composited here. This script only
prepares what the generator gets, in <out>/nahled_vstup/:
  frame.jpg   payoff frame of the best moment, 16:9, zoomed into the game: the side with the
              Twitch chat overlay and the HUD at the bottom are pushed out of the picture
  face.jpg    the streamer's face from his facecam, the frame around the payoff with the most
              expression (mouth / eyes moving away from his neutral face, loud audio) that is
              also sharp; upscaled with Lanczos at most 2.5x + light unsharp
  meta.json   brand, series, channel, tags, 3 text options (2-4 words, never the title),
              brand colours, where the character is in frame.jpg (YOLOX box, conf >= 0.4)

Usage:
  python nahled.py long out/_long/bananas/2026-11-19_funny-day.meta.json
  python nahled.py short out/xqc/20261117_twitch_xyz/meta.json --znacka fanpage
  python nahled.py moment --source x.mp4 --peak 12.3 --channel xqc --znacka bananas --tag crash --out dir
"""

import argparse
import json
import random
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

import klipuj as K

HERE = Path(__file__).resolve().parent
FW, FH = 1920, 1080
BRAND_LOOK = {
    "bananas": {"name": "gta6bananaclips", "colors": ["#FFE135", "#111111", "#FFFFFF"], "mascot": "assets/brand/pfp_bananas.webp",
                "style": "funny, bright, banana yellow, cartoon banana mascot"},
    "fanpage": {"name": "gta6fanpageclip", "colors": ["#FF4FD8", "#0B0B12", "#FFFFFF"], "mascot": None,
                "style": "sleek, dark, neon pink, Vice City night"},
}

# 2-4 words, never the title. By series first, then by the best moment's strongest tag.
TEXTS = {
    "bananas": {
        "glitches": ["GTA 6 IS BROKEN", "PHYSICS SAID NO", "WHAT IS THIS"],
        "chases": ["COPS HAD NO CHANCE", "FULL SEND", "HE GOT AWAY?"],
        "funny": ["HE GOT LAUNCHED", "NOT LIKE THIS", "INSTANT REGRET", "BRO WHAT"],
        "glitch": ["GTA 6 IS BROKEN", "PHYSICS SAID NO"],
        "chase": ["COPS HAD NO CHANCE", "FULL SEND"],
        "crash": ["HE GOT LAUNCHED", "WORST DRIVER EVER", "TOTAL WRECK"],
        "rage": ["HE LOST IT", "HE'S SO MAD", "RAGE QUIT?"],
        "first-reaction": ["FIRST TIME IN VICE CITY", "THIS IS GTA 6"],
        "wow": ["NO WAY", "HOW?!", "BRO WHAT"],
        None: ["NOT LIKE THIS", "BRO WHAT", "HE GOT LAUNCHED"],
    },
    "fanpage": {
        "glitches": ["GTA 6 IS BROKEN", "GLITCH CITY", "WHAT IS THIS"],
        "chases": ["CLEANEST ESCAPE", "ZERO CHILL", "FULL SEND"],
        "funny": ["NOT LIKE THIS", "ZERO CHILL", "BRO WHAT"],
        "glitch": ["GTA 6 IS BROKEN", "GLITCH CITY"],
        "chase": ["CLEANEST ESCAPE", "FULL SEND", "TOO CLEAN"],
        "crash": ["TOTAL CHAOS", "FULL SEND", "SENT HIM FLYING"],
        "rage": ["HE LOST IT", "ZERO CHILL"],
        "first-reaction": ["VICE CITY HITS DIFFERENT", "THIS IS GTA 6"],
        "wow": ["TOO CLEAN", "NO WAY", "VICE CITY CHAOS"],
        None: ["VICE CITY CHAOS", "TOO CLEAN", "NO WAY"],
    },
}

_cascade = _profile = _yolo = _eyes = None

def grab(src, t):
    """One frame at t seconds in the normalised 1920x1080 picture (facecam box coordinates)."""
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{max(t, 0):.2f}", "-i", str(src), "-frames:v", "1",
                          "-vf", K.src_norm(src), "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                         capture_output=True, timeout=120).stdout
    if len(raw) < FW * FH * 3:
        return None
    return np.frombuffer(raw[: FW * FH * 3], np.uint8).reshape(FH, FW, 3).copy()

def sharp(img):
    return float(cv2.Laplacian(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())

def face_box(img):
    """The biggest face (frontal, else profile both ways) in the facecam crop: (x, y, w, h) or None."""
    global _cascade, _profile
    if _cascade is None:
        _cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        _profile = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    ms = (max(24, g.shape[1] // 12),) * 2
    f = list(_cascade.detectMultiScale(g, 1.1, 5, minSize=ms))
    if not f:
        f = list(_profile.detectMultiScale(g, 1.1, 4, minSize=ms))
        gw = g.shape[1]
        f += [(gw - x - w, y, w, h) for x, y, w, h in _profile.detectMultiScale(cv2.flip(g, 1), 1.1, 4, minSize=ms)]
    return max(f, key=lambda b: b[2] * b[3]) if f else None

def cam_px(cam):
    return [int(round(v / 100 * d)) for v, d in zip(cam, (FW, FH, FW, FH))]

def best_face(src, peak, cam):
    """
    The most expressive sharp face around the payoff. Expression = how far the mouth and eye areas
    move away from the streamer's median (neutral) face; loud audio at that moment adds to it.
    Returns (face image upscaled, info) or (None, info).
    """
    dur = K.duration(src)
    x, y, w, h = cam_px(cam)
    ts, rms = K.rms_curve(src)
    loud_max = float(rms.max()) if rms.size else 1.0
    t_loud = peak                                  # the biggest audio peak near the payoff: the scream / laugh
    if rms.size:
        near = (ts >= peak - 1.5) & (ts <= peak + 1.5)
        if near.any():
            t_loud = float(ts[near][int(np.argmax(rms[near]))])
    cands = []
    for t in np.arange(max(0.2, t_loud - 1.0), min(dur - 0.1, t_loud + 1.05), 0.12):
        fr = grab(src, float(t))
        if fr is None:
            continue
        crop = fr[y: y + h, x: x + w]
        fb = face_box(crop)
        if fb is None:
            continue
        fx, fy, fw, fh = fb
        mouth = cv2.resize(cv2.cvtColor(crop[fy + int(fh * 0.6): fy + fh, fx: fx + fw], cv2.COLOR_BGR2GRAY), (32, 16))
        eyes = cv2.resize(cv2.cvtColor(crop[fy + int(fh * 0.2): fy + int(fh * 0.5), fx: fx + fw], cv2.COLOR_BGR2GRAY), (32, 16))
        m = int(fw * 0.95)                         # head + shoulders around the face
        cx, cy = fx + fw // 2, fy + int(fh * 0.55)
        face = crop[max(0, cy - int(m * 1.05)): min(h, cy + int(m * 1.2)), max(0, cx - m): min(w, cx + m)]
        loud = float(rms[np.argmin(np.abs(ts - t))]) / loud_max if rms.size else 0.0
        global _eyes
        if _eyes is None:
            _eyes = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_eye.xml")
        top = cv2.cvtColor(crop[fy: fy + int(fh * 0.6), fx: fx + fw], cv2.COLOR_BGR2GRAY)
        open_eyes = len(_eyes.detectMultiScale(top, 1.1, 4, minSize=(max(8, fw // 10),) * 2))   # eyes open, looking up
        cands.append({"t": round(float(t), 2), "face": face, "sharp": sharp(crop[fy: fy + fh, fx: fx + fw]),
                      "mouth": mouth.astype(np.float32), "eyes": eyes.astype(np.float32), "loud": loud,
                      "open": min(open_eyes, 2) / 2})
    if not cands:
        return None, {"found": False}
    mm = np.median(np.stack([c["mouth"] for c in cands]), axis=0)
    me = np.median(np.stack([c["eyes"] for c in cands]), axis=0)
    for c in cands:
        c["expr"] = float(np.abs(c["mouth"] - mm).mean() + 0.6 * np.abs(c["eyes"] - me).mean() + c["mouth"].std() * 0.3)
    ex, sh = max(c["expr"] for c in cands) or 1.0, max(c["sharp"] for c in cands) or 1.0
    best = max(cands, key=lambda c: 0.35 * c["expr"] / ex + 0.15 * c["sharp"] / sh + 0.25 * c["loud"] + 0.25 * c["open"])
    face = best["face"]
    scale = min(2.5, 720 / max(face.shape[0], 1))   # at most 2.5x (a smaller face beats a mushy one) ...
    scale = max(scale, 512 / max(face.shape[0], 1))  # ... but the generators want >= 512 px
    if scale > 1:
        face = cv2.resize(face, None, fx=scale, fy=scale, interpolation=cv2.INTER_LANCZOS4)
        face = cv2.addWeighted(face, 1.4, cv2.GaussianBlur(face, (0, 0), 1.6), -0.4, 0)
    return face, {"found": True, "t": best["t"], "expression": round(best["expr"] / ex, 2),
                  "sharpness": round(best["sharp"], 1), "upscale": round(max(scale, 1.0), 2)}

def overlay_side(img, cam):
    """Which edge carries the stream overlay (Twitch chat, alerts): 'left', 'right' or None."""
    g = cv2.Canny(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 80, 200)
    if cam:
        x, y, w, h = cam_px(cam)
        g[y: y + h, x: x + w] = 0
    band = FW // 4
    left, right = g[:, :band].mean(), g[:, -band:].mean()
    if max(left, right) < 6:
        return None
    return "left" if left > 1.3 * right else "right" if right > 1.3 * left else None

def game_frame(img, cam, box=None, zoom=1.45):
    """
    Zoom into the game around the character: the edges, where streams keep the chat overlay,
    alerts and the minimap, and the HUD at the bottom fall out. A clearly busier edge (chat)
    pushes the crop further away. Returns (crop 1920x1080, (x0, y0, scale), chat side).
    """
    cw, ch = int(FW / zoom), int(FH / zoom)
    side = overlay_side(img, cam)
    cx = box[0] + box[2] / 2 if box else FW / 2
    cx += {"left": 0.12 * FW, "right": -0.12 * FW}.get(side, 0)
    x0 = int(np.clip(cx - cw / 2, 0, FW - cw))
    y0 = int((FH - ch) * 0.3)                     # cut more at the bottom (HUD) than at the top
    crop = cv2.resize(img[y0: y0 + ch, x0: x0 + cw], (FW, FH), interpolation=cv2.INTER_LANCZOS4)
    return crop, (x0, y0, FW / cw), side

def character_box(img, cam):
    """Player character / car in the frame (YOLOX, conf >= 0.4, near the centre): [x, y, w, h] or None."""
    global _yolo
    if _yolo is None:
        _yolo = K.Yolox(K.MODEL)
    small = cv2.resize(img, (960, 540))
    cpx = [v / 2 for v in cam_px(cam)] if cam else None
    best, best_s = None, 0.0
    for cls, conf, cx, cy, bw, bh in _yolo(small):
        if conf < 0.4 or cls not in K.TARGETS or bw * bh > 0.5 * 960 * 540:
            continue
        if cpx and cpx[0] <= cx <= cpx[0] + cpx[2] and cpx[1] <= cy <= cpx[1] + cpx[3]:
            continue
        s = K.TARGETS[cls] * conf / (1 + 4 * np.hypot((cx - 480) / 960, (cy - 313) / 540))
        if s > best_s:
            best, best_s = [(cx - bw / 2) * 2, (cy - bh / 2) * 2, bw * 2, bh * 2], s
    return [int(v) for v in best] if best else None

def pick_texts(znacka, series, tags, seed):
    pool = TEXTS[znacka]
    key = series if series and series in pool else next((t for t in (tags or []) if t in pool), None)
    opts = list(dict.fromkeys(pool.get(key) or pool[None]))
    random.Random(seed).shuffle(opts)
    return opts[:3]

def cam_for(channel, src):
    cfg = K.load_json(K.STREAMERS, {}).get(channel, {})
    if cfg.get("no_cam"):
        return None
    if cfg.get("cam"):
        return cfg["cam"]
    try:
        return K.get_cam(channel, src)
    except Exception:
        return None

def prepare(source, peak, channel, znacka, out_dir, tags=None, series=None, cam="auto", extra=None):
    """Write <out_dir>/nahled_vstup/{frame.jpg, face.jpg, meta.json}. Returns the meta dict."""
    source = Path(source)
    d = Path(out_dir) / "nahled_vstup"
    d.mkdir(parents=True, exist_ok=True)
    cam = cam_for(channel, source) if cam == "auto" else cam
    dur = K.duration(source)
    peak = peak if peak is not None and peak < dur - 0.1 else dur * 0.6
    full = grab(source, peak)
    if full is None:
        raise RuntimeError(f"no frame from {source}")
    box = character_box(full, cam)
    frame, (x0, y0, s), side = game_frame(full, cam, box)
    if box:                                           # into frame.jpg coordinates; drop if cropped away
        bx, by = (box[0] - x0) * s, (box[1] - y0) * s
        box = [int(bx), int(by), int(box[2] * s), int(box[3] * s)]
        if bx + box[2] < 0 or by + box[3] < 0 or bx > FW or by > FH:
            box = None
    cv2.imwrite(str(d / "frame.jpg"), frame, [cv2.IMWRITE_JPEG_QUALITY, 94])
    face, finfo = best_face(source, peak, cam) if cam else (None, {"found": False, "why": "no facecam box"})
    if face is not None:
        cv2.imwrite(str(d / "face.jpg"), face, [cv2.IMWRITE_JPEG_QUALITY, 94])
    else:
        (d / "face.jpg").unlink(missing_ok=True)
    meta = {
        "znacka": znacka, "brand": BRAND_LOOK[znacka], "serie": series, "channel": channel, "tags": tags or [],
        "texts": pick_texts(znacka, series, tags, f"{source}:{znacka}"),
        "source": str(source), "peak": round(float(peak), 2),
        "frame": "frame.jpg", "frame_size": [FW, FH], "chat_side_cropped": side, "character_box": box,
        "face": "face.jpg" if face is not None else None, "face_info": finfo,
        "size": [1280, 720], **(extra or {}),
    }
    (d / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    K.log(f"  thumbnail inputs -> {d} (face: {finfo.get('found')}, chat side: {side}, character: {bool(box)})")
    return meta

# ---------------------------------------------------------------------------
# prompt for Gemini / ChatGPT (Aleš makes the thumbnail there from attachments 1-3)

EMO_PROMPT = {"funny": "laughing hysterically in disbelief", "crash": "pure shock", "chase": "pure shock",
              "wow": "pure shock", "rage": "screaming in rage", "glitch": "confused disbelief",
              "first-reaction": "jaw-dropping awe"}
SCENE_FALLBACK = {   # no vision model: from the tags
    "crash": ("a car crash in the middle of the street", "the wrecked car flying through the air"),
    "chase": ("a police chase at full speed", "the police car right behind him"),
    "glitch": ("the game glitching: a character stuck in a broken pose", "the glitched character"),
    "funny": ("a ridiculous fail in the middle of the street", "the character mid-fail"),
    "rage": ("everything going wrong at once", "the character in trouble"),
    "first-reaction": ("the first look at the city skyline", "the city skyline"),
    "wow": ("a wild moment out of nowhere", "the main character"),
}
COLOR_NAME = {"bananas": ("banana yellow", "#FFE135"), "fanpage": ("hot pink", "#FF4FD8")}
PROMPT = """Create a YouTube thumbnail, 16:9, 1280x720, in the style of the biggest gaming YouTube channels: bold, ultra-saturated, high contrast, cinematic lighting, razor-sharp.
Image 1 is the streamer. Keep their exact identity (face shape, hair, skin tone, headphones), same gender and age. Make the expression extremely exaggerated: {emotion}, eyes wide, mouth wide open, eyebrows up. Place them large on the {face_side} third, cut out with a thin white outline and a soft {color_name} rim glow, shoulders cropped by the bottom edge.
Image 2 is the game moment: {scene}. Keep the location and the action, but render it as a polished, sun-drenched Vice City-style screenshot: golden-hour sunset, palm trees, neon, wet reflective asphalt, motion blur, sparks and debris. Make {key_object} big and unmistakable in the middle of the picture.
Add ONE thick bright red arrow with a white outline pointing at {key_object}{circle_line}.
Text: "{text}" in huge heavy sans-serif capitals, white with the last word in {color_name} ({hex}), thick black outline, across the top, readable at 168x94 px. No other text, no logos, no UI, no watermarks.
{woman_line}
Layout: the streamer's face big on the {face_side} third, the woman on the {other_side} third, the action and the red arrow in the middle, the text across the top.
Keep it simple: face, woman, action, arrow, text. Nothing else. Do not invent anything that did not happen in image 2."""
# second figure on every thumbnail (Aleš 17. 11.): an invented Vice City woman, never a real person and
# never the brand's model / mascot. Outfit, hair and pose vary per video (seeded), always ad-friendly.
WOMAN_LINE = ("Add a second character: a fictional, attractive, curvy woman in sun-soaked Vice City style ({look}), "
              "standing on the {other_side} side near the action, reacting with a shocked, wide-eyed gasp, {pose}. "
              "Confident and glamorous, tasteful, fully clothed, no suggestive pose, no focus on body parts. "
              "She is an invented character, not a real person and not the streamer.")
WOMAN_LOOKS = [
    ("fitted crop top and high-waisted denim shorts, gold hoops, sunglasses pushed up, long wavy dark hair",
     "one hand on her cheek"),
    ("tight floral summer dress, gold hoops, sunglasses pushed up, high ponytail", "both hands over her mouth"),
    ("white tank top, high-waisted denim shorts, oversized gold chain, big curly hair", "one hand on her cheek"),
    ("pastel pink satin slip dress, gold hoops, sunglasses in her hair, sleek long blonde hair",
     "pointing at the action with her mouth open"),
    ("cropped denim jacket over a fitted top, high-waisted white jeans, gold hoops, braids", "one hand on her head"),
    ("neon-colored fitted bodycon dress, chunky gold bracelets, sunglasses pushed up, short bob haircut",
     "leaning back with one hand on her chest"),
]

def woman_line(seed, other_side):
    import random as _r
    look, pose = _r.Random(f"woman:{seed}").choice(WOMAN_LOOKS)
    return WOMAN_LINE.format(look=look, pose=pose, other_side=other_side)

def brand_ref(znacka, out):
    """3_znacka.png: bananas = the shocked banana keyed out of its greenscreen reaction video (clean, no
    background), else its profile picture; fanpage = its profile picture (the prompt dresses her)."""
    vid = HERE / "assets" / "banan" / "reakce1.mp4"
    if znacka == "bananas" and vid.exists():
        tmp = out.with_suffix(".raw.png")
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", "2.7", "-i", str(vid), "-frames:v", "1",
                        "-vf", f"colorkey={K.BANANA_KEY}:0.28:0.08,format=rgba", str(tmp)], timeout=60)
        img = cv2.imread(str(tmp), cv2.IMREAD_UNCHANGED)
        tmp.unlink(missing_ok=True)
        if img is not None and img.ndim == 3 and img.shape[2] == 4:
            ys, xs = np.where(img[..., 3] > 20)
            if len(xs):
                img = img[max(0, ys.min() - 8): ys.max() + 8, max(0, xs.min() - 8): xs.max() + 8]
                cv2.imwrite(str(out), img)
                return out
    pfp = HERE / "assets" / "brand" / f"pfp_{znacka}.webp"
    img = cv2.imread(str(pfp), cv2.IMREAD_COLOR) if pfp.exists() else None
    if img is None:
        return None
    cv2.imwrite(str(out), img)
    return out

def scene_text(frame_path, tags):
    """(scene sentence, key object, source): Workers AI vision on the frame, else from the tags."""
    try:
        import nahled_ai
        r = nahled_ai.describe_scene(frame_path)
        if r:
            return r[0], r[1], "vision"
    except Exception as e:
        K.log(f"  scene description: {str(e)[-150:]}")
    tag = next((t for t in tags or [] if t in SCENE_FALLBACK), "wow")
    return SCENE_FALLBACK[tag][0], SCENE_FALLBACK[tag][1], "tags"

def build_prompt(znacka, tags, text, scene, key_object, face_side, seed=""):
    other = "left" if face_side == "right" else "right"
    color_name, hx = COLOR_NAME[znacka]
    emotion = next((EMO_PROMPT[t] for t in tags or [] if t in EMO_PROMPT), "pure shock")
    return PROMPT.format(emotion=emotion, face_side=face_side, other_side=other, text_side=other, color_name=color_name,
                         hex=hx, scene=scene.rstrip("."), key_object=key_object, circle_line="", text=text.upper(),
                         woman_line=woman_line(seed, other))

def date_ord(s):
    from datetime import date as _d
    try:
        return _d.fromisoformat(s).toordinal()
    except (TypeError, ValueError):
        return 0

def prepare_long(meta_path):
    """
    Compilation: thumbnail inputs from the best of the top 3 (first one with a face found), a second
    streamer's face if another top-3 streamer has one, the brand reference, the scene description
    and the filled-in prompt for Gemini / ChatGPT. The face side alternates between videos.
    """
    meta_path = Path(meta_path)
    m = json.loads(meta_path.read_text(encoding="utf-8"))
    out = meta_path.with_name(meta_path.name.replace(".meta.json", ""))
    top = sorted(m["moments"], key=lambda x: x["rank"])[:3]
    res, used = None, None
    for mo in top:
        res = prepare(mo["source"], mo.get("peak"), mo["channel"], m["znacka"], out, mo.get("tags"), m.get("serie"),
                      extra={"title": m.get("title"), "moment": mo["id"]})
        used = mo
        if res.get("face"):
            break
    d = out / "nahled_vstup"
    face2 = None
    (d / "face2.jpg").unlink(missing_ok=True)
    for mo in top:                                    # a second streamer's face, if one of the top 3 has one
        if mo["channel"] == used["channel"]:
            continue
        cam = cam_for(mo["channel"], Path(mo["source"]))
        if cam:
            f2, _ = best_face(Path(mo["source"]), mo.get("peak") or 0.0, cam)
            if f2 is not None:
                cv2.imwrite(str(d / "face2.jpg"), f2, [cv2.IMWRITE_JPEG_QUALITY, 94])
                face2 = mo["channel"]
                break
    (d / "znacka.png").unlink(missing_ok=True)      # Aleš 17. 11.: no mascot / brand model on thumbnails
    scene, key, how = scene_text(d / "frame.jpg", used.get("tags"))
    face_side = "right" if (date_ord(m.get("datum")) + (0 if m["znacka"] == "bananas" else 1)) % 2 else "left"
    texts = res["texts"]
    seed = meta_path.name
    other = "left" if face_side == "right" else "right"
    res.update(face2=("face2.jpg" if face2 else None), face2_channel=face2,
               scene=scene, key_object=key, scene_from=how, face_side=face_side, woman=woman_line(seed, other),
               prompt=build_prompt(m["znacka"], used.get("tags"), texts[0], scene, key, face_side, seed))
    (d / "meta.json").write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    m.update(nahled_vstup=str(d), nahled_prompt=res["prompt"], nahled_texts=texts)
    meta_path.write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")
    K.log(f"  prompt: face {face_side}, scene ({how}): {scene} / {key}")
    return res

def main():
    p = argparse.ArgumentParser(description="inputs for the AI thumbnail generator (frame, face, text)")
    sub = p.add_subparsers(dest="cmd", required=True)
    lo = sub.add_parser("long", help="a compilation (.meta.json from kompilace.py)")
    lo.add_argument("meta")
    sh = sub.add_parser("short", help="a rendered short (its folder or meta.json)")
    sh.add_argument("meta")
    sh.add_argument("--znacka", default="bananas", choices=list(BRAND_LOOK))
    mo = sub.add_parser("moment", help="any 16:9 video + payoff time")
    mo.add_argument("--source", required=True)
    mo.add_argument("--peak", type=float)
    mo.add_argument("--channel", required=True)
    mo.add_argument("--znacka", default="bananas", choices=list(BRAND_LOOK))
    mo.add_argument("--tag")
    mo.add_argument("--serie")
    mo.add_argument("--out", required=True, help="folder; nahled_vstup/ is created inside")
    a = p.parse_args()
    if a.cmd == "long":
        prepare_long(a.meta)
    elif a.cmd == "short":
        mp = Path(a.meta)
        mp = mp / "meta.json" if mp.is_dir() else mp
        m = json.loads(mp.read_text(encoding="utf-8"))
        prepare(m.get("source_16x9") or mp.parent / "source.mp4", m.get("peak_src", m.get("peak")), m["channel"],
                a.znacka, mp.parent, m.get("tags"), None, m.get("cam") or "auto")
    else:
        prepare(a.source, a.peak, a.channel.lower(), a.znacka, a.out, [a.tag] if a.tag else None, a.serie)

if __name__ == "__main__":
    sys.exit(main())
