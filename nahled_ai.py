"""
nahled_ai.py - BACKUP AI thumbnail (Cloudflare Workers AI, FLUX.2 [klein] 9B) from nahled_vstup/,
plus the scene description for the Gemini / ChatGPT prompt (describe_scene, Llama 3.2 vision).
Aleš 17. 11.: he makes the real thumbnail himself from the e-mailed prompt + attachments; this one
only rides along as zaloha_nahled_ai.jpg.

Inputs (nahled.py): frame.jpg (payoff, no chat/HUD), face.jpg (the streamer's real expression),
meta.json (brand, series, 2-4 word text options, tags).
  1. FLUX.2 [klein] 9B re-renders the scene as a top-gaming-channel thumbnail with the real
     streamer large, his real expression exaggerated, a clean area for the headline, NO text.
  2. We put the headline on top ourselves (brand font from fonts/, thick outline).
  3. 2-3 variants, the best by a simple score (contrast, colour, face sharpness, text pop).

Budget (free plan: 10 000 neurons/day, resets 00:00 UTC): one 1280x720 image with two small
reference images ~1 460 neurons -> at most CF_MAX_IMAGES (default 6) images per UTC day for both
channels together, counted in nahled_ai_budget.json. A 429 stops all further calls (no retry).
No CF_ACCOUNT_ID / CF_API_TOKEN, or any error -> no thumbnail (YouTube picks a frame).
There is deliberately no fallback to a composited thumbnail (Aleš 17. 11.).

Usage:
  python nahled_ai.py out/_long/bananas/2026-11-19_funny-day/nahled_vstup [--varianty 3] [--zkouska]
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
MODEL = os.environ.get("CF_MODEL", "@cf/black-forest-labs/flux-2-klein-9b")
BUDGET = Path(os.environ.get("NAHLED_AI_BUDGET") or HERE / "nahled_ai_budget.json")   # env: tests keep the repo clean
NEURONS = {"@cf/black-forest-labs/flux-2-klein-9b": 1460, "@cf/black-forest-labs/flux-2-klein-4b": 140}
TW, TH = 1280, 720
FONTS = {"bananas": HERE / "fonts" / "cap" / "montserrat-900.ttf", "fanpage": HERE / "fonts" / "cap" / "anton.ttf"}
CHAR_W = {"bananas": 0.80, "fanpage": 0.47}          # average upper-case advance per font size unit
TEXT2 = {"bananas": "FFE135", "fanpage": "FF4FD8"}   # second line in the brand colour, first white
STYLE = {
    "bananas": "Bright sunny colours, warm golden light, banana-yellow glow, playful and fun.",
    "fanpage": "Night city, neon hot pink and deep purple-black, glossy reflections, sleek and cinematic.",
}
EMOTION = {   # the real expression, exaggerated - never a made-up action
    "funny": "laughing in disbelief", "rage": "furious, shouting", "crash": "shocked, mouth wide open, wide eyes",
    "wow": "shocked, mouth wide open, wide eyes", "glitch": "confused and shocked", "chase": "intense and focused",
    "first-reaction": "amazed, jaw dropped",
}
# where the streamer goes / where the headline goes, per variant
LAYOUTS = [("right", "left"), ("left", "right"), ("right", "top left")]
_stop = False                                          # set after a 429: no more calls this run

def log(msg):
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    enc = sys.stdout.encoding or "utf-8"
    print(line.encode(enc, "replace").decode(enc), flush=True)

def load_env():
    env = HERE / ".env"
    for line in env.read_text(encoding="utf-8").splitlines() if env.exists() else []:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            v = v.split(" #")[0].strip().strip('"')
            if v:
                os.environ.setdefault(k.strip(), v)

def budget_left():
    day = datetime.now(timezone.utc).date().isoformat()
    b = json.loads(BUDGET.read_text(encoding="utf-8")) if BUDGET.exists() else {}
    used = b.get(day, {}).get("images", 0)
    return int(os.environ.get("CF_MAX_IMAGES", "6")) - used, b, day

def spend(n_neurons):
    _, b, day = budget_left()
    b = {k: v for k, v in b.items() if k >= day[:8]}   # keep this month
    e = b.setdefault(day, {"images": 0, "neurons": 0})
    e["images"] += 1
    e["neurons"] += n_neurons
    BUDGET.write_text(json.dumps(b, indent=2), encoding="utf-8")
    log(f"  neurons today ~{e['neurons']} ({e['images']} images)")

def small_jpeg(path, limit=511):
    """Reference images must be smaller than 512x512."""
    img = cv2.imread(str(path))
    s = limit / max(img.shape[:2])
    if s < 1:
        img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])[1].tobytes()

def prompt_for(meta, face_side, text_side, with_frame=True, with_woman=False):
    """
    Plain words on purpose: the model's safety filter flagged prompts naming the game, "real person /
    recognisable" and game frames with falls or weapons (17. 11. tests) - so no game title, no identity talk.
    """
    tags = meta.get("tags") or []
    emo = next((EMOTION[t] for t in tags if t in EMOTION), "surprised, mouth open, wide eyes")
    ref = "1" if with_frame else "0"
    who = (f"The person from image {ref}, big and close, head and shoulders on the {face_side} side, "
           f"same face, same gender and age, same hair, expression: {emo}, sharp focus, bright rim light. " if meta.get("face") else "")
    scene = ("Background: the place from image 0 (a video game city), restyled bright and colourful, simple. "
             if with_frame else "Background: a sunny Miami-style street with palm trees and sports cars, simple. ")
    other = "left" if face_side == "right" else "right"
    woman = (f"On the {other} side a fictional woman in a colourful summer outfit, fully clothed, tasteful, "
             "reacting with a shocked gasp. " if with_woman else "")
    space = "the top band" if with_woman else f"the {text_side} side"
    return ("Eye-catching YouTube gaming thumbnail, 16:9. " + who + woman + scene +
            "Stylized, hyper-saturated, high contrast, clean composition. "
            f"Leave {space} empty and calm for a headline. No text, no letters, no logos. "
            + STYLE[meta["znacka"]])

def multipart(fields, files):
    """multipart/form-data body (stdlib): fields {name: str}, files {name: (filename, bytes)}."""
    bnd = uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out.append(f"--{bnd}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    for k, (fn, data) in files.items():
        out.append(f"--{bnd}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fn}\"\r\n"
                   f"Content-Type: image/jpeg\r\n\r\n".encode() + data + b"\r\n")
    out.append(f"--{bnd}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={bnd}"

def generate(body, ctype):
    """One image from Workers AI -> PNG/JPEG bytes. Raises on any error; a 429 also stops the run."""
    global _stop
    url = f"https://api.cloudflare.com/client/v4/accounts/{os.environ['CF_ACCOUNT_ID']}/ai/run/{MODEL}"
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {os.environ['CF_API_TOKEN']}", "Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            _stop = True
        raise RuntimeError(f"Workers AI {e.code}: {e.read().decode(errors='replace')[:300]}")
    img = (res.get("result") or {}).get("image")
    if not res.get("success", True) or not img:
        raise RuntimeError(f"Workers AI: no image ({str(res.get('errors'))[:200]})")
    return base64.b64decode(img)

def request(meta, frame, face, face_side, text_side, i, with_frame, with_woman=False):
    files = {}
    if with_frame:
        files["input_image_0"] = ("frame.jpg", frame)
    if face:
        files[f"input_image_{len(files)}"] = ("face.jpg", face)
    return multipart({"prompt": prompt_for(meta, face_side, text_side, with_frame, with_woman), "width": TW,
                      "height": TH, "seed": 1000 + i}, files)

def ff_path(p):
    return str(p).replace("\\", "/").replace(":", "\\:")

def split_lines(text):
    w = text.split()
    if len(w) <= 1 or (len(w) == 2 and len(text) <= 9):
        return [text]
    best = min(range(1, len(w)), key=lambda i: abs(len(" ".join(w[:i])) - len(" ".join(w[i:]))))
    return [" ".join(w[:best]), " ".join(w[best:])]

def text_box(text_side):
    if text_side == "left":
        return (40, 60, 560, 420)
    if text_side == "right":
        return (TW - 600, 60, 560, 420)
    return (40, 30, 700, 300)                              # top left

def draw_text(img_path, out_path, text, box, znacka, tmp):
    """Headline in the brand font with a thick outline (ffmpeg drawtext, text from files: no escaping)."""
    lines = split_lines(text)
    x0, y0, bw, bh = box
    size = int(min(bh / (len(lines) * 1.02), bw / (CHAR_W[znacka] * max(len(l) for l in lines))))
    size = max(60, min(size, 200 if len(lines) > 1 else 240))
    lh = int(size * (1.0 if znacka == "fanpage" else 1.08))
    top = y0 + (bh - lh * len(lines)) // 2
    flt = []
    for i, line in enumerate(lines):
        tf = tmp / f"line{i}.txt"
        tf.write_text(line, encoding="utf-8")
        col = TEXT2[znacka] if i == len(lines) - 1 else "FFFFFF"
        flt.append(f"drawtext=fontfile='{ff_path(FONTS[znacka])}':textfile='{ff_path(tf)}':fontsize={size}:"
                   f"fontcolor=0x{col}:borderw={max(7, size // 12)}:bordercolor=black:shadowx=6:shadowy=8:"
                   f"shadowcolor=black@0.85:x={x0}+({bw}-text_w)/2:y={top + i * lh}")
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(img_path),
                    "-vf", f"scale={TW}:{TH}:force_original_aspect_ratio=increase,crop={TW}:{TH}," + ",".join(flt),
                    "-frames:v", "1", "-q:v", "2", str(out_path)], check=True, timeout=60)
    return (x0, top, bw, lh * len(lines))

_cascade = None

def score(path, tbox):
    """Contrast + colourfulness + face sharpness (largest face found) + text pop -> 0..1."""
    global _cascade
    img = cv2.imread(str(path))
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    contrast = min(g.std() / 64, 1.0)
    b, gg, r = [c.astype(np.float32) for c in cv2.split(img)]
    rg, yb = r - gg, 0.5 * (r + gg) - b
    colorful = min((np.sqrt(rg.std() ** 2 + yb.std() ** 2) + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2)) / 90, 1.0)
    if _cascade is None:
        _cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces = _cascade.detectMultiScale(g, 1.1, 5, minSize=(80, 80))
    face = 0.0
    if len(faces):
        x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
        face = min(np.log1p(cv2.Laplacian(g[y: y + h, x: x + w], cv2.CV_64F).var()) / np.log1p(800), 1.0)
        face *= min(1.0, h / 250)                          # a big face beats a small one
    x, y, w, h = tbox
    reg = g[max(0, y): y + h, max(0, x): x + w].astype(np.float32)
    text = min(reg.std() / 70, 1.0) if reg.size else 0.0
    total = 0.3 * contrast + 0.25 * colorful + 0.25 * face + 0.2 * text
    return round(float(total), 3), {"contrast": round(float(contrast), 2), "colorful": round(float(colorful), 2),
                                    "face": round(float(face), 2), "text": round(float(text), 2)}

VISION = "@cf/meta/llama-3.2-11b-vision-instruct"   # ~10 neurons per call (one 640 px image, short answer)

def vision_left():
    """Vision calls left today (CF_MAX_VISION, default 120 ~ 1 200 neurons, shared by topic checks and the scene
    description; the day is Cloudflare's: it resets at 00:00 UTC = 01:00 Prague)."""
    day = datetime.now(timezone.utc).date().isoformat()
    b = json.loads(BUDGET.read_text(encoding="utf-8")) if BUDGET.exists() else {}
    return int(os.environ.get("CF_MAX_VISION", "120")) - b.get(day, {}).get("vision", 0)

def vision_spend():
    day = datetime.now(timezone.utc).date().isoformat()
    b = json.loads(BUDGET.read_text(encoding="utf-8")) if BUDGET.exists() else {}
    e = b.setdefault(day, {"images": 0, "neurons": 0})
    e["vision"] = e.get("vision", 0) + 1
    e["neurons"] = e.get("neurons", 0) + 10
    BUDGET.write_text(json.dumps(b, indent=2), encoding="utf-8")

def vision(jpg, prompt, max_tokens=80):
    """One Workers AI vision call -> the answer text (str) or parsed dict, or None (no keys / budget / error)."""
    load_env()
    if not (os.environ.get("CF_ACCOUNT_ID") and os.environ.get("CF_API_TOKEN")) or os.environ.get("CF_VISION") == "0":
        return None
    if vision_left() <= 0:
        log("  vision: today's budget used up")
        return None
    img = cv2.imread(str(jpg))
    w = min(1024, img.shape[1])
    img = cv2.resize(img, (w, int(img.shape[0] * w / img.shape[1])), interpolation=cv2.INTER_AREA)
    b64 = base64.b64encode(cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 86])[1].tobytes()).decode()
    url = f"https://api.cloudflare.com/client/v4/accounts/{os.environ['CF_ACCOUNT_ID']}/ai/run/{VISION}"
    body = json.dumps({"prompt": prompt, "image": b64, "max_tokens": max_tokens, "temperature": 0.1}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {os.environ['CF_API_TOKEN']}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        log(f"  vision {e.code}: {e.read().decode(errors='replace')[:160]}")
        return None
    except OSError as e:
        log(f"  vision: {e}")
        return None
    vision_spend()
    return (res.get("result") or {}).get("response")

def ask_yes_no(jpg, question):
    """Topic check: True / False, or None when the vision model is unavailable."""
    ans = vision(jpg, "These are frames from a video game stream (top left: 1.5 s before, top right: the moment). "
                      "Ignore chat, webcam and on-screen interface. " + question + " Answer with one word: yes or no.",
                 max_tokens=5)
    if ans is None:
        return None
    a = (json.dumps(ans) if isinstance(ans, dict) else str(ans)).strip().lower()
    return a.startswith("yes") or '"yes"' in a

def ask_gameplay(jpg, topic_question=None):
    """
    One vision call: (is it gameplay, topic yes/no or None). Returns (None, None) when unavailable.
    """
    import kategorie
    q = ("These are four frames from a stream moment (top: 1.5 s before and the moment; bottom: its start and later). "
         "Q1: " + kategorie.GAMEPLAY_Q + (" Q2: " + topic_question if topic_question else "")
         + ' Reply with JSON only: {"q1": "yes" or "no"' + (', "q2": "yes" or "no"' if topic_question else "") + "}")
    ans = vision(jpg, q, max_tokens=20)
    if ans is None:
        return None, None
    txt = (json.dumps(ans) if isinstance(ans, dict) else str(ans)).lower()
    pick = lambda k: (None if f'"{k}"' not in txt else
                      txt.split(f'"{k}"', 1)[1].lstrip(' :"').startswith("yes"))
    gp, tp = pick("q1"), pick("q2") if topic_question else None
    if gp is None:                                     # answer not in JSON: read the first yes / no
        gp = txt.strip().startswith("yes")
    return gp, tp

def describe_scene(frame_path):
    """
    One sentence for the thumbnail prompt: what happens in the game frame + the key object
    (Workers AI vision). Returns (scene, key_object) or None (no keys, error, unusable answer).
    The model wants a one-time licence agreement ("prompt": "agree"): sent automatically once.
    """
    load_env()
    if not (os.environ.get("CF_ACCOUNT_ID") and os.environ.get("CF_API_TOKEN")) or os.environ.get("CF_VISION") == "0":
        return None
    img = cv2.imread(str(frame_path))
    img = cv2.resize(img, (640, int(img.shape[0] * 640 / img.shape[1])), interpolation=cv2.INTER_AREA)
    b64 = base64.b64encode(cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])[1].tobytes()).decode()
    ask = ('This is a frame from a video game stream. Look only at the game, ignore chat, webcam and on-screen '
           'interface. Reply with JSON only: {"scene": "<one short sentence: what is happening in the game>", '
           '"key_object": "<2-6 words: the single thing in the action a viewer must see>"}')
    url = f"https://api.cloudflare.com/client/v4/accounts/{os.environ['CF_ACCOUNT_ID']}/ai/run/{VISION}"

    def call(body):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
            "Authorization": f"Bearer {os.environ['CF_API_TOKEN']}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.load(r)

    for attempt in range(2):
        try:
            res = call({"prompt": ask, "image": b64, "max_tokens": 120, "temperature": 0.2})
            break
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")
            if attempt == 0 and "agree" in msg.lower():
                log("  vision: accepting the model licence (one-time 'agree')")
                try:                                       # the agree call itself may answer 403 ("thank you")
                    call({"prompt": "agree"})
                except urllib.error.HTTPError:
                    pass
                continue
            log(f"  vision {e.code}: {msg[:200]}")
            return None
    resp = (res.get("result") or {}).get("response") or ""
    text = json.dumps(resp) if isinstance(resp, dict) else str(resp).strip()   # the API may parse the JSON itself
    try:
        j = resp if isinstance(resp, dict) else json.loads(text[text.index("{"): text.rindex("}") + 1])
        scene, key = str(j["scene"]).strip().rstrip("."), str(j["key_object"]).strip().rstrip(".")
    except (ValueError, KeyError):
        log(f"  vision: unusable answer {text[:120]!r}")
        return None
    if not scene or not key or len(scene) > 220:
        return None
    key = key if key.lower().startswith(("the ", "a ", "an ")) else f"the {key}"
    return scene[0].lower() + scene[1:], key

