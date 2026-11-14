"""
klipuj.py - gta6bananaclips: GTA 6 stream moments -> vertical 9:16 shorts, in bulk.

Where the viral moments come from (no guessing, we use signals that already exist):
  Twitch   clips made by viewers, ranked by views (live: by view growth)
  Kick     same, clips made by viewers
  YouTube  what the audience marked: "most replayed" heatmap, timestamps quoted
           in top comments, bursts of the live-chat replay. No signal -> skipped.
All sources are listed in sources.json.

Layout of every short (always vertical, no blurred filler, no extra text):
  top    1080x640   the streamer's facecam - a static box of the stream overlay
  below  captions of what is said (Whisper, word by word, spoken word in banana yellow)
  bottom 1080x1280  gameplay, cropped around the player character / car;
                    the crop follows the character with a lazy, smooth camera
  corner faint @channel watermark. Nothing else.

The facecam box is set once per streamer in streamers.json (percent of the
frame). If it's missing, we find the face automatically and save a guess
marked "auto"; an auto box is re-checked on every clip. No face -> the game
fills the whole frame.

Usage (from this folder):
  .venv/Scripts/python klipuj.py batch                     # every source in sources.json once
  .venv/Scripts/python klipuj.py watch --every 90          # launch day: Twitch + Kick live
  .venv/Scripts/python klipuj.py top kaicenat --n 5        # one Twitch channel
  .venv/Scripts/python klipuj.py one <twitch|kick clip url | youtube url --start 120 --end 155>
  .venv/Scripts/python klipuj.py cam darkviperau <clip url or file>
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import cv2
import numpy as np

BRAND = "gta6bananaclips"
HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
TMP = OUT / "_tmp"                   # audio for peak detection, deleted after use
STATE = HERE / "state.json"          # rendered / failed clip ids
STREAMERS = HERE / "streamers.json"  # facecam box per streamer, in percent
SOURCES = HERE / "sources.json"      # which channels we take moments from
MODEL = HERE / "models" / "yolox_tiny.onnx"
WHISPER_DIR = HERE / "models" / "whisper"
# open fonts shipped in the repo, so Windows and the Linux runner render the same
CAP_FONTS = HERE / "fonts" / "cap"                 # libass looks here for the caption font
CAP_FONT = "Montserrat Black"                      # family name inside fonts/cap/montserrat-900.ttf
CAP_SIZE = 130                                     # big captions (Aleš 13.+14. 11.: "mnohem větší", "větší")
CAP_CHARS = 11                                     # max letters on a line at that size (2 words max)
WM_FONT_FILE = HERE / "fonts" / "montserrat-600.ttf"  # watermarks

W, H = 1080, 1920      # output size
CAM_H = 640            # facecam band on top; gameplay gets the remaining 1280
FPS = 30               # output frame rate
DET_FPS = 5            # how often we look for the character
SW, SH = 1920, 1080    # every source is normalised to this first
WHISPER_MODEL = "medium.en"  # --whisper to change (small.en is faster, medium.en more accurate)
MAX_TRIES = 3          # a clip that fails this many times is skipped for good
YT_CLIP = 35.0         # seconds cut around a YouTube peak

# -----------------------------------------------------------------------------
# small helpers

def run(cmd, timeout=None):
    """Run a command, raise RuntimeError with its stderr tail if it fails or hangs."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{cmd[0]} timed out after {timeout}s")
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed ({p.returncode}): {p.stderr.strip()[-800:]}")
    return p.stdout

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def save_json(path, data):
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)

def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)

def ff_path(p):
    """Path as ffmpeg filter argument on Windows: forward slashes, escaped drive colon."""
    return str(p).replace("\\", "/").replace(":", "\\:")

NORM = f"scale={SW}:{SH}:force_original_aspect_ratio=increase,crop={SW}:{SH},setsar=1"