def make(vstup, out=None, variants=3, dry=False):
    """
    nahled_vstup/ -> <parent>/nahled.jpg (best) + nahled_ai_v1..N.jpg. Returns the path or None
    (no keys, no budget, any error: the video goes up without a custom thumbnail).
    """
    vstup = Path(vstup)
    meta = json.loads((vstup / "meta.json").read_text(encoding="utf-8"))
    out = Path(out) if out else vstup.parent / "nahled.jpg"
    load_env()
    keys = os.environ.get("CF_ACCOUNT_ID") and os.environ.get("CF_API_TOKEN")
    if not keys and not dry:
        log("  AI thumbnail: no CF_ACCOUNT_ID / CF_API_TOKEN -> no custom thumbnail")
        return None
    left, _, _ = budget_left()
    n = min(variants, len(LAYOUTS), max(left, 0)) if not dry else min(variants, len(LAYOUTS))
    if n <= 0:
        log("  AI thumbnail: today's image budget is used up -> no custom thumbnail")
        return None
    frame = small_jpeg(vstup / meta["frame"])
    face = small_jpeg(vstup / meta["face"]) if meta.get("face") else None
    texts = (meta.get("texts") or ["NO WAY"]) * 3
    tmp = vstup / "_ai_tmp"
    tmp.mkdir(exist_ok=True)
    res = []
    # safety-filter fallbacks (code 3030), kept for the rest of the run: frame + woman -> frame -> face only
    steps = [(True, True), (True, False)] + ([(False, False)] if face else [])
    step = 0
    for i, ((face_side, text_side), text) in enumerate(zip(LAYOUTS[:n], texts), 1):
        if _stop:
            break
        raw = tmp / f"ai_{i}.png"
        if dry:                                            # no call: request on disk, frame as a stand-in image
            body, ctype = request(meta, frame, face, face_side, text_side, i, True)
            files = ["input_image_0"] + (["input_image_1"] if face else [])
            (tmp / f"request_{i}.bin").write_bytes(body)
            log(f"  [zkouska] request {i}: {len(body) / 1024:.0f} kB, {ctype.split(';')[0]}, fields "
                f"{['prompt', 'width', 'height', 'seed'] + files}")
            cv2.imwrite(str(raw), cv2.imread(str(vstup / meta["frame"])))
        else:
            ok = False
            while step < len(steps) and not _stop:
                with_frame, with_woman = steps[step]
                body, ctype = request(meta, frame, face, face_side, text_side, i, with_frame, with_woman)
                try:
                    raw.write_bytes(generate(body, ctype))
                    spend(NEURONS.get(MODEL, 1460))
                    ok = True
                    break
                except Exception as e:
                    log(f"  AI thumbnail {i} failed: {str(e)[:200]}")
                    # the safety filter flags some game frames (falls, weapons, blood) and sometimes people:
                    # next try without the woman, then without the game frame
                    if "3030" in str(e):
                        step += 1
                        if step < len(steps):
                            log(f"  -> safety filter: retry {'without the woman' if steps[step][0] else 'without the game frame'}")
                        continue
                    break
            if not ok:
                continue
        vpath = out.with_name(f"{out.stem}_ai_v{i}.jpg")
        across = not dry and steps[min(step, len(steps) - 1)][1]
        tb = draw_text(raw, vpath, text, (60, 15, TW - 120, 230) if across else text_box(text_side), meta["znacka"], tmp)
        sc, parts = score(vpath, tb)
        res.append((vpath, sc, parts, text, f"face {face_side}, text {text_side}"))
    for f in tmp.glob("*"):
        f.unlink()
    tmp.rmdir()
    if not res:
        return None
    best = max(res, key=lambda r: r[1])
    out.write_bytes(best[0].read_bytes())
    for r in res:
        log(f"  {r[0].name}: {r[1]} {r[2]} '{r[3]}' ({r[4]})" + ("  <- best" if r is best else ""))
    return str(out)