def frames(src, fps, w, h):
    """Yield (t, BGR frame) from a video, resampled to fps and scaled to w x h."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(src), "-vf", f"fps={fps},{NORM},scale={w}:{h}",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    # stderr off: closing the pipe early (we often read only some frames) makes ffmpeg complain
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    n, size = 0, w * h * 3
    try:
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            yield n / fps, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            n += 1
    finally:
        p.stdout.close()
        p.wait()

def duration(src):
    """Seconds; falls back to the video stream when the container has no duration."""
    for entry in ("format=duration", "stream=duration"):
        out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", entry,
                   "-of", "csv=p=0", str(src)]).split()
        for v in out:
            try:
                return float(v)
            except ValueError:
                pass
    raise RuntimeError(f"cannot read duration of {src}")

def ytdlp_json(url, flat=False, limit=None, timeout=180):
    cmd = ["yt-dlp", "-J"] + (["--flat-playlist"] if flat else []) + (["--playlist-end", str(limit)] if limit else [])
    return json.loads(run(cmd + [url], timeout=timeout))

# -----------------------------------------------------------------------------
# 1) find moments: every source returns clip dicts
#    {id, url, title, views, duration, created, channel, platform, section?}

def entries_to_clips(entries, channel, platform):
    clips = []
    for e in entries or []:
        if not e.get("url"):
            continue
        clips.append({
            "id": f"{platform}_{e.get('id')}",
            "url": e["url"],
            "title": (e.get("title") or "").strip(),
            "views": int(e.get("view_count") or 0),
            "duration": float(e.get("duration") or 0),
            "created": int(e.get("timestamp") or 0),
            "channel": channel.lower(),
            "platform": platform,
        })
    clips.sort(key=lambda c: c["views"], reverse=True)
    return clips

def list_twitch(channel, rng="24hr", limit=100):
    """Twitch lists clips by views, so a fresh clip can sit low - hence the generous limit."""
    url = f"https://www.twitch.tv/{channel}/clips?filter=clips&range={rng}"
    return entries_to_clips(ytdlp_json(url, flat=True, limit=limit).get("entries"), channel, "twitch")

def list_kick(channel, limit=100):
    return entries_to_clips(ytdlp_json(f"https://kick.com/{channel}/clips", flat=True, limit=limit).get("entries"),
                            channel, "kick")

# A YouTube moment counts as viral only when the AUDIENCE marked it:
#   heatmap   "most replayed" curve (YouTube shows it once a video has enough views)
#   comments  viewers quote timestamps of the best moments ("3:12:45 LMAO"), weighted by likes
#   chat      live-chat replay of a past stream: sudden burst of messages / laughter
# No audience signal -> the video is skipped. Each candidate is (centre of the clip, score 0..1).

TS_RE = re.compile(r"(?<![\d:])(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?![\d:])")
LAUGH_RE = re.compile(r"lmao|lmfao|\blol\b|kekw|omegalul|icant|💀|😂|🤣|no way|\bw\b|\?\?\?|holy", re.I)

def norm(cands):
    m = max((s for _, s in cands), default=0)
    return [(t, s / m) for t, s in cands] if m > 0 else []

def comment_peaks(url, dur):
    """Timestamps viewers wrote in the top comments, summed per 20 s, weighted by likes."""
    try:
        info = json.loads(run(["yt-dlp", "-J", "--skip-download", "--get-comments", "--extractor-args",
                               "youtube:max_comments=400,400,0,0;comment_sort=top", url], timeout=300))
    except RuntimeError as e:
        log(f"  comments: {str(e)[-100:]}")
        return []
    bins = {}
    for c in info.get("comments") or []:
        for h, m, s in TS_RE.findall(c.get("text") or ""):
            t = int(h or 0) * 3600 + int(m) * 60 + int(s)
            if 0 < t < dur:
                bins[t // 20] = bins.get(t // 20, 0) + 1 + np.log1p(c.get("like_count") or 0)
    # a timestamp marks where the moment starts: centre the clip a bit after it
    return [(b * 20 + 10 + YT_CLIP * 0.3, s) for b, s in bins.items() if s >= 2]

def chat_peaks(url, vid):
    """Live-chat replay of a past stream: messages per 10 s (laughs count double) vs the 5 min median."""
    TMP.mkdir(parents=True, exist_ok=True)
    base = TMP / vid
    chat = TMP / f"{vid}.live_chat.json"
    try:
        run(["yt-dlp", "-q", "--skip-download", "--write-subs", "--sub-langs", "live_chat", "-o", str(base), url], timeout=900)
        if not chat.exists():
            return []                                     # this stream has no chat replay
        counts = {}
        for line in chat.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                act = json.loads(line)["replayChatItemAction"]
                t = int(act["videoOffsetTimeMsec"]) / 1000
                item = act["actions"][0]["addChatItemAction"]["item"]["liveChatTextMessageRenderer"]
                text = "".join(r.get("text") or r.get("emoji", {}).get("shortcuts", [""])[0] for r in item["message"]["runs"])
            except (KeyError, IndexError, ValueError, TypeError):
                continue
            counts[int(t // 10)] = counts.get(int(t // 10), 0) + (2 if LAUGH_RE.search(text) else 1)
    except (RuntimeError, FileNotFoundError) as e:
        log(f"  chat: {str(e)[-100:]}")
        return []
    finally:
        chat.unlink(missing_ok=True)
    if len(counts) < 30:
        return []
    n = max(counts) + 1
    c = np.array([counts.get(i, 0) for i in range(n)], dtype=float)
    med = np.array([np.median(c[max(0, i - 15): i + 15]) for i in range(n)])
    mad = np.array([np.median(np.abs(c[max(0, i - 15): i + 15] - med[i])) for i in range(n)]) + 1
    z = (c - med) / mad
    # chat reacts after the moment: centre the clip a bit before the burst
    return [(i * 10 + 5 - YT_CLIP * 0.25, float(z[i])) for i in range(n) if z[i] >= 4]

def viewer_signals(url, vid, info):
    dur = float(info.get("duration") or 0)
    hm = info.get("heatmap") or []
    if hm:
        return norm([((b["start_time"] + b["end_time"]) / 2, b["value"]) for b in hm if b["value"] >= 0.5]), "heatmap"
    cands, how = norm(comment_peaks(url, dur)), ["comments"]
    if info.get("was_live") or info.get("live_status") == "was_live":
        cands += norm(chat_peaks(url, vid))
        how.append("chat")
    return cands, "+".join(how)

def pick_windows(cands, dur, k, clip_len=YT_CLIP, skip_start=15):
    """Top k clip centres, at least 1.5 clip lengths apart, away from intro/outro."""
    out = []
    for t, s in sorted(cands, key=lambda c: -c[1]):
        if t < skip_start or t > dur - 20 or any(abs(t - u) < clip_len * 1.5 for u, _ in out):
            continue
        out.append((t, s))
        if len(out) == k:
            break
    return [(max(0.0, t - clip_len / 2), min(dur, t + clip_len / 2), s) for t, s in out]

def youtube_clips(handle, days, per_video, max_videos=4):
    """Recent videos and streams of a YouTube channel -> its peak moments."""
    clips, now = [], time.time()
    for tab in ("videos", "streams"):
        try:
            entries = ytdlp_json(f"https://www.youtube.com/{handle}/{tab}", flat=True, limit=max_videos).get("entries") or []
        except RuntimeError as e:
            log(f"  {handle}/{tab}: {str(e)[-120:]}")
            continue
        for e in entries:
            vid = e.get("id")
            if not vid or e.get("live_status") == "is_live":
                continue
            url = f"https://www.youtube.com/watch?v={vid}"
            try:
                info = ytdlp_json(url, timeout=120)
            except RuntimeError as err:
                log(f"  {vid}: {str(err)[-120:]}")
                continue
            if info.get("live_status") == "is_live" or now - (info.get("timestamp") or now) > days * 86400:
                continue
            about = " ".join([info.get("title") or ""] + (info.get("tags") or []))
            if not re.search(game_re(), about, re.I):
                continue                                  # not a GTA video
            dur = float(info.get("duration") or 0)
            if dur < 60:
                continue                                  # Shorts: already somebody's short
            cands, how = viewer_signals(url, vid, info)
            if not cands:
                log(f"  {handle}: {vid} no audience signal yet ({how}), skipped")
                continue
            # a stream's first minutes are "starting soon" hype in chat, not a moment
            was_live = info.get("was_live") or info.get("live_status") == "was_live"
            for s, e2, score in pick_windows(cands, dur, per_video, skip_start=150 if was_live else 15):
                clips.append({
                    "id": f"youtube_{vid}_{int(s)}", "url": url, "title": info.get("title") or "",
                    "views": int(info.get("view_count") or 0), "duration": e2 - s,
                    "created": int(info.get("timestamp") or 0),
                    "channel": (info.get("uploader_id") or handle).lstrip("@").lower(),
                    "platform": "youtube", "section": [round(s, 1), round(e2, 1)], "signal": how,
                    "category": about,
                })
            log(f"  {handle}: {vid} -> {how} peaks")
    return clips

# -----------------------------------------------------------------------------
# 2) facecam: a static box of the overlay, found once per streamer

_cascade = None

def faces_on(src, n, w=960, h=540):
    """Haar face boxes (x, y, w, h in w x h pixels) on ~n frames spread over the clip."""
    global _cascade
    if _cascade is None:
        _cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    out = []
    for _, img in frames(src, n / max(duration(src), 1.0), w, h):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        out.append(_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30)))
    return out

def detect_cam(src):
    """
    Guess the facecam box. The webcam is a static part of the overlay, so the
    real face sits at (almost) the same spot on most frames, while false hits
    in the game jump around. Keep the location that repeats most, take its
    median box and grow it into a box with the aspect of the top band.
    Returns [x, y, w, h] in percent of the frame, or None.
    """
    per_frame = faces_on(src, 16)
    hits = [(i, f) for i, found in enumerate(per_frame) for f in found]
    best = []
    for _, (x, y, w, h) in hits:                  # each hit proposes a location; count frames that agree
        cx, cy = x + w / 2, y + h / 2
        agree = {i: f for i, f in hits if abs(f[0] + f[2] / 2 - cx) < 25 and abs(f[1] + f[3] / 2 - cy) < 25}
        if len(agree) > len(best):
            best = list(agree.values())
    if len(best) < max(3, len(per_frame) // 3):   # not stable enough to be a webcam
        return None
    fx, fy, fw, fh = (float(v) for v in np.median(np.array(best), axis=0))
    bw = min(fw * 3.0, 960)                       # a webcam shot is ~3 face-widths wide
    if bw > 0.4 * 960:                            # that's a full-screen camera, not an overlay
        return None
    bh = bw * CAM_H / W                           # keep the top band's aspect (1080:640)
    cx, cy = fx + fw / 2, fy + fh * 0.65
    x = float(np.clip(cx - bw / 2, 0, 960 - bw))
    y = float(np.clip(cy - bh / 2, 0, 540 - bh))
    return [round(x / 9.6, 2), round(y / 5.4, 2), round(bw / 9.6, 2), round(bh / 5.4, 2)]

def face_in_box(src, cam):
    """Is there a face inside the cam box on at least a third of ~8 frames?"""
    bx, by, bw, bh = cam[0] * 9.6, cam[1] * 5.4, cam[2] * 9.6, cam[3] * 5.4
    per_frame = faces_on(src, 8)
    ok = sum(any(bx <= x + w / 2 <= bx + bw and by <= y + h / 2 <= by + bh for x, y, w, h in found)
             for found in per_frame)
    return ok >= max(2, len(per_frame) // 3)

def get_cam(channel, src):
    """
    Facecam box for this clip. A hand-set box is trusted. An auto-detected box
    is only used when a face is really inside it on this clip (just chatting,
    cutscene or a different overlay -> no cam band, game fills the frame).
    """
    db = load_json(STREAMERS, {})
    cfg = db.get(channel, {})
    if cfg.get("no_cam"):
        return None
    if cfg.get("cam") and not cfg.get("auto"):
        return cfg["cam"]
    if cfg.get("cam") and face_in_box(src, cfg["cam"]):
        return cfg["cam"]
    # no box yet, or the auto box has no face on this clip: detect again (a wrong first guess
    # - a cutscene face, an overlay avatar - must not stick forever)
    cam = detect_cam(src)
    if cam:
        db = load_json(STREAMERS, {})
        db[channel] = {"cam": cam, "auto": True}
        save_json(STREAMERS, db)
        log(f"  facecam auto-detected for {channel}: {cam} (check it, then remove 'auto')")
    return cam

# -----------------------------------------------------------------------------
# 3) the character: YOLOX-tiny detections -> a smooth crop path

# COCO classes that can be "the player": person, bicycle, car, motorcycle, airplane, bus, truck, boat
TARGETS = {0: 1.6, 1: 1.0, 2: 1.2, 3: 1.2, 4: 1.0, 5: 1.0, 7: 1.0, 8: 1.0}

SLUR_RE = re.compile(r"\bn[i1]gg(a|er)s?\b|\bf[a@]gg?(ot)?s?\b|\bretards?\b|\btr[a@]nn(y|ies)\b", re.I)

def pick_quote(words):
    """The line for the post text: a short exclamation/question if there is one, else the first full sentence."""
    text = " ".join(t for _, _, t in words)
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if 3 <= len(s.split()) <= 14]
    sents.sort(key=lambda s: (not s.endswith(("!", "?")), 0))
    q = sents[0] if sents else " ".join(text.split()[:10])
    return q[:90]

class NotGameplay(Exception):
    """The clip is in the GTA category but shows no game (e.g. full-screen webcam talk)."""

def talking_head(src):
    """
    Full-screen webcam instead of game: one person (the streamer) fills a big part of the
    picture on most frames. Measured on real clips: webcam 100 % of frames over 18 % of the
    area, GTA gameplay at most 7 % (even with a big facecam overlay the person stays small).
    """
    global _yolox
    if _yolox is None:
        _yolox = Yolox(MODEL)
    n = big = 0
    for _, img in frames(src, 2, 960, 540):
        people = [d for d in _yolox(img) if d[0] == 0]
        n += 1
        big += max((d[4] * d[5] / (960 * 540) for d in people), default=0) > 0.18
    return n > 0 and big / n >= 0.6

class Yolox:
    """Minimal YOLOX ONNX runner (official 0.1.1rc0 export: BGR 0-255, pad 114, raw grid output)."""

    def __init__(self, path, size=416):
        import onnxruntime as ort
        self.sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        self.inp = self.sess.get_inputs()[0].name
        self.size = size
        grids, strides = [], []
        for s in (8, 16, 32):
            n = size // s
            yv, xv = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")
            grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
            strides.append(np.full((n * n, 1), s))
        self.grids = np.concatenate(grids).astype(np.float32)
        self.strides = np.concatenate(strides).astype(np.float32)

    def __call__(self, img, conf_th=0.3):
        s = self.size
        h, w = img.shape[:2]
        r = min(s / h, s / w)
        small = cv2.resize(img, (int(w * r), int(h * r)), interpolation=cv2.INTER_LINEAR)
        pad = np.full((s, s, 3), 114, np.uint8)
        pad[: small.shape[0], : small.shape[1]] = small
        out = self.sess.run(None, {self.inp: pad.transpose(2, 0, 1)[None].astype(np.float32)})[0][0]
        xy = (out[:, :2] + self.grids) * self.strides / r
        wh = np.exp(out[:, 2:4]) * self.strides / r
        scores = out[:, 4:5] * out[:, 5:]
        cls = scores.argmax(1)
        conf = scores[np.arange(len(cls)), cls]
        keep = np.where((conf > conf_th) & np.isin(cls, list(TARGETS)))[0]
        if not len(keep):
            return []
        boxes = [[float(xy[i, 0] - wh[i, 0] / 2), float(xy[i, 1] - wh[i, 1] / 2), float(wh[i, 0]), float(wh[i, 1])] for i in keep]
        idx = cv2.dnn.NMSBoxes(boxes, [float(conf[i]) for i in keep], conf_th, 0.45)
        res = []
        for j in np.array(idx).flatten():
            x, y, bw, bh = boxes[j]
            res.append((int(cls[keep[j]]), float(conf[keep[j]]), x + bw / 2, y + bh / 2, bw, bh))
        return res

_yolox = None

def pick_target(dets, cam_px, prev, w, h):
    """
    The player in GTA's third-person camera sits near the screen centre, a bit
    low, and moves continuously. Score every detection by class, confidence,
    closeness to that spot and closeness to where the player was a moment ago.
    Detections inside the facecam box (the streamer's own face!) are ignored.
    """
    best, best_s = None, 0.0
    for cls, conf, cx, cy, bw, bh in dets:
        if cam_px and cam_px[0] <= cx <= cam_px[0] + cam_px[2] and cam_px[1] <= cy <= cam_px[1] + cam_px[3]:
            continue
        if bw * bh > 0.6 * w * h:          # the whole screen is not a character
            continue
        if abs(cx - w / 2) > 0.32 * w:     # GTA's camera keeps the player near the middle: edge = NPC
            continue
        d_centre = np.hypot((cx - w / 2) / w, (cy - h * 0.58) / h)
        s = TARGETS[cls] * conf / (1 + 4 * d_centre)
        if prev is not None and abs(cx - prev) < 0.12 * w:
            s *= 1.6                         # continuity: stick with the same character
        if s > best_s:
            best, best_s = cx, s
    return best if best_s > 0.12 else None

def track_path(src, cam, crop_w, dur):
    """Per-output-frame crop x (left edge, source pixels) that follows the player."""
    global _yolox
    if _yolox is None:
        _yolox = Yolox(MODEL)
    dw, dh = 960, 540                         # detect on half resolution (fast enough on CPU)
    cam_px = [cam[0] / 100 * dw, cam[1] / 100 * dh, cam[2] / 100 * dw, cam[3] / 100 * dh] if cam else None
    ts, xs, prev = [], [], None
    for t, img in frames(src, DET_FPS, dw, dh):
        cx = pick_target(_yolox(img), cam_px, prev, dw, dh)
        if cx is not None:
            prev = cx
        ts.append(t)
        xs.append(np.nan if cx is None else cx * SW / dw)
    n_out = max(int(dur * FPS), 1)
    raw = np.array(xs, dtype=np.float64)
    found = ~np.isnan(raw)
    log(f"  tracking: player found on {found.sum()}/{len(raw)} samples")
    if not found.any():
        target = np.full(n_out, SW / 2)       # GTA's camera already centres the player
    else:
        target = np.interp(np.arange(n_out) / FPS, np.array(ts)[found], raw[found])

    # lazy camera: ignore small moves (dead zone), follow big ones with capped speed
    dead, k, vmax = crop_w * 0.12, 0.10, 28.0
    cur, path = target[0], np.empty(n_out)
    for i, tx in enumerate(target):
        d = tx - cur
        if abs(d) > dead:
            cur += float(np.clip((d - np.sign(d) * dead) * k, -vmax, vmax))
        path[i] = cur
    path = np.convolve(np.pad(path, 7, mode="edge"), np.ones(15) / 15, mode="valid")  # 0.5 s smoothing

    # keep the facecam overlay out of the game crop (it is already on top): a cam on the
    # left pushes the crop's left edge past it, a cam on the right caps the right edge
    lo, hi = 0, SW - crop_w
    if cam:
        margin = 0.03 * SW
        cam_l, cam_r = cam[0] / 100 * SW, (cam[0] + cam[2]) / 100 * SW
        if cam_r < SW / 2:
            lo = min(cam_r + margin, hi)
        elif cam_l > SW / 2:
            hi = max(cam_l - margin - crop_w, lo)
    return np.clip(path - crop_w / 2, lo, hi).round().astype(int)

# -----------------------------------------------------------------------------
# 4) captions: Whisper word timestamps -> ASS, short chunks right under the facecam,
#    the spoken word turns banana yellow and pops

_whisper = None

def transcribe(src):
    global _whisper
    import whisper
    if _whisper is None:
        WHISPER_DIR.mkdir(parents=True, exist_ok=True)
        _whisper = whisper.load_model(WHISPER_MODEL, download_root=str(WHISPER_DIR))
    res = _whisper.transcribe(str(src), word_timestamps=True, condition_on_previous_text=False,
                              fp16=_whisper.device.type == "cuda", verbose=None)
    return [(w["start"], w["end"], w["word"].strip()) for seg in res["segments"] for w in seg.get("words", [])
            if w["word"].strip()]

def ass_time(t):
    t = max(t, 0.0)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"

def captions_ass(words, path, y):
    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
        "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        # no outline: a soft drop shadow only (Aleš: no thick outlined captions)
        f"Style: Cap,{CAP_FONT},{CAP_SIZE},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,1,0,1,0,5,5,40,40,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    clean = lambda s: s.replace("\\", "").replace("{", "(").replace("}", ")").upper()
    chunks, cur = [], []
    for wd in words:                      # break on 2 words / CAP_CHARS letters, a pause, or sentence end
        if cur and (len(cur) == 2 or sum(len(x[2]) + 1 for x in cur) + len(wd[2]) > CAP_CHARS or wd[0] - cur[-1][1] > 0.6):
            chunks.append(cur)
            cur = []
        cur.append(wd)
        if wd[2][-1:] in ".?!":
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    hi = "{\\c&H35E1FF&}"                 # spoken word in banana yellow #FFE135
    lines = []
    for ci, ch in enumerate(chunks):
        chunk_end = chunks[ci + 1][0][0] if ci + 1 < len(chunks) else ch[-1][1] + 0.4
        chunk_end = min(chunk_end, ch[-1][1] + 0.6)
        for i, (s, e, _) in enumerate(ch):
            end = ch[i + 1][0] if i + 1 < len(ch) else chunk_end
            text = " ".join((hi + clean(t) + "{\\r}") if j == i else clean(t) for j, (_, _, t) in enumerate(ch))
            # one very long word would run off the screen: shrink just that line
            n = len(" ".join(t for _, _, t in ch))
            fs = f"\\fs{int(CAP_SIZE * CAP_CHARS / n)}" if n > CAP_CHARS else ""
            text = text.replace("{\\r}", "{\\r" + fs + "}") if fs else text
            lines.append(f"Dialogue: 0,{ass_time(s)},{ass_time(max(end, s + 0.05))},Cap,,0,0,0,,{{\\blur1.5{fs}\\pos(540,{y})}}{text}")
    path.write_text(head + "\n".join(lines) + "\n", encoding="utf-8")
    return len(chunks)

# -----------------------------------------------------------------------------
# 5) render one clip to 9:16

def build_filter(cam, cmds_file, x0, crop_w, game_h, ass_file=None, mark_file=None):
    """Filter graph ending in [v]. Without ass/mark it returns the clean 9:16 base."""
    f = f"[0:v]fps={FPS},{NORM},split[a][b];"
    f += f"[b]sendcmd=f='{cmds_file}',crop@g={crop_w}:{SH}:{x0}:0,scale={W}:{game_h}[game];"
    if cam:
        x, y, w, h = [round(v / 100 * d) for v, d in zip(cam, (SW, SH, SW, SH))]
        f += f"[a]crop={w}:{h}:{x}:{y},scale={W}:{CAM_H}:force_original_aspect_ratio=increase,crop={W}:{CAM_H}[cam];"
        f += "[cam][game]vstack=inputs=2[base];"
    else:
        f += "[a]nullsink;[game]null[base];"
    if not mark_file:
        return f + "[base]null[v]"
    chain = "[base]"
    if ass_file:
        chain += f"subtitles=filename='{ass_file}':fontsdir='{ff_path(CAP_FONTS)}',"
    # two quiet watermarks:
    #   ours drifts slowly over the gameplay band, bouncing off its edges (DVD-logo style)
    #   the streamer's sits still at the bottom centre, above the TikTok/Reels caption bar
    wm = f"fontfile='{ff_path(WM_FONT_FILE)}':expansion=none:fontsize=30:fontcolor=white@0.6:shadowcolor=black@0.45:shadowx=1:shadowy=1"
    top = (CAM_H if cam else 0) + 230                 # below the captions
    span_y = H - 400 - top                            # down to just above the streamer mark
    bounce_x = "30+abs(mod(t*55\\,2*(w-text_w-60))-(w-text_w-60))"
    bounce_y = f"{top}+abs(mod(t*35\\,{2 * span_y})-{span_y})"
    chain += (f"drawtext={wm}:text='{BRAND}':x='{bounce_x}':y='{bounce_y}',"
              f"drawtext={wm}:textfile='{mark_file}':x=(w-text_w)/2:y=h-320[v]")
    return f + chain

# -----------------------------------------------------------------------------
# our banana: a small whole-figure reaction 1-2x per clip (our own element, so the short is
# not a bare re-upload). Two greenscreen videos with 5 reactions each.

BANANA = HERE / "assets" / "banan"
BANANA_KEY = "0x1BD02A"            # the greenscreen colour
REACTIONS = {                       # name: (video, start s, end s)
    "laugh": (1, 0.5, 2.0), "shock": (1, 2.0, 3.5), "hype": (1, 3.5, 5.0),
    "facepalm": (1, 5.0, 6.5), "mindblown": (1, 6.5, 8.0),
    "rage": (2, 0.5, 2.0), "sideeye": (2, 2.0, 3.6), "crylaugh": (2, 4.0, 5.0),
    "clap": (2, 5.0, 6.5), "point": (2, 6.5, 8.0),
}
TRIGGERS = [                        # what was said -> fitting reactions
    (r"\b(lmao|lmfao|lol|ha(ha)+|i'?m dead|crying)\b", ["laugh", "crylaugh"]),
    (r"\b(no way|what|wait|huh|how|oh my god|omg)\b", ["shock", "mindblown"]),
    (r"\b(let'?s go|yes+|clutch|easy|gg|w)\b", ["hype", "point"]),
    (r"\b(fuck(ing)?|shit|bullshit|are you serious)\b", ["rage", "facepalm"]),
    (r"\b(bruh|why|cringe|dude)\b", ["facepalm", "sideeye"]),
]
BANANA_W, BANANA_H, BANANA_X = 200, 356, 30   # whole banana, small; left side (TikTok's buttons are on the right)

def pick_reactions(words, dur, seed):
    """
    As few as possible: one reaction, a second only in clips over 30 s. It lands right
    after the line that triggered it; with no trigger, one calm reaction at 2/3 of the clip.
    """
    rnd = random.Random(seed)
    out = []
    for s, e, w in words:
        if e < 1.5 or (out and e - out[-1][0] < 8):
            continue
        for rx, names in TRIGGERS:
            if re.search(rx, w.lower().strip(".,!?\"'")) or re.search(rx, w.lower()):
                t = e + 0.15
                if t + 1.6 < dur:
                    out.append((t, rnd.choice(names)))
                break
        if len(out) >= (2 if dur > 30 else 1):
            break
    if not out and dur > 6:
        out.append((dur * 0.66, rnd.choice(["clap", "sideeye", "point"])))
    return out

def reaction_inputs(reactions):
    """ffmpeg inputs: each reaction is its own trimmed, time-shifted input."""
    args = []
    for t, name in reactions:
        v, s, e = REACTIONS[name]
        args += ["-ss", f"{s}", "-t", f"{e - s}", "-i", str(BANANA / f"reakce{v}.mp4")]
    return args

def reaction_overlay(f, reactions, cam):
    """Append overlays of the keyed banana (whole figure, full animation, no fades) to a graph ending in [v]."""
    if not reactions:
        return f
    f = f[: -len("[v]")] + "[r_in]"
    y = (CAM_H if cam else 0) + 520                  # left side of the game band, above TikTok's caption bar
    prev = "r_in"
    for i, (t, name) in enumerate(reactions):
        # setpts moves the reaction to its moment (-itsoffset does not work together with -ss)
        f += (f";[{i + 1}:v]setpts=PTS-STARTPTS+{t:.2f}/TB,"
              f"chromakey={BANANA_KEY}:0.16:0.06,scale={BANANA_W}:{BANANA_H},format=yuva420p[ban{i}];"
              f"[{prev}][ban{i}]overlay={BANANA_X}:{y}:eof_action=pass[o{i}]")
        prev = f"o{i}"
    return f + f";[{prev}]null[v]"

def encode(cmd_base, out_path):
    """Try NVIDIA NVENC first, fall back to CPU x264."""
    # capped at 6 Mb/s: plenty for phones, and every MB is fetched 5x from Netlify by Buffer
    cap = ["-maxrate", "6M", "-bufsize", "12M"]
    for venc in (["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "23", "-b:v", "0"] + cap,
                 ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22"] + cap):
        try:
            run(cmd_base + venc + ["-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out_path)], timeout=900)
            return venc[1]
        except RuntimeError as e:
            log(f"  encoder {venc[1]} failed ({str(e)[-200:]})")
    raise RuntimeError("all encoders failed")

def download(clip, dst):
    cmd = ["yt-dlp", "-q", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--merge-output-format", "mp4", "-o", str(dst)]
    if clip.get("section"):
        s, e = clip["section"]
        cmd += ["--download-sections", f"*{s}-{e}", "--force-keyframes-at-cuts"]
    run(cmd + [clip["url"]], timeout=600)

def home_url(clip):
    return {"twitch": f"twitch.tv/{clip['channel']}", "kick": f"kick.com/{clip['channel']}",
            "youtube": f"youtube.com/@{clip['channel']}"}.get(clip.get("platform"), clip["url"])

def render(clip, src=None, clean=False):
    day = datetime.fromtimestamp(clip["created"] or time.time(), tz=timezone.utc).strftime("%Y%m%d")
    d = OUT / clip["channel"] / f"{day}_{clip['id']}"
    d.mkdir(parents=True, exist_ok=True)
    if src is None:
        src = d / "source.mp4"
        if not src.exists():
            log(f"  download {clip['url']} {clip.get('section') or ''}")
            download(clip, src)

    dur = duration(src)
    if talking_head(src):                     # check first: a full-screen webcam also looks like a "facecam"
        raise NotGameplay("streamer's face fills the frame, no gameplay")
    cam = get_cam(clip["channel"], src)
    game_h = H - CAM_H if cam else H
    crop_w = round(W * SH / game_h)           # source width that fills the game band at full height
    xs = track_path(src, cam, crop_w, dur)
    cmds = d / "track.cmd"
    cmds.write_text("".join(f"{i / FPS:.4f} crop@g x {x};\n" for i, x in enumerate(xs)), encoding="utf-8")

    words = transcribe(src)
    speech = " ".join(t for _, _, t in words)
    if SLUR_RE.search(speech):
        raise NotGameplay("slur in the speech - not worth a strike or a ban")
    ass = d / "captions.ass"
    n_chunks = captions_ass(words, ass, y=CAM_H if cam else 600)   # on the seam between face and game
    log(f"  captions: {len(words)} words, {n_chunks} chunks")
    (d / "transcript.txt").write_text(" ".join(t for _, _, t in words), encoding="utf-8")
    (d / "mark.txt").write_text(f"@{clip['channel']}", encoding="utf-8")

    ff = ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)]
    args = (cam, ff_path(cmds), int(xs[0]), crop_w, game_h)
    out = d / "short_9x16.mp4"
    reactions = pick_reactions(words, dur, clip["id"]) if (BANANA / "reakce1.mp4").exists() else []
    flt = reaction_overlay(build_filter(*args, ff_path(ass) if words else None, ff_path(d / "mark.txt")), reactions, cam)
    enc = encode(ff + reaction_inputs(reactions) + ["-filter_complex", flt, "-map", "[v]", "-map", "0:a?"], out)
    log(f"  banana: {', '.join(f'{n} @{t:.1f}s' for t, n in reactions) or 'none'}")
    base = d / "base_9x16.mp4"               # clean, no captions: input for the editor (OpenMotion)
    if clean:
        encode(ff + ["-filter_complex", build_filter(*args), "-map", "[v]", "-map", "0:a?"], base)

    meta = {
        **clip,
        "cam": cam,
        "layout": "cam+game" if cam else "game",
        "encoder": enc,
        "short": str(out),
        "base": str(base) if clean else None,
        "source": str(src),
        "quote": pick_quote(words),          # publikuj.py builds a different post text per platform from it
        "caption": f"🎮 {home_url(clip)}\n#gta6 #{BRAND}",
        "rendered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "checklist": [
            "titulky sedí na řeč (jména, slang)",
            "bez hudby z rádia ve hře (Content ID)",
            "bez spoilerů: konec hry, velké cutscény",
        ],
    }
    save_json(d / "meta.json", meta)
    log(f"  done -> {out} ({meta['layout']}, {enc})")
    return meta

# -----------------------------------------------------------------------------
# 6) modes

def mark(clip_id, ok):
    """Record a finished clip, or count a failure (re-read the file: another process may write too)."""
    state = load_json(STATE, {"done": {}, "failed": {}})
    state.setdefault("failed", {})
    if ok:
        state["done"][clip_id] = int(time.time())
        state["failed"].pop(clip_id, None)
    else:
        state["failed"][clip_id] = state["failed"].get(clip_id, 0) + 1
    save_json(STATE, state)

def skip(clip_id):
    state = load_json(STATE, {"done": {}, "failed": {}})
    return (clip_id in state["done"] or clip_id in state.get("other_game", {})
            or state.get("failed", {}).get(clip_id, 0) >= MAX_TRIES)

def game_re():
    return load_json(SOURCES, {}).get("game", "Grand Theft Auto|GTA")

def is_game(clip):
    """
    Twitch and Kick clips carry the stream category (e.g. 'Grand Theft Auto VI').
    Only the game in sources.json passes; Just Chatting, slots or other games are
    remembered and never checked again. Unknown category = no (gate 1 of 2;
    publikuj.py checks the saved category again before anything goes out).
    YouTube windows were already filtered by video title.
    """
    if clip.get("platform") == "youtube":
        return bool(re.search(game_re(), clip.get("category", ""), re.I))
    if clip.get("platform") not in ("twitch", "kick"):
        return False
    try:
        cats = ytdlp_json(clip["url"], timeout=60).get("categories") or []
    except Exception as e:                            # yt-dlp error, bad JSON, timeout ...
        log(f"  skip {clip['channel']} {clip['id']}: category unknown ({str(e)[-80:]})")
        return False
    clip["category"] = " ".join(cats)
    if re.search(game_re(), clip["category"], re.I):
        return True
    state = load_json(STATE, {"done": {}, "failed": {}})
    state.setdefault("other_game", {})[clip["id"]] = " ".join(cats)
    save_json(STATE, state)
    log(f"  skip {clip['channel']} {clip['id']}: category {cats}")
    return False

def process(clips, clean=False, limit=None):
    n = 0
    for c in clips:
        if limit is not None and n >= limit:
            break
        if skip(c["id"]) or not is_game(c):
            continue
        log(f"render {c['platform']}/{c['channel']} '{c['title'][:60]}' {c['views']} views")
        try:
            render(c, clean=clean)
            mark(c["id"], True)
            n += 1
        except NotGameplay as e:                 # right category, wrong picture: never again
            state = load_json(STATE, {"done": {}, "failed": {}})
            state.setdefault("other_game", {})[c["id"]] = str(e)
            save_json(STATE, state)
            log(f"  skip: {e}")
        except Exception as e:  # one broken clip must not stop launch day
            mark(c["id"], False)
            log(f"  FAILED {c['url']}: {e}")
    return n

def virality(c):
    """Views per hour of age (softened): what is spreading right now beats an old slow climber."""
    age_h = (time.time() - c["created"]) / 3600 if c.get("created") else 24
    return (c.get("views") or 0) / (max(age_h, 0) + 1) ** 0.5

def fresh(clips, days):
    now = time.time()
    return [c for c in clips if not c["created"] or now - c["created"] <= days * 86400]

def cmd_batch(a):
    """
    Every source in sources.json: collect the fresh moments first (listing is cheap),
    rank them all together, then render only the best `max` (rendering is the cost).
    Twitch/Kick rank by views (at most n per channel, so one streamer can't fill the day);
    YouTube windows by audience signal, at most `yt` of them.
    """
    src = load_json(SOURCES, {})
    blocked = {b.lower() for b in src.get("blocklist", [])}   # streamers who asked us to stop
    if a.max is None:                                          # render only what we will post today
        ramp = src.get("ramp") or {}
        today = datetime.now().date().isoformat()
        past = [k for d, k in sorted(ramp.items()) if d <= today]
        a.max = past[-1] if past else (min(ramp.values()) if ramp else 10)
    live = []
    for platform, lister in (("twitch", lambda ch: list_twitch(ch, "24hr" if a.days <= 1 else "7d")),
                             ("kick", list_kick)):
        for ch in src.get(platform, []):
            if ch.lower() in blocked:
                continue
            try:
                clips = [c for c in fresh(lister(ch), a.days) if c["views"] >= a.min_views and not skip(c["id"])]
            except Exception as e:                             # one dead channel must not stop the day
                log(f"{platform} {ch}: {str(e)[-150:]}")
                continue
            live += clips[: a.n]
    live.sort(key=virality, reverse=True)
    yt = []
    if a.yt:
        for handle in src.get("youtube", []):
            if handle.lstrip("@").lower() in blocked:
                continue
            try:
                yt += [c for c in youtube_clips(handle, a.days, a.per_video) if not skip(c["id"])]
            except Exception as e:
                log(f"youtube {handle}: {str(e)[-150:]}")
    log(f"candidates: {len(live)} twitch/kick, {len(yt)} youtube -> rendering up to {a.max}")
    total = process(yt, a.clean, limit=min(a.yt, a.max))
    total += process(live, a.clean, limit=a.max - total)
    log(f"batch done: {total} new shorts")
    if total == 0 and os.environ.get("GITHUB_ACTIONS") and datetime.now().date() >= date(2026, 11, 19):
        print("::warning::Dnes se nevyrenderoval zadny GTA 6 klip (zadne cerstve klipy, nebo blokace Twitch/Kick).")

def cmd_top(a):
    for ch in a.channels:
        clips = [c for c in list_twitch(ch, a.range) if c["views"] >= a.min_views and not skip(c["id"])]
        process(clips[: a.n], a.clean)

def clip_info(url):
    info = ytdlp_json(url, timeout=120)
    platform = "kick" if "kick.com" in url else "youtube" if ("youtube.com" in url or "youtu.be" in url) else "twitch"
    # Twitch: 'uploader' is the viewer who made the clip, 'channel' is the streamer
    channel = (info.get("uploader_id") if platform == "youtube" else info.get("channel")) or ""
    if not channel and "twitch.tv/" in url and "clips.twitch.tv" not in url:
        channel = url.split("twitch.tv/")[1].split("/")[0]
    return {"id": f"{platform}_{info.get('id')}", "url": url, "title": info.get("title") or "",
            "views": int(info.get("view_count") or 0), "duration": float(info.get("duration") or 0),
            "created": int(info.get("timestamp") or 0), "channel": (channel or "unknown").lstrip("@").lower(),
            "platform": platform,
            "category": " ".join(info.get("categories") or []) if platform != "youtube" else
                        " ".join([info.get("title") or ""] + (info.get("tags") or []))}

def cmd_one(a):
    if Path(a.url).exists():                  # local file: channel must be given
        clip = {"id": Path(a.url).stem, "url": a.url, "title": "", "views": 0, "duration": 0,
                "created": 0, "channel": (a.channel or "local").lower(), "platform": "local"}
        render(clip, src=Path(a.url).resolve(), clean=a.clean)
        return
    clip = clip_info(a.url)
    if a.start is not None:
        end = a.end if a.end is not None else a.start + YT_CLIP
        clip.update(id=f"{clip['id']}_{int(a.start)}", section=[a.start, end], duration=end - a.start)
    render(clip, clean=a.clean)

def cmd_cam(a):
    ch = a.channel.lower()
    db = load_json(STREAMERS, {})
    if db.get(ch, {}).get("cam") and not db[ch].get("auto"):
        sys.exit(f"{ch} has a hand-set cam box in streamers.json; edit it there")
    src = Path(a.source)
    if not src.exists():
        src = OUT / f"_cam_{ch}.mp4"
        run(["yt-dlp", "-q", "-f", "best[height<=1080]/best", "-o", str(src), a.source], timeout=300)
    cam = detect_cam(src)
    if not cam:
        sys.exit("no stable face found - set the box by hand in streamers.json")
    db[ch] = {"cam": cam, "auto": True}
    save_json(STREAMERS, db)
    x, y, w, h = [round(v / 100 * d) for v, d in zip(cam, (SW, SH, SW, SH))]
    preview = OUT / f"_cam_{ch}.jpg"
    run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", "1", "-i", str(src), "-frames:v", "1", "-vf",
         f"{NORM},drawbox=x={x}:y={y}:w={w}:h={h}:color=red:t=6", str(preview)])
    log(f"{ch}: cam {cam} -> preview {preview} (if right, remove \"auto\" in streamers.json)")

def cmd_watch(a):
    """
    Launch-day loop over Twitch + Kick channels (from the command line or sources.json).
    Every `every` seconds list each channel's clips and compute view velocity
    (views per minute since the last poll). A clip is rendered when it is fresh
    (younger than max_age) and either grows faster than min_vpm or already has min_views.
    """
    src = load_json(SOURCES, {})
    targets = [("twitch", c) for c in (a.channels or src.get("twitch", []))]
    if not a.channels:
        targets += [("kick", c) for c in src.get("kick", [])]
    seen, first_seen = {}, {}  # clip id -> (views, poll time); clip id -> time we first saw it
    log(f"watching {len(targets)} channels every {a.every}s (min {a.min_vpm} views/min or {a.min_views} views)")
    while True:
        for platform, ch in targets:
            try:
                clips = list_twitch(ch, "24hr") if platform == "twitch" else list_kick(ch)
            except Exception as e:
                log(f"list {platform}/{ch} failed: {str(e)[-150:]}")
                continue
            now = time.time()
            hot = []
            for c in clips:
                first_seen.setdefault(c["id"], now)
                created = c["created"] or first_seen[c["id"]]   # no timestamp -> first sighting
                age_min = (now - created) / 60
                prev = seen.get(c["id"])
                vpm = (c["views"] - prev[0]) / max((now - prev[1]) / 60, 0.1) if prev else 0.0
                seen[c["id"]] = (c["views"], now)
                if skip(c["id"]) or age_min > a.max_age:
                    continue
                if vpm >= a.min_vpm or c["views"] >= a.min_views:
                    c["vpm"] = round(vpm, 1)
                    hot.append(c)
            hot.sort(key=lambda c: (c["vpm"], c["views"]), reverse=True)
            if hot:
                process(hot[: a.per_round], a.clean)
        time.sleep(a.every)

def main():
    global WHISPER_MODEL
    p = argparse.ArgumentParser(description="gta6bananaclips: stream moments -> 9:16 shorts")
    p.add_argument("--clean", action="store_true", help="also write base_9x16.mp4 without captions (editor input)")
    p.add_argument("--whisper", default=WHISPER_MODEL, help="whisper model: small.en, medium.en, turbo ...")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("batch", help="every source in sources.json once")
    b.add_argument("--max", type=int, default=None, help="render at most this many (default: today's ramp quota)")
    b.add_argument("--yt", type=int, default=4, help="of which at most this many from YouTube (0 = skip YouTube)")
    b.add_argument("--n", type=int, default=3, help="clips per Twitch/Kick channel")
    b.add_argument("--per-video", type=int, default=3, help="moments per YouTube video")
    b.add_argument("--days", type=float, default=1, help="only moments from the last N days")
    b.add_argument("--min-views", type=int, default=500)
    b.set_defaults(fn=cmd_batch)

    t = sub.add_parser("top", help="best clips of Twitch channels")
    t.add_argument("channels", nargs="+")
    t.add_argument("--range", default="24hr", choices=["24hr", "7d", "30d", "all"])
    t.add_argument("--n", type=int, default=5)
    t.add_argument("--min-views", type=int, default=0)
    t.set_defaults(fn=cmd_top)

    o = sub.add_parser("one", help="one clip URL, a YouTube video section, or a local file")
    o.add_argument("url")
    o.add_argument("--channel", help="streamer login (needed for a local file)")
    o.add_argument("--start", type=float, help="section start (s), for long videos")
    o.add_argument("--end", type=float, help="section end (s)")
    o.set_defaults(fn=cmd_one)

    c = sub.add_parser("cam", help="detect the facecam box of a streamer and save it")
    c.add_argument("channel")
    c.add_argument("source", help="clip URL or local video file")
    c.set_defaults(fn=cmd_cam)

    w = sub.add_parser("watch", help="live loop for launch day (Twitch + Kick)")
    w.add_argument("channels", nargs="*", help="Twitch logins; default: twitch + kick from sources.json")
    w.add_argument("--every", type=int, default=90, help="seconds between polls")
    w.add_argument("--min-vpm", type=float, default=300, help="views per minute that make a clip hot")
    w.add_argument("--min-views", type=int, default=20000)
    w.add_argument("--max-age", type=int, default=180, help="ignore clips older than N minutes")
    w.add_argument("--per-round", type=int, default=3, help="max renders per channel per poll")
    w.set_defaults(fn=cmd_watch)

    a = p.parse_args()
    WHISPER_MODEL = a.whisper
    if getattr(a, "channels", None):
        a.channels = [ch.lower() for ch in a.channels]
    OUT.mkdir(exist_ok=True)
    try:
        a.fn(a)
    except KeyboardInterrupt:
        log("stopped")

if __name__ == "__main__":
    sys.exit(main())