def main():
    p = argparse.ArgumentParser(description="AI thumbnail from nahled_vstup/ (Cloudflare Workers AI)")
    p.add_argument("vstup", help="the nahled_vstup folder (from nahled.py)")
    p.add_argument("--varianty", type=int, default=3)
    p.add_argument("--out", help="default: <parent of nahled_vstup>/nahled.jpg")
    p.add_argument("--zkouska", action="store_true", help="no API call: build the requests, text over the frame")
    a = p.parse_args()
    r = make(a.vstup, a.out, a.varianty, a.zkouska)
    log(f"thumbnail: {r or 'none'}")

if __name__ == "__main__":
    sys.exit(main())


# -----------------------------------------------------------------------------
# Text model (19. 11., after SamurAIGPT/AI-Youtube-Shorts-Generator's virality prompt): the hook question of a
# short from what the streamer really says, and highlight candidates of a YouTube video nobody marked yet.
TEXT = os.environ.get("CF_TEXT_MODEL", "@cf/meta/llama-3.1-8b-instruct-fast")

def text_left():
    """Text calls left today (CF_MAX_TEXT, default 300; tiny prompts, a few neurons each)."""
    day = datetime.now(timezone.utc).date().isoformat()
    b = json.loads(BUDGET.read_text(encoding="utf-8")) if BUDGET.exists() else {}
    return int(os.environ.get("CF_MAX_TEXT", "300")) - b.get(day, {}).get("text", 0)

def text_spend(neurons=5):
    day = datetime.now(timezone.utc).date().isoformat()
    b = json.loads(BUDGET.read_text(encoding="utf-8")) if BUDGET.exists() else {}
    e = b.setdefault(day, {"images": 0, "neurons": 0})
    e["text"] = e.get("text", 0) + 1
    e["neurons"] = e.get("neurons", 0) + neurons
    BUDGET.write_text(json.dumps(b, indent=2), encoding="utf-8")

def ask_text(system, prompt, max_tokens=120, temperature=0.4):
    """One Workers AI text call -> the answer (str), or None (no keys / budget / error). Never raises."""
    load_env()
    if not (os.environ.get("CF_ACCOUNT_ID") and os.environ.get("CF_API_TOKEN")) or os.environ.get("CF_TEXT") == "0":
        return None
    if text_left() <= 0:
        log("  text model: today's budget used up")
        return None
    url = f"https://api.cloudflare.com/client/v4/accounts/{os.environ['CF_ACCOUNT_ID']}/ai/run/{TEXT}"
    body = json.dumps({"messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                       "max_tokens": max_tokens, "temperature": temperature}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {os.environ['CF_API_TOKEN']}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        log(f"  text model {e.code}: {e.read().decode(errors='replace')[:160]}")
        return None
    except OSError as e:
        log(f"  text model: {e}")
        return None
    text_spend(5 + len(prompt) // 800)
    ans = (res.get("result") or {}).get("response")
    return ans if isinstance(ans, str) else (json.dumps(ans) if ans is not None else None)

HOOK_SYSTEM = ("You are an elite short-form video editor who has studied thousands of viral TikTok, Reels and Shorts "
               "clips. You write the on-screen text of the first 2 seconds: one question that makes a viewer stop "
               "scrolling and watch to the end.")

def hook_question(name, tags, transcript):
    """A 3-8 word question in capitals from the streamer's real words + the moment's tags, or None. Only facts from
    the input; validated (length, letters, no swear/sexual word, no number the input does not have)."""
    import re as _re
    words = " ".join(transcript.split()[:80])
    ans = ask_text(HOOK_SYSTEM,
                   f"GTA 6 Twitch clip. Streamer: {name}. Detected moment type: {', '.join(tags) or 'reaction'}.\n"
                   f"What the streamer says: \"{words}\"\n"
                   "Write ONE question, 3 to 8 words, in capital letters, that teases this exact moment. Use only what "
                   "the transcript and the moment type say - never invent events, people, places or numbers. You may "
                   "use the streamer's name. No emoji, no hashtags, no quotes, no swear words. Reply with the question only.",
                   max_tokens=30)
    if not ans:
        return None
    q = ans.strip().splitlines()[0].strip().strip('"\'').upper()
    if not _re.fullmatch(r"[A-Z0-9' ,.!?-]{8,60}\?", q) or not 3 <= len(q.split()) <= 8:
        return None
    nums = set(_re.findall(r"\d+", q))
    if nums and not nums <= set(_re.findall(r"\d+", words + " 6")):
        return None
    from cenzura import cenzura
    if cenzura(q) != q:
        return None
    return q

HIGHLIGHT_SYSTEM = ("You are an elite short-form video editor who has studied thousands of viral clips. From a GTA 6 "
                    "stream transcript you pick the moments a viewer would share: a loud reaction, a crash or chase, a "
                    "funny line, a rage outburst, a shocked 'no way'. Calm talking, menus, reading chat or 'starting soon' "
                    "are never highlights.")

def stream_highlights(lines, max_n=3):
    """lines = ['[mm:ss] text', ...] (one per ~20 s). -> [(seconds, score 0..1)] the model rates >= 80/100.
    Long transcripts in chunks; never raises."""
    import re as _re
    out = []
    for i in range(0, len(lines), 90):                  # ~30 min of transcript per call
        chunk = "\n".join(lines[i:i + 90])
        ans = ask_text(HIGHLIGHT_SYSTEM,
                       "Transcript (timestamps mm:ss or h:mm:ss):\n" + chunk + "\n\nReturn JSON only: "
                       '{"highlights":[{"t":"mm:ss of the peak","score":0-100,"why":"few words"}]} with at most '
                       f"{max_n} highlights, score 80+ only for something truly shareable; none -> empty list.",
                       max_tokens=220, temperature=0.2)
        if not ans:
            continue
        m = _re.search(r"[\[{].*[\]}]", ans, _re.S)      # the model returns the object or just the list
        try:
            data = json.loads(m.group(0)) if m else []
        except ValueError:
            continue
        items = data.get("highlights", []) if isinstance(data, dict) else data if isinstance(data, list) else []
        for it in items:
            if not isinstance(it, dict):
                continue
            try:
                parts = [int(x) for x in str(it.get("t", "")).split(":")]
                sec = sum(p * 60 ** k for k, p in enumerate(reversed(parts)))
                sc = float(it.get("score", 0))
            except (ValueError, TypeError):
                continue
            if sc >= 80:
                out.append((float(sec), sc / 100))
    return sorted(out, key=lambda x: -x[1])[:max_n]
