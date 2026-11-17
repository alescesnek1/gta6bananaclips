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
from concurrent.futures import ThreadPoolExecutor
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

import cv2
import numpy as np

BRAND = "gta6bananaclips"
HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
TMP = OUT / "_tmp"                   # audio for peak detection, deleted after use
# rendered / failed clip ids; the YouTube worker PC keeps its own file (KLIPY_STATE), because it
# resets its checkout to GitHub before every run (worker/beh.ps1)
STATE = Path(os.environ.get("KLIPY_STATE") or HERE / "state.json")
STREAMERS = HERE / "streamers.json"  # facecam box per streamer, in percent
SOURCES = HERE / "sources.json"      # which channels we take moments from
BRANDS = HERE / "brands.json"        # our accounts (bananas, fanpage): look of each short, see docs/dva-ucty.md
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

# Age-restricted YouTube videos need a signed-in account. Export the cookies of a separate
# throwaway YouTube account (not your main Google account) to this file; it stays local
# (.gitignore) and is only passed to yt-dlp for YouTube URLs.
YT_COOKIES = HERE / "youtube_cookies.txt"

def run(cmd, timeout=None):
    """Run a command, raise RuntimeError with its stderr tail if it fails or hangs."""
    if cmd[0] == "yt-dlp" and YT_COOKIES.exists() and any("youtube.com" in c or "youtu.be" in c for c in cmd):
        cmd = [cmd[0], "--cookies", str(YT_COOKIES)] + cmd[1:]
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
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    enc = sys.stdout.encoding or "utf-8"              # Windows console is cp1250: emoji in titles must not crash
    print(line.encode(enc, "replace").decode(enc), flush=True)

def ff_path(p):
    """Path as ffmpeg filter argument on Windows: forward slashes, escaped drive colon."""
    return str(p).replace("\\", "/").replace(":", "\\:")

NORM = f"scale={SW}:{SH}:force_original_aspect_ratio=increase,crop={SW}:{SH},setsar=1"

_norm_cache = {}

def src_norm(src):
    """
    Per-source normalisation: cut black letterbox/pillarbox bars that are baked into the
    video (YouTube uploads often have them), then fill 1920x1080. Everything (tracking,
    facecam box, render) uses this, so coordinates stay consistent.
    """
    key = str(src)
    if key not in _norm_cache:
        crop = ""
        try:
            iw, ih = (int(v) for v in run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                           "stream=width,height", "-of", "csv=p=0", str(src)]).strip().split(",")[:2])
            p = subprocess.run(["ffmpeg", "-nostdin", "-i", str(src), "-vf", "fps=2,cropdetect=limit=20:round=2:reset=0",
                                "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                               stdin=subprocess.DEVNULL, timeout=300)
            m = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", p.stderr)
            if m:
                w, h, x, y = map(int, m[-1])
                if w * h < 0.97 * iw * ih and w > 0.5 * iw and h > 0.5 * ih:
                    crop = f"crop={w}:{h}:{x}:{y},"
                    log(f"  black bars in source: cropping to {w}x{h}+{x}+{y}")
        except Exception as e:
            log(f"  cropdetect failed ({str(e)[-80:]}), no crop")
        _norm_cache[key] = crop + NORM
    return _norm_cache[key]

def frames(src, fps, w, h):
    """Yield (t, BGR frame) from a video, resampled to fps and scaled to w x h."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(src), "-vf", f"fps={fps},{src_norm(src)},scale={w}:{h}",
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
        # Twitch: the clip slug from the URL, the same id the Twitch API uses (one clip = one id)
        slug = e["url"].rstrip("/").split("/clip/")[-1] if "/clip/" in e["url"] else e.get("id")
        clips.append({
            "id": f"{platform}_{slug}",
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

HELIX = "https://api.twitch.tv/helix"
_helix = {}

def helix_keys():
    """TWITCH_CLIENT_ID / _SECRET from the environment (GitHub secrets) or .env (home PC)."""
    env = HERE / ".env"
    for line in env.read_text(encoding="utf-8").splitlines() if env.exists() else []:
        k, _, v = line.strip().partition("=")
        if k in ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET") and v.strip():
            os.environ.setdefault(k, v.strip().strip('"'))
    return os.environ.get("TWITCH_CLIENT_ID"), os.environ.get("TWITCH_CLIENT_SECRET")

def helix(path, params):
    """GET on the Twitch API with an app access token (client credentials, no user login)."""
    cid, secret = helix_keys()
    if "token" not in _helix:
        q = urllib.parse.urlencode({"client_id": cid, "client_secret": secret, "grant_type": "client_credentials"})
        req = urllib.request.Request(f"https://id.twitch.tv/oauth2/token?{q}", method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            _helix["token"] = json.load(r)["access_token"]
    req = urllib.request.Request(f"{HELIX}/{path}?{urllib.parse.urlencode(params)}",
                                 headers={"Client-Id": cid, "Authorization": f"Bearer {_helix['token']}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

def list_twitch_game(days, pages=3):
    """
    Clips viewers made in the whole 'Grand Theft Auto VI' category, from EVERY streamer,
    best first (Twitch API Get Clips by game_id). The strongest viral signal we have: a person
    picked the start, the end and the title. English streams only (our captions are English).
    Two windows: the last `days` (pages x 100, by views) and the last 3 hours (100), so a
    fresh, fast-rising clip is not buried under yesterday's big ones on launch day.
    """
    out, seen = [], set()
    for d, n in ((days, pages), (3 / 24, 1)):
        for c in _game_clips(d, n):
            if c["id"] not in seen:
                seen.add(c["id"])
                out.append(c)
    return out

def _game_clips(days, pages):
    name = load_json(SOURCES, {}).get("twitch_game", "Grand Theft Auto VI")
    games = helix("games", {"name": name}).get("data") or []
    if not games:
        log(f"twitch api: category '{name}' not found")
        return []
    since = datetime.fromtimestamp(time.time() - days * 86400, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    clips, cursor = [], None
    for _ in range(pages):
        params = {"game_id": games[0]["id"], "started_at": since, "first": 100}
        if cursor:
            params["after"] = cursor
        res = helix("clips", params)
        page = [c for c in res.get("data") or [] if (c.get("language") or "en").split("-")[0] == "en"]
        for c in page:
            clips.append({
                "id": f"twitch_{c['id']}", "url": c["url"], "title": (c.get("title") or "").strip(),
                "views": int(c.get("view_count") or 0), "duration": float(c.get("duration") or 0),
                "created": int(datetime.fromisoformat(c["created_at"].replace("Z", "+00:00")).timestamp()),
                "channel": (c.get("broadcaster_name") or "twitch").lower(), "platform": "twitch",
                "category": games[0]["name"],          # known from the API: no category lookup needed
                "video_id": c.get("video_id") or None, "vod_offset": c.get("vod_offset"),   # same moment = same VOD spot
            })
        ids = {c["broadcaster_id"] for c in res.get("data") or []}
        if ids:                                    # Get Clips has only the display name: fetch logins
            users = helix("users", [("id", i) for i in sorted(ids)[:100]]).get("data") or []
            logins = {u["id"]: u["login"] for u in users}
            for clip, c in zip(clips[len(clips) - len(page):], page):
                clip["channel"] = logins.get(c["broadcaster_id"], clip["channel"])
        cursor = (res.get("pagination") or {}).get("cursor")
        if not cursor:
            break
    log(f"twitch api: {len(clips)} English clips in '{games[0]['name']}' since {since}")
    return clips

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
    bins, who = {}, {}
    for c in info.get("comments") or []:
        stamps = TS_RE.findall(c.get("text") or "")
        if len(stamps) >= 3 or c.get("author_is_uploader"):
            continue                                  # a chapter list / the uploader's own index, not a reaction
        for h, m, s in stamps:
            t = int(h or 0) * 3600 + int(m) * 60 + int(s)
            if 0 < t < dur:
                bins[t // 20] = bins.get(t // 20, 0) + 1 + np.log1p(c.get("like_count") or 0)
                who.setdefault(t // 20, set()).add(c.get("author_id") or c.get("author"))
    # a timestamp marks where the moment starts: centre the clip a bit after it.
    # At least 2 different viewers must point at the same moment.
    return [(b * 20 + 10 + YT_CLIP * 0.3, s) for b, s in bins.items() if s >= 2 and len(who[b]) >= 2]

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
    # chat reacts AFTER the moment (stream delay + typing, the payoff can be ~20 s earlier):
    # the 35 s window covers 30 s before the burst and 5 s after it
    return [(i * 10 + 5 - YT_CLIP * 0.36, float(z[i])) for i in range(n) if z[i] >= 4]

def viewer_signals(url, vid, info):
    dur = float(info.get("duration") or 0)
    hm = info.get("heatmap") or []
    if hm:
        v = [b["value"] for b in hm]
        peaks = []
        for i, b in enumerate(hm):
            near = v[max(0, i - 3): i + 4]
            around = np.median(v[max(0, i - 10): i + 11])
            left = max(v[max(0, i - 3): i], default=-1)   # strict on the left: a flat top counts once
            if v[i] >= 0.5 and v[i] > left and v[i] == max(near) and v[i] - around >= 0.15:   # sticks out
                peaks.append(((b["start_time"] + b["end_time"]) / 2, v[i] - around))
        return norm(peaks), "heatmap"
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

def launched():
    """After the launch (sources.json youtube_prelaunch.until = 2026-11-19): gameplay only."""
    until = (load_json(SOURCES, {}).get("youtube_prelaunch") or {}).get("until", "2026-11-19")
    return datetime.now().date() >= date.fromisoformat(until)

def exclude_re():
    """Titles we never take; from the launch on also trailers and their reactions (Aleš 17. 11.)."""
    bad = load_json(SOURCES, {}).get("exclude_titles") or ""
    return bad + ("|trailer|extended look|showcase" if launched() else "")

def video_moments(url, info, per_video, label):
    """One YouTube video (full yt-dlp info) -> clip dicts of its audience-marked moments."""
    vid, dur = info["id"], float(info.get("duration") or 0)
    about = " ".join([info.get("title") or ""] + (info.get("tags") or []))
    if dur < 60 or not re.search(game_re(), about, re.I):
        return []                                         # Shorts, or not a GTA 6 video
    bad = exclude_re()
    if bad and re.search(bad, info.get("title") or "", re.I):
        log(f"  {label}: {vid} skipped (fake/leak/concept title)")
        return []                                         # fan-made, AI or leaked "GTA 6" would mislead viewers
    cands, how = viewer_signals(url, vid, info)
    if not cands:
        log(f"  {label}: {vid} no audience signal yet ({how}), skipped")
        return []
    # a stream's first minutes are "starting soon" hype in chat, not a moment
    was_live = info.get("was_live") or info.get("live_status") == "was_live"
    clips = []
    for s, e2, score in pick_windows(cands, dur, per_video, skip_start=150 if was_live else 15):
        clips.append({
            "id": f"youtube_{vid}_{int(s)}", "url": url, "title": info.get("title") or "",
            "views": int(info.get("view_count") or 0), "duration": e2 - s,
            "created": int(info.get("timestamp") or 0),
            "channel": (info.get("uploader_id") or info.get("channel") or label).lstrip("@").lower(),
            "platform": "youtube", "section": [round(s, 1), round(e2, 1)], "signal": how, "category": about,
            "signal_score": round(float(score), 2),
        })
    log(f"  {label}: {vid} -> {len(clips)} moments ({how})")
    return clips

def full_info(vid, days, min_followers=0):
    """Full info of a fresh, finished video, or None (too old, live now, small channel, error)."""
    url = f"https://www.youtube.com/watch?v={vid}"
    try:
        info = ytdlp_json(url, timeout=120)
    except RuntimeError as err:
        log(f"  {vid}: {str(err)[-120:]}")
        cookie_alarm(str(err))
        return None
    if info.get("live_status") == "is_live" or time.time() - (info.get("timestamp") or 0) > days * 86400:
        return None
    if (info.get("channel_follower_count") or 0) < min_followers:
        return None                                       # small / re-upload channels: credit would go to the wrong person
    return info

def youtube_clips(handle, days, per_video, max_videos=3):
    """Recent videos and streams of a YouTube channel -> its peak moments."""
    clips = []
    for tab in ("videos", "streams"):
        try:
            entries = ytdlp_json(f"https://www.youtube.com/{handle}/{tab}", flat=True, limit=max_videos).get("entries") or []
        except RuntimeError as e:
            log(f"  {handle}/{tab}: {str(e)[-120:]}")
            continue
        for e in entries:
            if not e.get("id") or e.get("live_status") == "is_live":
                continue
            if e.get("title") and not re.search(game_re(), e["title"], re.I):
                continue                                  # cheap title check before the full lookup
            info = full_info(e["id"], days)
            if info:
                clips += video_moments(info.get("webpage_url") or f"https://www.youtube.com/watch?v={e['id']}",
                                       info, per_video, handle)
    return clips

def youtube_search_clips(queries, days, per_video, min_followers, skip_ids=(), max_videos=10):
    """
    Discovery beyond our channel list: the newest YouTube videos for searches like
    "GTA 6 reaction", only from channels with at least min_followers subscribers
    (official creators, not re-upload/clip channels).
    """
    seen, cands = set(skip_ids), []
    for q in queries:
        try:
            url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(q) + "&sp=CAI%253D"
            entries = ytdlp_json(url, flat=True, limit=20).get("entries") or []   # sp=CAI%3D: newest first
        except RuntimeError as e:
            log(f"  search '{q}': {str(e)[-120:]}")
            continue
        for e in entries:
            vid = e.get("id")
            if not vid or vid in seen or e.get("live_status") == "is_live":
                continue
            seen.add(vid)
            if e.get("title") and re.search(game_re(), e["title"], re.I):
                cands.append((int(e.get("view_count") or 0), vid, q))
    cands = sorted(cands, reverse=True)[:max_videos]          # the most watched fresh ones only

    def one(c):
        _, vid, q = c
        info = full_info(vid, days, min_followers)
        return video_moments(info.get("webpage_url") or f"https://www.youtube.com/watch?v={vid}", info,
                             per_video, f"search:{q}") if info else []
    with ThreadPoolExecutor(6) as ex:
        return [x for r in ex.map(one, cands) for x in r]

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

def detect_cam(src, mult=3.0):
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
    bw = min(fw * mult, 960)                      # ~3 face-widths: stays inside the cam overlay (no borders)
    if bw > 0.4 * 960:                            # that's a full-screen camera, not an overlay
        return None
    bh = bw * CAM_H / W                           # keep the top band's aspect (1080:640)
    cx, cy = fx + fw / 2, fy + fh * 0.4           # fallback: face slightly above the middle
    y = cy - bh / 2
    # Haar sometimes "finds a face" on a T-shirt print. The person detector sees the whole
    # streamer: put the top of his head just under the top edge of the box.
    tops = person_tops(src, fx + fw / 2, fy + fh / 2)
    if tops:
        head_top = float(np.median(tops))
        y = head_top - 0.06 * bh
    x = float(np.clip(cx - bw / 2, 0, 960 - bw))
    y = float(np.clip(y, 0, 540 - bh))
    return [round(x / 9.6, 2), round(y / 5.4, 2), round(bw / 9.6, 2), round(bh / 5.4, 2)]

def person_tops(src, fcx, fcy, n=8):
    """Top edge (y, 960x540 px) of the YOLO person that contains the face point, on ~n frames."""
    global _yolox
    if _yolox is None:
        _yolox = Yolox(MODEL)
    tops = []
    for _, img in frames(src, n / max(duration(src), 1.0), 960, 540):
        for cls, conf, cx, cy, bw, bh in _yolox(img):
            if cls == 0 and abs(cx - fcx) < bw / 2 and cy - bh / 2 <= fcy <= cy + bh / 2:
                tops.append(cy - bh / 2)
                break
    return tops if len(tops) >= 3 else []

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

def pick_quote(words, peak=None):
    """
    The line for the post text: the sentence said closest to the payoff (peak, seconds) if we
    know it, else a short exclamation/question, else the first full sentence.
    """
    if peak is not None and words:
        sents, cur = [], []
        for w in words:
            cur.append(w)
            if w[2][-1:] in ".?!":
                sents.append(cur)
                cur = []
        if cur:
            sents.append(cur)
        ok = [x for x in sents if 3 <= len(x) <= 14]
        if ok:
            best = min(ok, key=lambda x: 0 if x[0][0] - 1 <= peak <= x[-1][1] + 1 else min(abs(x[0][0] - peak), abs(x[-1][1] - peak)))
            return " ".join(t for _, _, t in best)[:90]
    text = " ".join(t for _, _, t in words)
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if 3 <= len(s.split()) <= 14]
    sents.sort(key=lambda s: (not s.endswith(("!", "?")), 0))
    q = sents[0] if sents else " ".join(text.split()[:10])
    return q[:90]

def quality_check(short, cam, cam_h=CAM_H, until=None):
    """
    Last gate before anything can be published (Aleš: no cut faces, no black bars, no errors):
      - black bars: full-width rows that are pure black on most frames
      - facecam band: a whole face (not touching the band edges) on at least half the frames
    """
    global _cascade
    if _cascade is None:
        _cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    n = black = face_ok = 0
    for i, (_, img) in enumerate(frames_raw(short, 1, 540, 960)):    # 1 fps: frame i = second i
        if until is not None and i >= until:          # the score card covers the picture: not a facecam fault
            break
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        n += 1
        dark = (g.mean(axis=1) < 10) & (g.std(axis=1) < 2.5)     # true letterbox black, not a dark scene
        # a letterbox bar sits on an EDGE of a band (top/bottom of the game, top of the short);
        # a dark night sky in the middle of the picture is not a bar
        edges = [0, (cam_h // 2) if cam else 0, g.shape[0] - 1]
        best = 0
        for e0 in edges:
            step = -1 if e0 == g.shape[0] - 1 else 1
            run, i = 0, e0
            while 0 <= i < g.shape[0] and dark[i]:
                run, i = run + 1, i + step
            best = max(best, run)
        black += best >= 10                       # 10 rows here = 20 px in the 1080x1920 short
        if cam:
            band = g[: cam_h // 2]
            faces = _cascade.detectMultiScale(band, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40))
            # hair cut at the top edge is fine; a chin or cheek cut off is not
            face_ok += any(y + h < cam_h // 2 - 4 and x > 4 and x + w < 536 for x, y, w, h in faces)
    problems = [] if cam else ["no facecam"]
    if n and black / n >= 0.3:
        problems.append("black bars")
    if cam and n and face_ok / n < 0.4:
        problems.append(f"face in the top band missing or cut ({face_ok}/{n} frames)")
        if os.environ.get("KLIPY_DEBUG"):
            shutil.copy(short, Path(os.environ["KLIPY_DEBUG"]) / f"failed_{int(time.time())}.mp4")
    return problems

def frames_raw(src, fps, w, h):
    """Frames of an already rendered short (no normalisation)."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(src), "-vf", f"fps={fps},scale={w}:{h}",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    size = w * h * 3
    try:
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            yield None, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
    finally:
        p.stdout.close()
        p.wait()

class NotGameplay(Exception):
    """The clip is in the GTA category but shows no game (e.g. full-screen webcam talk)."""

def talking_head(src):
    """
    Full-screen webcam instead of game: one person (the streamer) fills a big part of the
    picture on most frames AND stays in the same place. Measured on real clips: webcam 100 %
    of frames over 18 % of the area, GTA gameplay at most 7 %. Trailers/cutscenes also have big
    close-ups, but they cut between shots, so the big person jumps around - a webcam doesn't.
    """
    global _yolox
    if _yolox is None:
        _yolox = Yolox(MODEL)
    n, centres = 0, []
    for _, img in frames(src, 2, 960, 540):
        people = [d for d in _yolox(img) if d[0] == 0]
        n += 1
        if people:
            best = max(people, key=lambda d: d[4] * d[5])
            if best[4] * best[5] / (960 * 540) > 0.18:
                centres.append((best[2] / 960, best[3] / 540))
    if n == 0 or len(centres) / n < 0.6:
        return False
    c = np.array(centres)
    return bool(c[:, 0].std() < 0.08 and c[:, 1].std() < 0.08)

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

def game_bars(src, loose=False):
    """
    Black bars at the top/bottom of the GAME picture only (a letterboxed trailer inside a stream:
    the streamer's cam and chat cover the bars at the sides, so a whole-frame cropdetect misses
    them). Looks at the middle columns; a row counts as bar when it is pure black on 70 % of frames.
    Returns (top, bottom) in 1920x1080 pixels.
    """
    masks = []
    for _, img in frames(src, 1, 960, 540):
        g = cv2.cvtColor(img[:, 300:660], cv2.COLOR_BGR2GRAY)
        masks.append((g.mean(axis=1) < (26 if loose else 16)) & (g.std(axis=1) < (10 if loose else 6)))
    if len(masks) < 3:
        return 0, 0
    bar = np.mean(masks, axis=0) >= (0.5 if loose else 0.7)
    top = int(np.argmin(bar)) if bar[0] else 0
    bot = int(np.argmin(bar[::-1])) if bar[-1] else 0
    if top + bot > 270:                       # more than half black: something is wrong, don't trust it
        return 0, 0
    if top or bot:
        log(f"  black bars in the game picture: top {top * 2}px, bottom {bot * 2}px -> cropped")
    return top * 2 + (2 if top else 0), bot * 2 + (2 if bot else 0)

def subtitle_band(src, top, bot):
    """
    Burned-in subtitles of the source (YouTube uploads, trailers): white text with dark
    edges in a fixed band near the bottom of the picture. A row counts as subtitle row when
    it has many sharp white pixels on at least 20 % of frames. Returns how many pixels to cut
    from the bottom (1920x1080 space), 0 if there are no subtitles.
    """
    lo, hi = top // 2, 540 - bot // 2                      # the picture area in 960x540
    zone = int(lo + (hi - lo) * 0.7)                       # look only at the lowest 30 %
    hits = []
    for _, img in frames(src, 2, 960, 540):
        g = cv2.cvtColor(img[zone:hi, 120:840], cv2.COLOR_BGR2GRAY).astype(np.int16)
        edge = np.abs(np.diff(g, axis=1))[:, :] > 60
        white = g[:, 1:] > 200
        hits.append((white & edge).mean(axis=1) > 0.015)
    if len(hits) < 4:
        return 0
    rows = np.mean(hits, axis=0) >= 0.25
    if not rows.any():
        return 0
    # subtitles = ONE narrow band at the bottom; a busy bright scene lights up many rows -> ignore
    blocks, start = [], None
    for i, r in enumerate(list(rows) + [False]):
        if r and start is None:
            start = i
        elif not r and start is not None:
            blocks.append((start, i))
            start = None
    b0, b1 = blocks[-1]                                    # the lowest block ...
    for c0, c1 in reversed(blocks[:-1]):                   # ... plus the lines just above it (2-line subtitles)
        if b0 - c1 <= 24:
            b0 = c0
        else:
            break
    if b1 - b0 > 0.2 * (hi - lo) or b1 - b0 < 4:
        return 0
    cut = (hi - (zone + b0) + 6) * 2                       # from the top of the band to the bottom, plus a margin
    log(f"  subtitles in the source: cutting {cut}px at the bottom")
    return cut

def content_box(src):
    """
    The part of the frame that is EVER not black (per-pixel max over 1 fps frames), as
    (x0, y0, x1, y1) in 1920x1080 - finds black bars of any size, even half the picture.
    """
    mx = None
    for _, img in frames(src, 1, 480, 270):
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        mx = g if mx is None else np.maximum(mx, g)
    if mx is None:
        return 0, 0, SW, SH
    rows, cols = np.where(mx.max(axis=1) > 30)[0], np.where(mx.max(axis=0) > 30)[0]
    if rows.size < 20 or cols.size < 20:
        return 0, 0, SW, SH
    return int(cols[0]) * 4, int(rows[0]) * 4, int(cols[-1] + 1) * 4, int(rows[-1] + 1) * 4

_detections = {}   # (source, cam) -> player positions; both brands' crops reuse one detection pass

def track_path(src, cam, crop_w, dur, xlim=None):
    """
    Per-output-frame crop x (left edge, source pixels) that follows the player.
    Returns (xs, lo, hi, cuts): the path, the allowed range of x, the cuts in the source (s).
    """
    global _yolox
    key = (str(src), tuple(cam) if cam else None)
    if key not in _detections:
        if _yolox is None:
            _yolox = Yolox(MODEL)
        dw, dh = 960, 540                         # detect on half resolution (fast enough on CPU)
        cam_px = [cam[0] / 100 * dw, cam[1] / 100 * dh, cam[2] / 100 * dw, cam[3] / 100 * dh] if cam else None
        ts, xs, prev, cuts, hist0 = [], [], None, [], None
        for t, img in frames(src, DET_FPS, dw, dh):
            # a cut in the source (or our cold start): the camera must jump, not pan across (PySceneDetect idea)
            hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            hist = cv2.normalize(cv2.calcHist([hsv], [0, 1], None, [16, 16], [0, 180, 0, 256]), None).flatten()
            if hist0 is not None and cv2.compareHist(hist0, hist, cv2.HISTCMP_BHATTACHARYYA) > 0.45:
                cuts.append(t)
                prev = None                               # the player from the old shot is gone
            hist0 = hist
            cx = pick_target(_yolox(img), cam_px, prev, dw, dh)
            if cx is not None:
                prev = cx
            ts.append(t)
            xs.append(np.nan if cx is None else cx * SW / dw)
        raw = np.array(xs, dtype=np.float64)
        log(f"  tracking: player found on {(~np.isnan(raw)).sum()}/{len(raw)} samples, {len(cuts)} cuts")
        _detections[key] = (ts, raw, cuts)
    ts, raw, cuts = _detections[key]
    n_out = max(int(dur * FPS), 1)
    found = ~np.isnan(raw)
    if not found.any():
        target = np.full(n_out, SW / 2)       # GTA's camera already centres the player
    else:
        target = np.interp(np.arange(n_out) / FPS, np.array(ts)[found], raw[found])

    # lazy camera: ignore small moves (dead zone), follow big ones with capped speed
    dead, k, vmax = crop_w * 0.12, 0.10, 28.0
    cut_frames = sorted({min(n_out - 1, int(c * FPS)) for c in cuts})
    cur, path = target[0], np.empty(n_out)
    for i, tx in enumerate(target):
        if i in cut_frames:
            cur = tx                                  # new shot: jump straight to the player
        d = tx - cur
        if abs(d) > dead:
            cur += float(np.clip((d - np.sign(d) * dead) * k, -vmax, vmax))
        path[i] = cur
    out, edges = [], [0] + cut_frames + [n_out]       # 0.5 s smoothing inside each shot, never across a cut
    for a, b in zip(edges, edges[1:]):
        if b > a:
            seg = path[a:b]
            out.append(np.convolve(np.pad(seg, 7, mode="edge"), np.ones(15) / 15, mode="valid"))
    path = np.concatenate(out)

    # keep the facecam overlay out of the game crop (it is already on top): a cam on the
    # left pushes the crop's left edge past it, a cam on the right caps the right edge
    lo, hi = (xlim[0], max(xlim[0], xlim[1] - crop_w)) if xlim else (0, SW - crop_w)
    if cam:
        margin = 0.03 * SW
        cam_l, cam_r = cam[0] / 100 * SW, (cam[0] + cam[2]) / 100 * SW
        if cam_r < SW / 2:
            lo = min(cam_r + margin, hi)
        elif cam_l > SW / 2:
            hi = max(cam_l - margin - crop_w, lo)
    return np.clip(path - crop_w / 2, lo, hi).round().astype(int), lo, hi, cuts

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
    audio = whisper.load_audio(str(src))              # 16 kHz mono, for the singing check
    words, dropped = [], 0
    for seg in res["segments"]:
        # captions = what the STREAMER says (Aleš: they were lyrics of the in-game radio):
        # drop segments Whisper itself doubts, repetitive ones (choruses) and sung ones
        sung = singing(audio[int(seg["start"] * 16000): int(seg["end"] * 16000)])
        if (seg.get("no_speech_prob", 0) > 0.5 or seg.get("avg_logprob", 0) < -0.9
                or seg.get("compression_ratio", 1) > 2.2 or sung > 0.45):
            dropped += 1
            continue
        words += [(w["start"], w["end"], w["word"].strip()) for w in seg.get("words", []) if w["word"].strip()]
    if dropped:
        log(f"  captions: {dropped} segment(s) dropped as music/lyrics or unsure")
    return words

def singing(x, sr=16000):
    """
    0..1: how much of the voiced audio holds a steady pitch for 120+ ms. Sung notes do (song
    lyrics from the game radio), speech glides all the time. Autocorrelation pitch, 80-500 Hz.
    """
    if x.size < sr // 2:
        return 0.0
    win, hop = int(0.04 * sr), int(0.01 * sr)
    lo, hi = sr // 500, sr // 80
    f0 = []
    for i in range(0, x.size - win, hop):
        fr = x[i: i + win] - x[i: i + win].mean()
        if np.sqrt((fr * fr).mean()) < 0.01:          # silence
            f0.append(0.0)
            continue
        ac = np.correlate(fr, fr, mode="full")[win - 1:]
        lag = lo + int(np.argmax(ac[lo:hi]))
        f0.append(sr / lag if ac[lag] > 0.5 * ac[0] else 0.0)
    f0 = np.array(f0)
    voiced = f0 > 0
    if voiced.sum() < 10:
        return 0.0
    steady = np.zeros_like(voiced)
    run = 0
    for i in range(1, len(f0)):
        ok = voiced[i] and voiced[i - 1] and abs(f0[i] - f0[i - 1]) / f0[i - 1] < 0.02
        run = run + 1 if ok else 0
        if run >= 12:
            steady[i - run: i + 1] = True
    return float(steady[voiced].mean())

def ass_time(t):
    t = max(t, 0.0)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"

def ass_col(rgb):
    """RGB hex 'FFE135' -> ASS BGR '35E1FF'."""
    rgb = rgb.lstrip("#").upper()
    return rgb[4:6] + rgb[2:4] + rgb[0:2]

def cap_font(cap):
    """The brand's caption font if its file is in fontdir (e.g. Anton -> anton.ttf), else Montserrat Black."""
    name = cap.get("font") or CAP_FONT
    if name == CAP_FONT:
        return name
    fdir = HERE / cap.get("fontdir", "fonts/cap")
    key = name.lower().split()[0]
    return name if any(p.stem.lower().startswith(key) for p in fdir.glob("*.[ot]tf")) else CAP_FONT

# caption colour by the emotion of the word (HPC frame, Aleš 17. 11., both accounts)
EMO_WIN = ["lol", "lmao", "lmfao", "let's go", "lets go", "w", "yes", "gg", "clutch", "easy"]
EMO_DRAMA = ["what", "no", "wtf", "dead", "died", "run", "help", "oh my god", "fuck", "fucking", "shit"]
EMO_COL = {"win": "39FF88", "drama": "FF3B3B"}
SWEAR_RE = re.compile(r"^(motherfuck|fuck|shit|bullshit|bitch|asshole|dick)", re.I)

def _tok(t):
    return t.lower().strip(".,!?\"'")

def word_emotions(words):
    """Per word: 'win', 'drama' or None. Phrases ('let's go', 'oh my god') colour all their words."""
    toks = [_tok(t) for _, _, t in words]
    out = [None] * len(toks)
    for kind, phrases in (("win", EMO_WIN), ("drama", EMO_DRAMA)):
        for ph in phrases:
            pt = ph.split()
            for i in range(len(toks) - len(pt) + 1):
                if toks[i: i + len(pt)] == pt:
                    out[i: i + len(pt)] = [kind] * len(pt)
    for i, t in enumerate(toks):
        if re.fullmatch(r"(ha){2,}h?", t) or re.fullmatch(r"y+e+s+", t):
            out[i] = "win"
        elif "***" in t or SWEAR_RE.match(t):
            out[i] = "drama"
    return out

def mask_swears(words, until=1e9):
    """Hard swears anywhere in the short (Aleš 17. 11.): caption F***, and their times to BLEEP."""
    out, mute = [], []
    for s0, e0, t in words:
        core = _tok(t)
        if s0 < until and SWEAR_RE.match(core):
            t = t.replace(t.strip(".,!?\"'"), core[0].upper() + "***", 1)
            mute.append((s0, e0))
        out.append((s0, e0, t))
    return out, mute

def captions_ass(words, path, y, cap=None, per=2, anim="pop"):
    """
    Word-by-word captions. `cap` = the brand's caption style from brands.json (font, size,
    colours, outline, shadow, upper); None = the original banana look.
    per=2, anim='pop': bananas - two words, the spoken one in the highlight colour pops.
    per=1, anim='bounce': fanpage - one word at a time scaling in 0 -> 115 % -> 100 % in 0.18 s,
    strong words (long, or shouted with ! ?) in the highlight colour.
    The spoken word takes the colour of its emotion (win green, drama red, word_emotions)
    instead of the highlight colour.
    """
    cap = cap or {}
    font, size = cap_font(cap), int(cap.get("size", CAP_SIZE))
    col, hcol = ass_col(cap.get("color", "FFFFFF")), ass_col(cap.get("highlight", "FFE135"))
    # same letters per line as the original look at its size; a smaller font fits a few more
    chars = int(cap.get("chars") or (CAP_CHARS if size == CAP_SIZE else round(CAP_CHARS * CAP_SIZE / size)))
    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
        "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        # bananas: thin outline + a soft drop shadow (Aleš: no thick outlined captions)
        f"Style: Cap,{font},{size},&H00{col},&H00{col},&H00000000,&H64000000,0,0,0,0,100,100,1,0,1,"
        f"{cap.get('outline', 4)},{cap.get('shadow', 4)},5,40,40,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    up = cap.get("upper", True)
    clean = lambda s: (lambda x: x.upper() if up else x)(s.replace("\\", "").replace("{", "(").replace("}", ")"))
    emo = [None] * len(words)                          # emotion colours off (Aleš 17. 11.): one steady highlight
    ecol = lambda k, default: ass_col(EMO_COL[k]) if k else default
    chunks, cur = [], []
    for wi, wd in enumerate(words):       # break on 2 words / `chars` letters, a pause, or sentence end
        wd = (*wd, wi)
        if cur and (len(cur) == per or sum(len(x[2]) + 1 for x in cur) + len(wd[2]) > chars or wd[0] - cur[-1][1] > 0.6):
            chunks.append(cur)
            cur = []
        cur.append(wd)
        if wd[2][-1:] in ".?!":
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    hi = "{\\c&H%s&\\fscx90\\fscy90\\t(0,110,\\fscx108\\fscy108)}"   # highlight colour + quick pop (openshorts)
    lines = []
    for ci, ch in enumerate(chunks):
        chunk_end = chunks[ci + 1][0][0] if ci + 1 < len(chunks) else ch[-1][1] + 0.4
        chunk_end = min(chunk_end, ch[-1][1] + 0.6)
        for i, (s, e, _, wi) in enumerate(ch):
            end = ch[i + 1][0] if i + 1 < len(ch) else chunk_end
            if anim == "bounce":
                t = ch[i][2]
                strong = len(t.strip(".,!?\"'")) >= 6 or t[-1:] in "!?"
                c = ecol(emo[wi], hcol if strong else None)
                text = ("{\\fscx0\\fscy0\\t(0,110,\\fscx115\\fscy115)\\t(110,180,\\fscx100\\fscy100)"
                        + (f"\\c&H{c}&" if c else "") + "}" + clean(t) + "{\\r}")
            else:
                text = " ".join((hi % ecol(emo[wi], hcol) + clean(t) + "{\\r}") if j == i else clean(t)
                                for j, (_, _, t, _) in enumerate(ch))
            # one very long word would run off the screen: shrink just that line
            n = len(" ".join(x[2] for x in ch))
            fs = f"\\fs{int(size * chars / n)}" if n > chars else ""
            text = text.replace("{\\r}", "{\\r" + fs + "}") if fs else text
            lines.append(f"Dialogue: 0,{ass_time(s)},{ass_time(max(end, s + 0.05))},Cap,,0,0,0,,{{\\blur1.5{fs}\\pos(540,{y})}}{text}")
    path.write_text(head + "\n".join(lines) + "\n", encoding="utf-8")
    return len(chunks)

# -----------------------------------------------------------------------------
# 5) render one clip to 9:16

GRADE = "eq=contrast=1.06:brightness=0.02:saturation=1.3"   # edit look: punchier colours in the game band

# fanpage night-drive look on top of its brands.json grade: teal shadows, orange highlights,
# more contrast and a soft vignette (Aleš 17. 11.: "teasing, sexy, vrrr", a sleek edit)
FAN_LOOK = "colorbalance=rs=-0.07:gs=0.0:bs=0.09:rh=0.09:gh=0.02:bh=-0.08,eq=contrast=1.08,vignette=angle=PI/5"

# the original account; used as is when brands.json is missing (then every clip is only 'bananas')
DEFAULT_BRAND = {"name": BRAND, "short": "short_9x16.mp4", "banana": True, "score_card": True, "music": None,
                 "caption": None, "grade": GRADE, "hook": "flash", "punch": 1.08, "crop_shift": 0.0,
                 "watermark": BRAND, "follow": "assets/follow/follow_bananas.webm"}

def brands():
    """brands.json (docs/dva-ucty.md); missing or broken -> only 'bananas', the look of before."""
    cfg = load_json(BRANDS, None)
    if not isinstance(cfg, dict) or not any(isinstance(v, dict) for v in cfg.values()):
        return {"both_score": float("inf"), "bananas": dict(DEFAULT_BRAND)}
    return cfg

def brand_cfg(cfg, name):
    """One brand with defaults filled in; a brand without 'music' gets none (never the other brand's)."""
    b = {**DEFAULT_BRAND, "music": None if name == "bananas" else [], **cfg[name]}
    b["key"] = name
    return b

def assign_brands(clip, cfg=None):
    """
    Which accounts get this clip: a very viral one (virality >= both_score, or a live chat
    explosion with spike_z >= 10) goes to all of them, each in its own version; every other
    clip goes to one account, taking turns (round robin, last one in state.json 'rr').
    KLIPY_BOTH_SCORE overrides both_score (tests).
    """
    cfg = cfg or brands()
    names = [k for k, v in cfg.items() if isinstance(v, dict)]
    if len(names) < 2:
        return names
    both = float(os.environ.get("KLIPY_BOTH_SCORE") or cfg.get("both_score", float("inf")))
    if virality(clip) >= both:                         # both_score is off in brands.json: one clip = one account
        return names
    state = load_json(STATE, {"done": {}, "failed": {}})
    last = state.get("rr")
    nxt = names[(names.index(last) + 1) % len(names)] if last in names else names[0]
    state["rr"] = nxt
    save_json(STATE, state)
    return [nxt]

def shift_path(xs, s, lo, hi):
    """Crop path moved by s px (the second account's frame differs); flips side if the edge blocks it."""
    a, b = np.clip(xs + s, lo, hi), np.clip(xs - s, lo, hi)
    return (a if np.abs(a - xs).mean() >= np.abs(b - xs).mean() else b).round().astype(int)

def beat_grid(path, start, dur):
    """
    Beat period and first beat (s, in the short's timeline) of the music under the short:
    kick energy (40-150 Hz) -> autocorrelation for the tempo, comb sum for the phase.
    """
    try:
        raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{max(dur, 8):.2f}",
                              "-i", str(path), "-af", "lowpass=150,highpass=40", "-ac", "1", "-ar", "4000",
                              "-f", "s16le", "-"], capture_output=True, timeout=120).stdout
    except subprocess.TimeoutExpired:
        return None
    a = np.frombuffer(raw, np.int16).astype(np.float64)
    if a.size < 4000 * 4:
        return None
    e = np.sqrt(np.convolve(a * a, np.ones(40) / 40, mode="same"))[::40]          # 100 Hz envelope
    on = np.maximum(0, np.diff(e, prepend=e[0]))                                    # onsets
    ac = np.correlate(on, on, mode="full")[on.size - 1:]
    lags = np.arange(33, 101)                                                       # 60-180 BPM
    T = lags[int(np.argmax(ac[lags]))] / 100
    k = int(round(T * 100))
    if T < 0.5:                                    # fast beat: pulse on every other one (the heavy hits)
        T, k = T * 2, k * 2
    phase = int(np.argmax([on[p::k].sum() for p in range(k)])) / 100
    return T, phase

def build_filter(cam, cmds_file, x0, crop_w, game_h, crop_h=SH, y0=0, ass_file=None, mark_file=None, nf=NORM,
                 zoom_at=None, beat=None, punch=None, grade=GRADE, punch_z=0.08, wm_text=BRAND, hook=None,
                 cam_h=CAM_H, push=None, replay=None):
    """
    Filter graph ending in [v]. Without ass/mark it returns the clean 9:16 base.
    hook = (kind, T): the cold-start hook ends at T s. 'flash' = slow white flash there (the
    whole picture, under captions and watermarks), 'cut' = hard cut with a 0.2 s whip zoom 1.15 -> 1.0.
    push = shot edges (s): a slow push-in 1.0 -> 1.06 of the game band within every shot (fanpage,
    instead of the alternating punch and the beat pulses).
    replay = (main_end, r0, r1): the short ends at main_end, then r0..r1 again at 0.6x speed with
    a slight zoom - an instant replay of the payoff (fanpage, instead of the flash + shake).
    """
    flash = ""
    if hook and hook[0] == "flash" and hook[1] > 0:
        # slow flash (Aleš: the first was far too fast): fade to white over 0.35 s, hold 0.1 s, back over 0.4 s
        T = hook[1]
        t0, t1 = T - 0.35, T + 0.1
        flash = (f",eq=eval=frame:brightness='0.85*if(lt(t\\,{T:.3f})\\,clip((t-{t0:.3f})/0.35\\,0\\,1)"
                 f"\\,clip(1-(t-{t1:.3f})/0.4\\,0\\,1))'")
    f = f"[0:v]fps={FPS},{nf}{flash},split[a][b];"
    zs = []
    if zoom_at is not None:      # punch-in 1.12x on the payoff: 0.25 s in, hold 1.3 s, 0.55 s out (game band only)
        t0 = max(0.0, zoom_at - 0.25)
        zs.append(f"(1+0.12*clip((it-{t0:.2f})/0.25\\,0\\,1)*clip(({t0 + 2.1:.2f}-it)/0.55\\,0\\,1))")
    if hook and hook[0] == "cut" and hook[1] > 0:      # whip zoom right after the hard cut
        T = hook[1]
        zs.append(f"(1+0.15*between(it\\,{T:.3f}\\,{T + 0.2:.3f})*({T + 0.2:.3f}-it)/0.2)")
    if push and mark_file:
        zs.append("(1+0.06*(" + "+".join(f"between(it\\,{x:.2f}\\,{y:.2f})*(it-{x:.2f})/{y - x:.2f}"
                                          for x, y in zip(push, push[1:]) if y - x > 0.1) + "))")
    if punch and mark_file:      # alternating 1.0 / 1.08 punch-in on phrases and jump cuts (brand 'punch')
        zs.append(f"(1+{round(punch_z, 4):g}*(" + "+".join(f"between(it\\,{x:.2f}\\,{y:.2f})" for x, y in punch) + "))")
    if beat and mark_file:       # edit feel: a small 3 % zoom pulse on every beat of the music
        T, ph = beat
        zs.append(f"(1+0.03*exp(-14*mod(it-{ph:.3f}+{T * 1000:.3f}\\,{T:.3f})))")
    zoom = (f",zoompan=z='{'*'.join(zs)}':d=1:x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':s={W}x{game_h}:fps={FPS}"
            if zs else "")
    grade = f",{grade}" if mark_file and grade else ""
    f += f"[b]sendcmd=f='{cmds_file}',crop@g={crop_w}:{crop_h}:{x0}:{y0},scale={W}:{game_h}{grade}{zoom}[game];"
    if cam:
        x, y, w, h = [round(v / 100 * d) for v, d in zip(cam, (SW, SH, SW, SH))]
        f += f"[a]crop={w}:{h}:{x}:{y},scale={W}:{cam_h}:force_original_aspect_ratio=increase,crop={W}:{cam_h}[cam];"
        f += "[cam][game]vstack=inputs=2[base];"
    else:
        f += "[a]nullsink;[game]null[base];"
    if not mark_file:
        return f + "[base]null[v]"
    chain = "[base]"
    if replay:
        m_end, r0, r1 = replay
        f += (f"[base]split[bm][br];[bm]trim=0:{m_end:.3f},setpts=PTS-STARTPTS,format=yuv420p,setsar=1[bm1];"
              f"[br]trim={r0:.3f}:{r1:.3f},setpts=(PTS-STARTPTS)/0.6,"
              f"zoompan=z='1.03+0.0007*on':d=1:x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':s={W}x{H}:fps={FPS},"
              f"format=yuv420p,setsar=1[br1];[bm1][br1]concat=n=2:v=1:a=0[rp];")
        chain = "[rp]"
    if zoom_at is not None:      # payoff: a 2-frame white flash and a short camera shake (captions stay steady)
        P = zoom_at
        on = f"between(t\\,{P:.2f}\\,{P + 0.35:.2f})"
        chain += (f"eq=eval=frame:brightness='0.28*between(t\\,{P:.2f}\\,{P + 0.07:.2f})',"
                  f"crop=w=iw-32:h=ih-32:x='16+14*sin(t*95)*{on}':y='16+14*cos(t*80)*{on}',scale={W}:{H},setsar=1,")
    if ass_file:
        chain += f"subtitles=filename='{ass_file}':fontsdir='{ff_path(CAP_FONTS)}',"
    # two quiet watermarks:
    #   ours drifts slowly over the gameplay band, bouncing off its edges (DVD-logo style)
    #   the streamer's sits still at the bottom centre, above the TikTok/Reels caption bar
    wm = f"fontfile='{ff_path(WM_FONT_FILE)}':expansion=none:fontsize=30:fontcolor=white@0.6:shadowcolor=black@0.45:shadowx=1:shadowy=1"
    top = (cam_h if cam else 0) + 230                 # below the captions
    span_y = H - 400 - top                            # down to just above the streamer mark
    bounce_x = "30+abs(mod(t*55\\,2*(w-text_w-60))-(w-text_w-60))"
    bounce_y = f"{top}+abs(mod(t*35\\,{2 * span_y})-{span_y})"
    chain += (f"drawtext={wm}:text='{wm_text}':x='{bounce_x}':y='{bounce_y}',"
              f"drawtext={wm}:textfile='{mark_file}':x=(w-text_w)/2:y=h-320[v]")
    return f + chain

# -----------------------------------------------------------------------------
# our banana: a small whole-figure reaction 1-2x per clip (our own element, so the short is
# not a bare re-upload). Two greenscreen videos with 5 reactions each.

BANANA = HERE / "assets" / "banan"
BANANA_KEY = "0x1BD02A"            # the greenscreen colour
REACTIONS = {                       # name: (video number -> reakce<N>.mp4, start s, end s)
    "laugh": (1, 0.5, 2.0), "shock": (1, 2.0, 3.5), "hype": (1, 3.5, 5.0),
    "facepalm": (1, 5.0, 6.5), "mindblown": (1, 6.5, 8.0),
    "rage": (2, 0.5, 2.0), "sideeye": (2, 2.0, 3.6), "crylaugh": (2, 4.0, 5.0),
    "clap": (2, 5.0, 6.5), "point": (2, 6.5, 8.0),
}
# more reactions without touching the code: assets/banan/reakce.json {"name": [video, start, end]}
# (new greenscreen videos reakce3.mp4, reakce4.mp4 ... from docs/banan-reakce-prompt.md)
for _k, _v in load_json(BANANA / "reakce.json", {}).items() if (BANANA / "reakce.json").exists() else []:
    if not _k.startswith("_") and (BANANA / f"reakce{_v[0]}.mp4").exists():
        REACTIONS[_k] = tuple(_v)
TRIGGERS = [                        # what was said -> fitting reactions (only those we have are used)
    (r"\b(lmao|lmfao|lol|ha(ha)+|i'?m dead|crying)\b", ["laugh", "crylaugh", "popcorn"]),
    (r"\b(no way|what|wait|huh|how|oh my god|omg)\b", ["shock", "mindblown", "scared", "thinking"]),
    (r"\b(let'?s go|yes+|clutch|easy|gg|w)\b", ["hype", "point", "dance", "celebrate", "cool", "chefkiss"]),
    (r"\b(fuck(ing)?|shit|bullshit|are you serious)\b", ["rage", "facepalm", "cry"]),
    (r"\b(bruh|why|cringe|dude)\b", ["facepalm", "sideeye", "shrug"]),
]
LOUD = ["shock", "mindblown", "laugh", "hype", "scared"]    # payoff without a trigger word
# the banana's one-line comment next to it (Aleš 15. 11.: banana as a commentator, our own layer)
PHRASES = {
    "laugh": ["LMAOOO", "I'M DEAD", "HAHAHA"], "crylaugh": ["I'M CRYING", "STOPPP", "NOT AGAIN"],
    "shock": ["NO WAY", "WAIT WHAT", "HOLD ON"], "mindblown": ["WHAT?!", "HOW?!", "GTA 6 IS INSANE"],
    "hype": ["LET'S GOOO", "HUGE W", "CLEAN"], "point": ["LOOK AT THIS", "DID YOU SEE THAT", "W"],
    "rage": ["NAHHH", "UNFAIR", "SO MAD"], "facepalm": ["BRUH", "WHY THO", "MAN..."],
    "sideeye": ["SUS", "HMMM", "OKAY..."], "clap": ["RESPECT", "BIG W", "GG"],
    "popcorn": ["THIS IS CINEMA", "PEAK", "MORE"], "scared": ["RUNNN", "NOPE", "AAAAH"],
    "dance": ["VIBES", "LET'S GOOO", "EZ"], "celebrate": ["W", "LEGEND", "LET'S GOOO"],
    "shrug": ["IT IS WHAT IT IS", "WELL...", "OK THEN"], "cool": ["TOO SMOOTH", "EZ", "COLD"],
    "cry": ["NOOO", "PAIN", "WHY"], "thinking": ["WAIT...", "HOW THO", "BIG BRAIN"],
    "chefkiss": ["PERFECTION", "CHEF'S KISS", "10/10"], "salute": ["RESPECT", "LEGEND", "O7"],
}
BANANA_W, BANANA_H, BANANA_X = 200, 356, 30   # whole banana, small; left side (TikTok's buttons are on the right)

def pick_reactions(words, dur, seed, peak=None):
    """
    As few as possible: one reaction, a second only in clips over 30 s. The first one sits on
    the payoff (the loudest moment), its kind chosen by what was said around it; without a
    payoff it lands right after a trigger line, or one calm reaction at 2/3 of the clip.
    """
    rnd = random.Random(seed)
    have = lambda names: [n for n in names if n in REACTIONS] or ["shock"]
    out = []
    if peak is not None and 1.0 <= peak - 0.15 and peak + 1.35 < dur:
        near = " ".join(w.lower() for s0, e0, w in words if peak - 3 <= e0 <= peak + 2)
        kind = next((names for rx, names in TRIGGERS if re.search(rx, near)), LOUD)
        out.append((peak - 0.15, rnd.choice(have(kind))))
    for s, e, w in words:
        if e < 1.5 or (out and e - out[-1][0] < 8):
            continue
        for rx, names in TRIGGERS:
            if re.search(rx, w.lower().strip(".,!?\"'")) or re.search(rx, w.lower()):
                t = e + 0.15
                if t + 1.6 < dur:
                    out.append((t, rnd.choice(have(names))))
                break
        if len(out) >= (2 if dur > 30 else 1):
            break
    if not out and dur > 6:
        out.append((dur * 0.66, rnd.choice(have(["clap", "sideeye", "point"]))))
    return sorted(out)[: (2 if dur > 30 else 1)]

RATING = HERE / "assets" / "rating"            # score_N.webm: end cards from tools/rating (Blender)
RATING_DUR = 2.6                               # seconds added after the clip, on its frozen last frame

def banana_rating(views):
    """The banana's score 1-10 on a log scale: 50 views -> 1, 500 -> 3, 5k -> 6, 50k -> 8, 500k -> 10."""
    return int(round(max(1, min(10, 1 + np.log10(max(views or 1, 1) / 50) * 2.25))))

_tracks = {}

def track_window(path):
    """Usable part of a track (s): from where it gets loud (skips a quiet intro) to 10 s before the end."""
    if path not in _tracks:
        raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "4000",
                              "-f", "s16le", "-"], capture_output=True, timeout=120).stdout
        a = np.frombuffer(raw, np.int16).astype(np.float64)
        rms = np.sqrt(np.convolve(a * a, np.ones(8000) / 8000, mode="valid")[::4000])       # every 1 s
        loud = int(np.argmax(rms > 0.5 * rms.max())) if rms.size else 0
        _tracks[path] = (float(loud), max(float(loud), rms.size - 10.0))
    return _tracks[path]

def music_cfg(seed, total, music=None):
    """
    Background track for the short (sources.json 'music': a list of our own tracks, no Content
    ID). Each short gets one of them and starts at a different place, so it never repeats.
    `music` = the brand's own list from brands.json (globs allowed); [] or nothing found -> no
    music (never the other brand's). Returns (path, volume, start s) or (None, 0, 0).
    """
    cfg = load_json(SOURCES, {})
    if music is not None:
        paths = []
        for m in music:
            paths += sorted(HERE.glob(m)) if any(ch in m for ch in "*?[") else [HERE / m]
    else:
        paths = [HERE / m for m in (cfg.get("music") or [])] if isinstance(cfg.get("music"), list) else         [HERE / cfg.get("music", "assets/hudba/The_Heavy_Toll.mp3")]
    paths = [p for p in paths if p.exists()]
    if not paths:
        return None, 0.0, 0.0
    rnd = random.Random(seed)
    path = rnd.choice(paths)
    lo, hi = track_window(path)
    return path, float(cfg.get("music_volume", 0.18)), rnd.uniform(lo, max(lo, hi - total))

def audio_mix(clip_audio, total, card_idx=None, card_at=0.0, music_idx=None, music_vol=0.4, sfx_idx=None, sfx_at=0.0,
              replay=None, mute=(), riser_idx=None, hit_idx=None, peak=None,
              clip_end=None, outro_idx=None, follow_idx=None, follow_at=0.0):
    """
    The short's sound: the clip's own audio + our music at one steady level (Aleš: always the
    same volume) + the score card's landing 'tups' + the hook flash whoosh (sfx, at sfx_at s).
    A limiter keeps the sum from clipping. replay = (main_end, r0, r1): the clip's sound ends at
    main_end, then r0..r1 again slowed to 0.6x (atempo) and quieter, under the music.
    Tension (HPC): a quiet riser in the 1.2 s before the payoff (peak), a deep hit on it, and the
    music drops to 20 % for 0.6 s there (sudden silence). mute = (start, end) of swears -> silent.
    clip_end = the clip's own sound stops there (an outro follows; its sound = outro_idx, from
    clip_end on). follow_idx = the Follow card's soft click at follow_at. Music runs to `total`.
    """
    parts, f = [], ""
    fmt = "aformat=sample_rates=48000:channel_layouts=stereo"
    if mute:
        fmt0 = fmt + ",volume=0:enable='" + "+".join(f"between(t\\,{a:.2f}\\,{b:.2f})" for a, b in mute) + "'"
    else:
        fmt0 = fmt
    if clip_audio and replay:
        m_end, r0, r1 = replay
        f += (f";[0:a]{fmt0},asplit[ram][rar];[ram]atrim=0:{m_end:.3f},asetpts=PTS-STARTPTS[ram1];"
              f"[rar]atrim={r0:.3f}:{r1:.3f},asetpts=PTS-STARTPTS,atempo=0.6,volume=0.6,{fmt}[rar1];"
              f"[ram1][rar1]concat=n=2:v=0:a=1,apad=whole_dur={total:.3f}[a0]")
        parts.append("[a0]")
    elif clip_audio:
        end = f",atrim=0:{clip_end:.3f},afade=t=out:st={max(0.0, clip_end - 0.05):.3f}:d=0.05" if clip_end else ""
        f += f";[0:a]{fmt0}{end},apad=whole_dur={total:.3f}[a0]"
        parts.append("[a0]")
    if outro_idx is not None and clip_end:
        f += f";[{outro_idx}:a]{fmt},adelay={int(clip_end * 1000)}:all=1,apad=whole_dur={total:.3f}[ou]"
        parts.append("[ou]")
    if follow_idx is not None:
        f += f";[{follow_idx}:a]volume=0.6,{fmt},adelay={int(follow_at * 1000)}:all=1,apad=whole_dur={total:.3f}[fo]"
        parts.append("[fo]")
    if music_idx is not None:                  # adeclick: the generated track has a little crackle
        duck = (f",volume='if(between(t\\,{peak:.2f}\\,{peak + 0.6:.2f})\\,0.2\\,1)':eval=frame"
                if peak is not None and hit_idx is not None else "")
        f += f";[{music_idx}:a]atrim=0:{total:.3f},adeclick,volume={music_vol}{duck},{fmt},apad=whole_dur={total:.3f}[mu]"
        parts.append("[mu]")
    if card_idx is not None:
        f += f";[{card_idx}:a]{fmt},adelay={int(card_at * 1000)}:all=1,apad=whole_dur={total:.3f}[ar]"
        parts.append("[ar]")
    if riser_idx is not None and peak is not None:     # the file is 1.2 s and ends on the payoff
        cut = max(0.0, 1.2 - peak)
        f += (f";[{riser_idx}:a]atrim=start={cut:.3f},asetpts=PTS-STARTPTS,volume=0.35,"
              f"adelay={int(max(0.0, peak - 1.2) * 1000)}:all=1,{fmt},apad=whole_dur={total:.3f}[rs]")
        parts.append("[rs]")
    if hit_idx is not None and peak is not None:
        f += f";[{hit_idx}:a]volume=0.8,adelay={int(peak * 1000)}:all=1,{fmt},apad=whole_dur={total:.3f}[ht]"
        parts.append("[ht]")
    if mute:                                    # the bleep over every masked swear
        on = "+".join(f"between(t\\,{a:.2f}\\,{b:.2f})" for a, b in mute)
        f += (f";aevalsrc='0.45*sin(2*PI*1000*t)*({on})':s=48000:d={total:.3f},afade=t=in:d=0.005,{fmt}[bp]")
        parts.append("[bp]")
    if sfx_idx is not None:
        f += f";[{sfx_idx}:a]volume=0.7,adelay={int(sfx_at * 1000)}:all=1,{fmt},apad=whole_dur={total:.3f}[sx]"
        parts.append("[sx]")
    if not parts:
        return None
    f += f";{''.join(parts)}amix=inputs={len(parts)}:duration=longest:normalize=0,atrim=0:{total:.3f},alimiter=limit=0.95[aud]"
    return f

def card_overlay(f, idx, at, pad, tag="", fade_out=None):
    """
    The banana score card starts where the clip's story ends (`at`, after the payoff, never
    over a moment) and the footage keeps running underneath; only when the source has no more
    footage the last frame holds for `pad` s. Bananas drop into the 'jar' of the short.
    Also lays the Follow card (tag='f': its own labels) over whatever is running.
    """
    f = f[: -len("[v]")] + f"[pre{tag}]"
    hold = f"tpad=stop_mode=clone:stop_duration={pad:.3f}," if pad > 0.01 else ""
    f += (f";[pre{tag}]{hold}null[pz{tag}];"
          f"[{idx}:v]{fade_out or ''}setpts=PTS-STARTPTS+{at:.3f}/TB[rc{tag}];[pz{tag}][rc{tag}]overlay=0:0:eof_action=pass[v]")
    return f

# Follow card (tools/follow/follow.py -> assets/follow/follow_<brand>.webm, brands.json 'follow'):
# a profile card whose Follow button gets clicked, laid over the last FOLLOW_DUR s of the short
# (bananas: over the running score card; fanpage: over the running outro). It never makes the
# short longer. KLIPY_NO_FOLLOW=1 turns it off.
FOLLOW_DUR = 2.2

def outro_append(f, idx, at):
    """The brand's outro (brands.json 'outro', input idx) joined AFTER the clip, which ends at `at` s."""
    f = f[: -len("[v]")] + "[mo]"
    return f + (f";[mo]trim=0:{at:.3f},setpts=PTS-STARTPTS,fps={FPS},format=yuv420p,setsar=1[mv];"
                f"[{idx}:v]fps={FPS},scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                f"setpts=PTS-STARTPTS,format=yuv420p,setsar=1[ov];[mv][ov]concat=n=2:v=1:a=0[v]")

def extras_ass(path, reactions, rating, dur, cam, seed):
    """
    Our own layer on top of the captions (Aleš 15. 11.): a short speech bubble next to every
    banana reaction, and the banana's score for the clip in the last 1.6 s.
    """
    rnd = random.Random(seed)
    y0 = (CAM_H if cam else 0) + 520                   # banana's top edge (see reaction_overlay)
    style = (f"Style: Bub,{CAP_FONT},50,&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,0,0,0,0,100,100,1,0,3,10,0,4,0,0,0,1\n"
             f"Style: Rate,{CAP_FONT},76,&H0035E1FF,&H0035E1FF,&H00000000,&H64000000,0,0,0,0,100,100,1,0,1,0,5,5,0,0,0,1\n")
    txt = path.read_text(encoding="utf-8").replace("\n\n[Events]", "\n" + style.rstrip("\n") + "\n\n[Events]", 1)
    lines = []
    for t, name in []:                                  # bubble off (Aleš 15. 11.: the banana says nothing)
        e = t + (REACTIONS[name][2] - REACTIONS[name][1])
        lines.append(f"Dialogue: 1,{ass_time(t + 0.1)},{ass_time(e)},Bub,,0,0,0,,"
                     f"{{\\pos({BANANA_X + BANANA_W + 6},{y0 + 110})\\fscx70\\fscy70\\t(0,120,\\fscx100\\fscy100)}}"
                     f"{rnd.choice(PHRASES.get(name, ['W']))}")
    if rating and dur > 8:
        lines.append(f"Dialogue: 1,{ass_time(dur - 1.6)},{ass_time(dur)},Rate,,0,0,0,,"
                     f"{{\\pos(540,{(CAM_H if cam else 0) + 380})\\fscx60\\fscy60\\t(0,140,\\fscx100\\fscy100)}}"
                     f"BANANA SCORE {rating}/10")
    path.write_text(txt.rstrip("\n") + "\n" + "\n".join(lines) + "\n", encoding="utf-8")

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
    cap = ["-maxrate", "4M", "-bufsize", "8M"]     # 4 Mb/s: the networks re-encode to ~2-3 anyway; keeps Netlify < 1000 credits
    for venc in (["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "23", "-b:v", "0"] + cap,
                 ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22"] + cap):
        try:
            run(cmd_base + venc + ["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out_path)], timeout=900)
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

PAD = 8.0   # seconds downloaded around a YouTube window, room to move the cut

def snap_cut(words, s, e, total, lo=15.0, hi=40.0):
    """
    Cut points in the padded download: start just before a sentence begins near s, end just
    after a sentence ends a few seconds past the peak (the window centre), 15-40 s long.
    """
    if not words:
        return s, min(total, e)
    gaps = [(0.0, words[0][0])] + [(w1[1], w2[0]) for w1, w2 in zip(words, words[1:]) if w2[0] - w1[1] >= 0.35]
    gaps.append((words[-1][1], total))
    starts = [max(0.0, g1 - 0.15) for _, g1 in gaps if g1 - 0.15 <= s + 6]
    a = min(starts, key=lambda t: abs(t - s), default=s)
    peak = (s + e) / 2
    ends = [min(g0 + 0.5, g1) for g0, g1 in gaps if a + lo <= g0 <= a + hi]
    b = min(ends, key=lambda t: abs(t - (peak + 10)), default=min(total, a + max(lo, e - s)))
    return a, min(b, total, a + hi)

def fetch_section(clip, d, dst):
    """
    A YouTube window is a guess around the audience peak. Download it with room on both
    sides, find the sentence pauses and cut there, so the short starts on a new sentence and
    ends after the reaction instead of mid-word. Returns the words (times in the cut file).
    """
    s, e = clip["section"]
    ps = max(0.0, s - PAD)
    raw = d / "padded.mp4"
    log(f"  download {clip['url']} [{ps:.0f}-{e + PAD:.0f}]")
    download({**clip, "section": [ps, e + PAD]}, raw)
    words = transcribe(raw)
    a, b = snap_cut(words, s - ps, e - ps, duration(raw))
    encode(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{a:.2f}", "-to", f"{b:.2f}",
            "-i", str(raw), "-map", "0:v:0", "-map", "0:a?"], dst)
    raw.unlink(missing_ok=True)
    clip["section"] = [round(float(ps + a), 1), round(float(ps + b), 1)]
    log(f"  cut on sentence pauses: {clip['section']} ({b - a:.0f} s)")
    return [(w0 - a, w1 - a, t) for w0, w1, t in words if w0 >= a and w1 <= b]

def has_audio(src):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                              "-of", "csv=p=0", str(src)], capture_output=True, text=True, timeout=60).stdout
    except subprocess.TimeoutExpired:
        return False
    return bool(out.strip())

def audio_peak(src):
    """
    The payoff: the loudest 0.5 s of the clip (seconds, centre), ignoring the first second.
    Used only for TIMING (cut, banana, zoom, thumbnail) - which clips we take stays a viewer
    decision (clip views), never a volume spike.
    """
    try:
        raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "8000",
                              "-f", "s16le", "-"], capture_output=True, timeout=300).stdout
    except subprocess.TimeoutExpired:
        return None
    a = np.frombuffer(raw, np.int16).astype(np.float64)
    if a.size < 8000 * 3:
        return None
    c = np.concatenate([[0.0], np.cumsum(a * a)])
    rms = np.sqrt((c[4000:] - c[:-4000]) / 4000)[::800]          # 0.5 s windows, every 0.1 s
    t = np.arange(len(rms)) * 0.1 + 0.25
    # Aleš: the hook must be the real payoff (e.g. flying out of the car + his line), not just the
    # loudest second -> loudness + how much the picture changes, weighted towards the clip's end
    mot, prev = np.zeros_like(rms), None
    for tt, img in frames(src, 5, 160, 90):
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
        k = int(round((tt - 0.25) / 0.1))
        if prev is not None and 0 <= k < len(mot):
            mot[k] = float(np.abs(g - prev).mean())
        prev = g
    mot = np.convolve(mot, np.ones(5) / 5, mode="same")
    score = 0.6 * rms / (rms.max() + 1e-9) + 0.4 * mot / (mot.max() + 1e-9)
    pos = t / max(t[-1], 1e-9)
    score *= np.where(pos < 0.3, 0.35, 0.75 + 0.5 * pos)
    score[t < 1.0] = 0
    i = int(np.argmax(score))
    return float(t[i]) if score[i] > 0 else None

def trim_points(words, peak, dur, hi=35.0, lo=8.0):
    """
    Twitch/Kick clip: drop the dead run-up and the tail after the payoff (Fable 15. 11.):
    start 0.4 s before the first word (if the talking starts late), end just after the
    sentence around the payoff, at most 35 s.
    """
    a, b = 0.0, dur
    if words and words[0][0] > 1.5:
        a = max(0.0, min(words[0][0], (peak - 6) if peak else words[0][0]) - 0.4)
    talk_after = [w for w in words if w[0] > (peak or 0) + 5]
    if peak is not None and not (peak < dur / 3 and talk_after):   # a shout early on is not the payoff
        end_sent = next((w[1] for w in words if w[0] >= peak - 0.5 and w[2][-1:] in ".?!"), None)
        end_word = next((w[1] for w in words if w[1] >= peak + 2.5), None)       # no punctuation: end of a word
        # Aleš 17. 11.: after the climax leave a few seconds - the reaction and the sentence that is
        # running finish, never cut mid-sentence (the xQc test ended just barely)
        end = end_sent if end_sent and end_sent <= peak + 6 else (end_word if end_word and end_word <= peak + 6 else peak + 3.0)
        b = min(dur, max(end + 0.5, peak + 3.0))
    elif words and dur - words[-1][1] > 2:
        b = words[-1][1] + 0.8
    if b - a > hi:                                 # too long: keep the 35 s around the payoff, never cut it off
        a = max(a, min(peak - 8, b - hi)) if peak is not None else b - hi
        b = min(b, a + hi)
    if b - a < lo:
        return 0.0, dur
    return a, b

def rms_curve(src):
    """Loudness every 0.1 s (0.5 s windows) -> (times, rms); empty if there is no audio."""
    try:
        raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "8000",
                              "-f", "s16le", "-"], capture_output=True, timeout=300).stdout
    except subprocess.TimeoutExpired:
        return np.zeros(0), np.zeros(0)
    a = np.frombuffer(raw, np.int16).astype(np.float64)
    if a.size < 8000:
        return np.zeros(0), np.zeros(0)
    c = np.concatenate([[0.0], np.cumsum(a * a)])
    rms = np.sqrt((c[4000:] - c[:-4000]) / 4000)[::800]
    return np.arange(len(rms)) * 0.1 + 0.25, rms

def dead_air(words, src, a, b, peak):
    """
    Pieces of [a, b] to KEEP: pauses longer than 0.9 s between words that are also quiet (no
    shouting, no explosion) are jump-cut out, never near the payoff (Aleš: built for ADHD
    scrollers, no dead second). auto-editor idea, done with our own word times + loudness.
    """
    ts, rms = rms_curve(src)
    if not words or rms.size == 0:
        return [(a, b)]
    med = float(np.median(rms))
    keep, cur = [], a
    inside = [w for w in words if a <= w[0] and w[1] <= b]
    for w1, w2 in zip(inside, inside[1:]):
        g0, g1 = w1[1] + 0.2, w2[0] - 0.15
        if g1 - g0 < 0.6 or (peak is not None and g0 - 1.0 < peak < g1 + 1.0):
            continue
        m = (ts >= g0) & (ts <= g1)
        if m.any() and rms[m].mean() < 0.6 * med:
            keep.append((cur, g0))
            cur = g1
    keep.append((cur, b))
    return [(x, y) for x, y in keep if y - x > 0.05]

def edit_cut(src, words, d, clip):
    """
    Cut the source before rendering: trim (Twitch/Kick; YouTube windows are already cut on
    sentences) and a COLD START - the payoff flashes for 1 s first, then the clip plays from
    its start (Aleš 15. 11.). Up to RATING_DUR s of the source after the cut are kept, so the
    clip keeps running under the banana score card (Aleš: the background must not freeze).
    Returns (source, words, payoff time, where the card starts, jump cuts, T) in the new
    timeline; T = where the hook ends and the clip starts (0 = no hook). The transition itself
    (flash or whip cut) is the brand's and is added in the final encode (build_filter).
    """
    dur = duration(src)
    peak = audio_peak(src)
    a, b = (0.0, dur) if clip.get("section") else trim_points(words, peak, dur)
    e = b + min(RATING_DUR, dur - b)                  # live footage for under the card
    tease = peak is not None and peak - a >= 4 and b - peak >= 0.3
    segs = dead_air(words, src, a, b, peak)
    segs[-1] = (segs[-1][0], e)                       # the tail under the card stays continuous
    if not tease and a < 0.5 and b > dur - 0.5 and len(segs) == 1:
        # untouched: the card covers the last seconds, but only if the payoff is safely before them
        late = peak is not None and dur - peak >= RATING_DUR + 1.8
        return src, words, peak, dur - RATING_DUR if late else dur, [], 0.0
    if not has_audio(src):
        return src, words, peak, dur, [], 0.0
    out = d / "edit.mp4"
    # hook (Aleš: 1 s looked like a cut by mistake; the rewind he did not like): ~2.7 s of the
    # payoff, then the transition (bananas: white FLASH + whoosh) and the clip from its start
    ps, pe = (max(0.0, peak - 1.8), min(dur, peak + 0.9)) if tease else (0, 0)
    T = pe - ps if tease else 0.0
    if tease:
        flt = (f"[0:v]trim={ps:.2f}:{pe:.2f},setpts=PTS-STARTPTS[v0];[0:a]atrim={ps:.2f}:{pe:.2f},asetpts=PTS-STARTPTS[a0];"
               + pieces(segs) + f"[v0][a0]{pairs(len(segs))}concat=n={len(segs) + 1}:v=1:a=1[v][a]")
    else:
        flt = pieces(segs) + f"{pairs(len(segs))}concat=n={len(segs)}:v=1:a=1[v][a]"
    encode(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
            "-filter_complex", flt, "-map", "[v]", "-map", "[a]"], out)
    starts, acc = [], T                                # where each kept piece starts in the new timeline
    for x, y in segs:
        starts.append(acc)
        acc += y - x

    def remap(t):
        for (x, y), st in zip(segs, starts):
            if x <= t <= y:
                return st + t - x
        return None

    new = [(w0 - ps, w1 - ps, t) for w0, w1, t in words if tease and w0 >= ps and w1 <= pe]
    for w0, w1, t in words:
        m0, m1 = remap(w0), remap(w1)
        if m0 is not None and m1 is not None:
            new.append((m0, m1, t))
    pk = None if peak is None else remap(peak)
    card = remap(b) if remap(b) is not None else acc - (e - b)
    jumps = [round(st, 2) for st in starts[1:]]       # jump cuts: the zoom switches there to hide them
    log(f"  cut {a:.1f}-{b:.1f} s of {dur:.0f} s (+{e - b:.1f} s under the card), {len(jumps)} jump cuts"
        + (f", hook {pe - ps:.1f} s from the payoff at {peak:.1f} s" if tease else ""))
    return out, new, pk, card, jumps, T

def pieces(segs):
    """ffmpeg trims of the kept pieces -> [v1][a1] ... [vN][aN]."""
    return "".join(f"[0:v]trim={x:.2f}:{y:.2f},setpts=PTS-STARTPTS[v{i}];[0:a]atrim={x:.2f}:{y:.2f},asetpts=PTS-STARTPTS[a{i}];"
                   for i, (x, y) in enumerate(segs, 1))

def pairs(n):
    return "".join(f"[v{i}][a{i}]" for i in range(1, n + 1))

def zoom_jumps(words, jumps, dur):
    """
    Alternating punch-in (1.0 <-> 1.08) for the game band: switches on every jump cut and on
    new phrases, at most every 1.6 s - the picture never sits still (ADHD-proof pacing).
    Returns the zoomed-in intervals.
    """
    marks = set(jumps)
    prev_end = None
    for w0, w1, _ in words:
        if prev_end is None or w0 - prev_end > 0.35:
            marks.add(round(w0, 2))
        prev_end = w1
    out, last = [], -9.0
    for t in sorted(marks):
        if t - last >= 1.6 and 0.3 < t < dur - 0.3:
            out.append(t)
            last = t
    edges = out + [dur]
    return [(edges[i], edges[i + 1]) for i in range(0, len(edges) - 1, 2)]

def home_url(clip):
    return {"twitch": f"twitch.tv/{clip['channel']}", "kick": f"kick.com/{clip['channel']}",
            "youtube": f"youtube.com/@{clip['channel']}"}.get(clip.get("platform"), clip["url"])

def render(clip, src=None, clean=False):
    day = datetime.fromtimestamp(clip["created"] or time.time(), tz=timezone.utc).strftime("%Y%m%d")
    d = OUT / clip["channel"] / f"{day}_{clip['id']}"
    d.mkdir(parents=True, exist_ok=True)
    words = None
    if src is None:
        src = d / "source.mp4"
        if not src.exists():
            if clip.get("section"):                   # YouTube window: cut on sentence pauses
                words = fetch_section(clip, d, src)
            else:
                log(f"  download {clip['url']}")
                download(clip, src)

    dur = duration(src)
    if talking_head(src):                     # check first: a full-screen webcam also looks like a "facecam"
        raise NotGameplay("streamer's face fills the frame, no gameplay")
    if words is None:
        words = transcribe(src)
    speech = " ".join(t for _, _, t in words)
    if SLUR_RE.search(speech):
        raise NotGameplay("slur in the speech - not worth a strike or a ban")
    (d / "transcript.txt").write_text(speech, encoding="utf-8")
    (d / "mark.txt").write_text(f"@{clip['channel']}", encoding="utf-8")
    orig = src                                         # facecam is found on the uncut source (the edit fooled it)
    src, words, peak, card_at, jumps, hook_t = edit_cut(src, words, d, clip)
    dur = duration(src)
    card_at = min(card_at, dur)
    words = [w for w in words if w[0] < card_at]      # no captions / banana under the score card
    words, mute = mask_swears(words)                  # swears in the first 3 s: F*** and silent
    rating = (int(round(5 + 5 * clip["signal_score"])) if clip.get("platform") == "youtube" and "signal_score" in clip
              else banana_rating(clip.get("views")))
    reactions = pick_reactions(words, card_at, clip["id"], peak) if (BANANA / "reakce1.mp4").exists() else []
    ff = ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)]
    # which accounts get it (docs/dva-ucty.md); each gets its own final encode in its own style,
    # everything above (download, Whisper, cut, hook) and the player detection are done once
    bcfg = brands()
    blist = [brand_cfg(bcfg, n) for n in assign_brands(clip, bcfg)]
    log(f"  brands: {', '.join(b['key'] for b in blist)}")
    _detections.clear()
    whoosh = HERE / "assets" / "sfx" / "flash.wav"
    riser, hit = HERE / "assets" / "sfx" / "riser.wav", HERE / "assets" / "sfx" / "hit.wav"

    # Render, check, and REPAIR instead of giving up (Aleš: a cut face or black bars must be fixed):
    #   1) normal: saved/auto facecam box, detected bars and subtitles cropped
    #   2) facecam detected again with a wider box, bars found with looser limits, 6 % extra zoom
    #   3) only for a clip with viral potential: no facecam band, the game fills the short
    # No facecam = no short, unless the clip is big (Aleš 15. 11.: "pustit i bez kamery, pokud má
    # potenciál být virální"): sources.json nocam_min_views, or several viewers clipped it.
    cfg = load_json(SOURCES, {})
    nocam_ok = (clip.get("views") or 0) >= cfg.get("nocam_min_views", 5000) or (clip.get("cluster") or 1) >= 2
    #   (between 2 and 3) "content": big black areas of any size cut off by the ever-not-black box
    #   (Aleš: a viral clip is a shame to throw away because of a black bar)
    modes = [("saved", False, 0.0), ("again", True, 0.06), ("content", True, 0.03)] + ([("none", True, 0.0)] if nocam_ok else [])
    problems, shorts = [], {}
    for attempt, (cam_mode, loose, zoom) in enumerate(modes):
        cam = (get_cam(clip["channel"], orig) if cam_mode == "saved" else
               detect_cam(orig, mult=3.4) if cam_mode == "again" else
               (get_cam(clip["channel"], orig) or detect_cam(orig, mult=3.4)) if cam_mode == "content" else None)
        if not cam and cam_mode != "none":
            if cam_mode in ("saved", "content") or nocam_ok:
                continue                                  # wider detection / game-only layout next
            raise NotGameplay("no facecam - every short needs the streamer's face on top")
        xlim = None
        if cam_mode == "content":
            x0, y0, x1, y1 = content_box(src)
            top, bot, xlim = y0, SH - y1, (x0, x1)
            log(f"  content box {x0},{y0}-{x1},{y1}: black area cut off")
        else:
            top, bot = game_bars(src, loose=loose)        # black bars inside the game picture
        bot = max(bot, subtitle_band(src, top, bot))      # burned-in subtitles of the source video
        crop_h = SH - top - bot
        cut = int(crop_h * zoom / 2)
        top, crop_h = top + cut, crop_h - 2 * cut
        problems, shorts = [], {}
        for bi, b in enumerate(blist):
            fan = b.get("style", "fanpage" if b["key"] == "fanpage" else "bananas") == "fanpage"
            sfx = "" if b["key"] == "bananas" else f"_{b['key']}"
            out = d / b["short"]
            # fanpage (Aleš 17. 11.: a different edit, not a recolour): taller facecam, one-word
            # captions low in the game band, slow push-in per shot, instant replay of the payoff
            cam_h = 720 if fan else CAM_H
            game_h = H - cam_h if cam else H
            crop_w = round(W * crop_h / game_h)           # source width that fills the game band at full height
            xs, lo, hi, cuts = track_path(src, cam, crop_w, dur, xlim)
            track = "".join(f"{i / FPS:.4f} crop@g x {x};\n" for i, x in enumerate(xs))
            if bi == 0:                                   # unshifted path of the first brand: the clean base
                (d / "track.cmd").write_text(track, encoding="utf-8")
                base_args = (cam, ff_path(d / "track.cmd"), int(xs[0]), crop_w, game_h, crop_h, top)
            shift = float(b.get("crop_shift") or 0.0)
            if shift:                                     # the second account's frame sits elsewhere
                xs = shift_path(xs, shift * crop_w, lo, hi)
                track = "".join(f"{i / FPS:.4f} crop@g x {x};\n" for i, x in enumerate(xs))
            cmds = d / ("track.cmd" if not shift and bi == 0 else f"track{sfx}.cmd")
            cmds.write_text(track, encoding="utf-8")
            ass = d / f"captions{sfx}.ass"
            if fan:   # Anton white, neon green highlight, thin dark outline (sleek, not the banana look)
                n_chunks = captions_ass(words, ass, y=cam_h if cam else 600,     # on the seam (Aleš)
                                        cap={**(b.get("caption") or {}), "outline": 3, "shadow": 0},
                                        per=1, anim="bounce")
            else:
                n_chunks = captions_ass(words, ass, y=CAM_H if cam else 600, cap=b.get("caption"))   # face/game seam
            card = RATING / f"score_{rating}.webm"
            use_card = bool(b.get("score_card")) and card.exists()
            if b.get("score_card"):
                extras_ass(ass, reactions, None if card.exists() else rating, card_at, cam, clip["id"])   # (+ text score)
            rx = reactions if b.get("banana") else []
            args = (cam, ff_path(cmds), int(xs[0]), crop_w, game_h, crop_h, top)
            replay, push = None, None
            main_end = dur if b.get("score_card") else min(dur, card_at + 0.4)   # no card: ends with the story
            if fan:
                r0, r1 = (max(0.0, peak - 1.0), min(card_at, peak + 0.5)) if peak is not None else (0.0, 0.0)
                if False and r1 - r0 >= 0.8:              # replay off (Aleš 17. 11.: looked like the hook again + a freeze)
                    main_end = card_at
                    replay = (main_end, r0, r1)
                edges = sorted({0.0, round(hook_t, 2), *jumps, *(round(c, 2) for c in cuts), round(main_end, 2)})
                push = []
                for e in edges:
                    if e <= main_end and (not push or e - push[-1] >= 0.5):
                        push.append(e)
            total = (card_at + RATING_DUR if use_card else
                     main_end + (replay[2] - replay[1]) / 0.6 if replay else main_end)
            # ending: the brand's outro joined after the clip; the Follow card over the last
            # FOLLOW_DUR s of what is running then (outro / banana score card), never longer
            outro = HERE / b["outro"] if b.get("outro") else None
            outro = outro if outro and outro.exists() else None
            clip_total = total
            total += duration(outro) if outro else 0.0
            follow = HERE / b["follow"] if b.get("follow") and not os.environ.get("KLIPY_NO_FOLLOW") else None
            follow = follow if follow and follow.exists() and total > FOLLOW_DUR + 1 else None
            mpath, mvol, mstart = music_cfg(clip["id"], total, b.get("music"))
            mvol = float(b.get("music_volume", mvol))
            beat = beat_grid(mpath, mstart, total) if mpath and not fan else None
            flt = reaction_overlay(build_filter(
                *args, ass_file=ff_path(ass), zoom_at=None if fan else peak, beat=beat,
                punch=None if fan else zoom_jumps(words, jumps, card_at),
                mark_file=ff_path(d / "mark.txt"), nf=src_norm(src),
                grade=",".join(x for x in (b.get("grade"), FAN_LOOK if fan else "") if x),
                punch_z=float(b.get("punch") or 1.08) - 1, wm_text=b.get("watermark") or BRAND,
                hook=(b.get("hook") or "flash", hook_t) if hook_t else None, cam_h=cam_h, push=push,
                replay=replay), rx, cam)
            inputs, idx = [], 1 + len(rx)
            card_idx = music_idx = sfx_idx = None
            sfx_at = 0.0
            if use_card:
                flt = card_overlay(flt, idx, card_at, max(0.0, card_at + RATING_DUR - dur))
                inputs += ["-c:v", "libvpx-vp9", "-i", str(card)]
                card_idx, idx = idx, idx + 1
            if mpath:                                     # each short starts the track somewhere else
                inputs += ["-ss", f"{mstart:.2f}", "-i", str(mpath)]
                music_idx, idx = idx, idx + 1
            if hook_t and (b.get("hook") or "flash") == "flash" and whoosh.exists():
                inputs += ["-i", str(whoosh)]             # whoosh into a soft boom on the white (impact 0.45 s in)
                sfx_idx, sfx_at = idx, max(0.0, hook_t - 0.45)
            riser_idx = hit_idx = None
            if peak is not None and peak < main_end and riser.exists() and hit.exists():
                inputs += ["-i", str(riser), "-i", str(hit)]
                riser_idx, hit_idx = idx + (sfx_idx is not None), idx + (sfx_idx is not None) + 1
            nxt = idx + (sfx_idx is not None) + (2 if riser_idx is not None else 0)
            outro_idx = follow_idx = None
            if outro:
                flt = outro_append(flt, nxt, clip_total)
                inputs += ["-i", str(outro)]
                outro_idx, nxt = nxt if has_audio(outro) else None, nxt + 1
            if follow:
                # over the outro the card darkens with it (the outro fades to black in its last ~0.45 s)
                fade = f"fade=t=out:st={FOLLOW_DUR - 0.45:.2f}:d=0.45:alpha=1," if outro else None
                flt = card_overlay(flt, nxt, total - FOLLOW_DUR, 0.0, tag="f", fade_out=fade)
                inputs += ["-c:v", "libvpx-vp9", "-i", str(follow)]
                follow_idx = nxt
            mix = audio_mix(has_audio(src), total, card_idx, card_at, music_idx, mvol, sfx_idx, sfx_at, replay,
                            mute, riser_idx, hit_idx, peak, clip_end=clip_total if outro else None,
                            outro_idx=outro_idx, follow_idx=follow_idx, follow_at=total - FOLLOW_DUR)
            maps = inputs + ["-filter_complex", flt + (mix or ""), "-map", "[v]"] + (["-map", "[aud]"] if mix else [])
            if not outro and not use_card and not replay and total < dur - 0.01:
                maps += ["-t", f"{total:.3f}"]
            enc = encode(ff + reaction_inputs(rx) + maps, out)
            problems = [x for x in quality_check(out, cam, cam_h, until=card_at)
                        if not (x == "no facecam" and cam_mode == "none")]
            if problems:
                break
            shorts[b["key"]] = str(out)
            log(f"  {b['key']}: captions {len(words)} words, {n_chunks} chunks; "
                f"banana: {', '.join(f'{n} @{t:.1f}s' for t, n in rx) or 'none'}"
                + (f"; replay {replay[1]:.1f}-{replay[2]:.1f} s" if replay else ""))
        if not problems:
            break
        log(f"  quality check: {'; '.join(problems)} -> repair attempt {attempt + 1}")
    if problems:
        for b in blist:
            (d / b["short"]).unlink(missing_ok=True)
        raise NotGameplay(f"quality check failed after repairs: {'; '.join(problems)}")
    base = d / "base_9x16.mp4"               # clean, no captions: input for the editor (OpenMotion)
    if clean:
        encode(ff + ["-filter_complex", build_filter(*base_args, nf=src_norm(src)), "-map", "[v]", "-map", "0:a?"], base)
    out = Path(shorts.get("bananas") or next(iter(shorts.values())))

    meta = {
        **clip,
        "cam": cam,
        "layout": "cam+game" if cam else "game",
        "encoder": enc,
        "short": str(out),                   # bananas (or fanpage when the clip is only theirs)
        "shorts": shorts,                    # brand -> its own version (publikuj.py den --znacka)
        "brands": list(shorts),
        "style": "fanpage" if "fanpage" in shorts else "bananas",
        "base": str(base) if clean else None,
        "source": str(src),
        "quote": pick_quote(words, peak),    # publikuj.py builds a different post text per platform from it
        "peak": peak,                        # payoff (s): thumbnail frame, banana, zoom
        "card_at": round(card_at, 2),        # where the banana score card starts
        "rating": rating,
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
    for f in {STATE, HERE / "state.json"}:             # own state + what the repo already knows
        state = load_json(f, {})
        if (clip_id in state.get("done", {}) or clip_id in state.get("other_game", {})
                or state.get("failed", {}).get(clip_id, 0) >= MAX_TRIES):
            return True
    return False

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
    if clip.get("platform") == "youtube" or clip.get("category"):   # YouTube title / Twitch API category
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
            if has_moment(c):                    # other viewers' clips of this moment: skip them later
                save_moment(c)
            n += 1
        except NotGameplay as e:                 # right category, wrong picture: never again
            if "no facecam" in str(e):           # detection can miss once: counts as a failure (max 3 tries)
                mark(c["id"], False)
                log(f"  skip: {e}")
                continue
            state = load_json(STATE, {"done": {}, "failed": {}})
            state.setdefault("other_game", {})[c["id"]] = str(e)
            save_json(STATE, state)
            log(f"  skip: {e}")
        except Exception as e:  # one broken clip must not stop launch day
            mark(c["id"], False)
            log(f"  FAILED {c['url']}: {e}")
    return n

def virality(c):
    """
    Views per hour of age (softened): what is spreading right now beats an old slow climber.
    Boosted by measured growth since the last run (views/h relative, capped 3x) and by how
    many viewers clipped the same moment (cluster). publikuj.candidates uses the same formula.
    """
    age_h = (time.time() - c["created"]) / 3600 if c.get("created") else 24
    base = (c.get("views") or 0) / (max(age_h, 0) + 1) ** 0.5
    return base * (1 + min(c.get("growth") or 0, 3)) * (1 + 0.5 * ((c.get("cluster") or 1) - 1))

def cluster(clips):
    """Several viewers clipping the same 30 s of the same stream = one moment, a stronger one."""
    groups = []
    for c in sorted(clips, key=lambda c: -c["views"]):
        if c.get("video_id") and c.get("vod_offset") is not None:
            g = next((g for g in groups if g[0].get("video_id") == c["video_id"]
                      and g[0].get("vod_offset") is not None and abs(g[0]["vod_offset"] - c["vod_offset"]) < 30), None)
            if g:
                g.append(c)
                continue
        groups.append([c])
    for g in groups:
        g[0]["cluster"] = len(g)
    return [g[0] for g in groups]

def has_moment(c):
    return bool(c.get("video_id")) and c.get("vod_offset") is not None

def moment_done(c):
    """The same stream moment (same VOD, < 30 s apart) already rendered from another viewer's clip."""
    if not has_moment(c):
        return False
    seen = load_json(STATE, {}).get("moments", {}).get(c["video_id"], [])
    return any(abs(off - c["vod_offset"]) < 30 for _, off in seen)

def save_moment(c):
    state = load_json(STATE, {"done": {}, "failed": {}})
    now = int(time.time())
    m = {v: [x for x in xs if now - x[0] < 3 * 86400] for v, xs in state.get("moments", {}).items()}
    m.setdefault(c["video_id"], []).append([now, c["vod_offset"]])
    state["moments"] = {v: xs for v, xs in m.items() if xs}
    save_json(STATE, state)

def add_growth(clips):
    """Views growth since the previous run (saved per clip in state 'views', kept 2 days)."""
    state = load_json(STATE, {"done": {}, "failed": {}})
    snap, now = state.get("views", {}), time.time()
    for c in clips:
        prev = snap.get(c["id"])
        if prev and 600 <= now - prev[0] <= 4 * 3600:
            c["growth"] = round(max(0.0, (c["views"] - prev[1]) / max(prev[1], 50) / ((now - prev[0]) / 3600)), 3)
        snap[c["id"]] = [int(now), c["views"]]
    state["views"] = {k: v for k, v in snap.items() if now - v[0] < 2 * 86400}
    save_json(STATE, state)

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
    listers = (("twitch", lambda ch: list_twitch(ch, "24hr" if a.days <= 1 else "7d")), ("kick", list_kick))
    for platform, lister in () if a.jen_yt else listers:
        for ch in src.get(platform, []):
            if ch.lower() in blocked:
                continue
            try:
                clips = [c for c in fresh(lister(ch), a.days) if c["views"] >= a.min_views and not skip(c["id"])]
            except Exception as e:                             # one dead channel must not stop the day
                log(f"{platform} {ch}: {str(e)[-150:]}")
                continue
            live += clips[: a.n]
    if not a.jen_yt and not all(helix_keys()):
        log("twitch api: no TWITCH_CLIENT_ID/SECRET - only the channel list, not the whole GTA VI category")
    if not a.jen_yt and all(helix_keys()):
        try:                                               # every streamer in the category, not just our list
            seen, per = {c["id"] for c in live}, {}
            for c in live:
                per[c["channel"]] = per.get(c["channel"], 0) + 1
            for c in cluster(list_twitch_game(max(a.days, 1))):
                if (c["id"] in seen or c["channel"] in blocked or c["views"] < a.min_views
                        or per.get(c["channel"], 0) >= a.n or skip(c["id"]) or moment_done(c)):
                    continue
                per[c["channel"]] = per.get(c["channel"], 0) + 1
                live.append(c)
        except Exception as e:
            log(f"twitch api: {str(e)[-150:]}")
    add_growth(live)
    live.sort(key=virality, reverse=True)
    yt = []
    if a.yt:
        def chan(handle):
            try:
                return youtube_clips(handle, a.days, a.per_video)
            except Exception as e:
                log(f"youtube {handle}: {str(e)[-150:]}")
                return []
        handles = [h for h in src.get("youtube", []) if h.lstrip("@").lower() not in blocked]
        with ThreadPoolExecutor(6) as ex:                     # network-bound: 6 channels at once
            yt += [c for r in ex.map(chan, handles) for c in r if not skip(c["id"])]
        try:
            seen = {c["id"].split("_")[1] for c in yt}
            queries = [q for q in src.get("youtube_search", []) if not (launched() and "trailer" in q.lower())]
            yt += [c for c in youtube_search_clips(queries, max(a.days, 2), a.per_video,
                                                   src.get("youtube_min_followers", 300000), seen)
                   if not skip(c["id"]) and c["channel"] not in blocked]
        except Exception as e:
            log(f"youtube search: {str(e)[-150:]}")
        yt.sort(key=virality, reverse=True)
    log(f"candidates: {len(live)} twitch/kick, {len(yt)} youtube -> rendering up to {a.max}")
    # viewer-made clips first (a person picked the moment); YouTube windows only fill the rest
    total = process(live, a.clean, limit=a.max)
    total += process(yt, a.clean, limit=min(a.yt, a.max - total))
    log(f"batch done: {total} new shorts")
    if total == 0 and os.environ.get("GITHUB_ACTIONS") and datetime.now().date() >= date(2026, 11, 19):
        print("::warning::Dnes se nevyrenderoval zadny GTA 6 klip (zadne cerstve klipy, nebo blokace Twitch/Kick).")

def cookie_alarm(err):
    """YouTube asks for sign-in again = the cookies expired: say it loudly on GitHub (e-mail via ::warning)."""
    if any(k in err for k in ("confirm your age", "not a bot", "cookies are no longer valid")):
        print("::warning::YouTube cookies uz neplati - vyexportuj nove (anonymni okno) a nahraj secret YT_COOKIES.")

def cmd_yt(a):
    """
    One long YouTube video (e.g. a streamer's GTA 6 trailer reaction) -> its moments that
    the audience marked (heatmap, timestamps in comments, chat replay), rendered like clips.
    Runs from a home PC only: YouTube blocks GitHub's servers.
    """
    for url in a.urls:
        try:
            info = ytdlp_json(url, timeout=120)
        except Exception as e:                        # age-gated, private, members-only ...
            err = re.search(r"ERROR:[^\n]*", str(e))
            log(f"skip {url}: {(err.group(0) if err else str(e))[:300]}")
            cookie_alarm(str(e))
            continue
        vid, dur = info["id"], float(info.get("duration") or 0)
        about = " ".join([info.get("title") or ""] + (info.get("tags") or []))
        cands, how = viewer_signals(url, vid, info)
        log(f"{info.get('channel')}: '{(info.get('title') or '')[:60]}' -> {len(cands)} audience peaks ({how})")
        was_live = info.get("was_live") or info.get("live_status") == "was_live"
        clips = []
        for s, e, _ in pick_windows(cands, dur, a.n, skip_start=150 if was_live else 15):
            clips.append({
                "id": f"youtube_{vid}_{int(s)}", "url": url, "title": info.get("title") or "",
                "views": int(info.get("view_count") or 0), "duration": e - s,
                "created": int(info.get("timestamp") or 0),
                "channel": (info.get("uploader_id") or info.get("channel") or "youtube").lstrip("@").lower(),
                "platform": "youtube", "section": [round(s, 1), round(e, 1)], "signal": how, "category": about,
            })
        process(clips, a.clean)

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

# -----------------------------------------------------------------------------
# live: chat explosion on a running GTA VI stream -> the last ~40 s from its live VOD -> short

LAUGH_IRC = re.compile(r"KEKW|OMEGALUL|LUL|LMAO|ICANT|💀|😂|\bW\b|\?\?\?|HOLY|NO WAY|WTF", re.I)

class ChatMeter:
    """
    Anonymous, read-only Twitch chat (irc.chat.twitch.tv, nick justinfan*, no account): messages
    per 5 s for every joined channel, laughs / hype emotes count double.
    """
    def __init__(self):
        import socket, threading
        self.sock = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=10)
        self.sock.send(f"NICK justinfan{random.randint(10000, 99999)}\r\n".encode())
        self.counts, self.joined, self.lock = {}, set(), threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def join(self, logins):
        for lg in set(logins) - self.joined:
            self.sock.send(f"JOIN #{lg}\r\n".encode())
            self.joined.add(lg)
        for lg in self.joined - set(logins):
            self.sock.send(f"PART #{lg}\r\n".encode())
        self.joined = set(logins)

    def _read(self):
        buf = b""
        while True:
            try:
                data = self.sock.recv(65536)
            except Exception:
                continue
            if not data:
                return
            buf += data
            *lines, buf = buf.split(b"\r\n")
            for ln in lines:
                if ln.startswith(b"PING"):
                    self.sock.send(b"PONG :tmi.twitch.tv\r\n")
                    continue
                if b" PRIVMSG #" not in ln:
                    continue
                head, _, text = ln.decode("utf-8", "replace").partition(" :")
                chan = head.rsplit("#", 1)[-1].strip()
                b5 = int(time.time() // 5)
                w = 2 if LAUGH_IRC.search(text.split(" :", 1)[-1]) else 1
                with self.lock:
                    c = self.counts.setdefault(chan, {})
                    c[b5] = c.get(b5, 0) + w

    def spike(self, chan, z_min, cool):
        """(z, msgs) when the last finished 5 s bin explodes over the channel's last 5 minutes."""
        now = int(time.time() // 5) - 1
        with self.lock:
            c = self.counts.get(chan, {})
            for old in [k for k in c if k < now - 80]:
                del c[old]
            hist = [c.get(i, 0) for i in range(now - 60, now)]
            cur = c.get(now, 0)
        if sum(1 for x in hist if x) < 20:            # not enough chat history yet
            return None
        med = float(np.median(hist))
        mad = float(np.median([abs(x - med) for x in hist])) + 1
        z = (cur - med) / mad
        return (round(z, 1), cur) if z >= z_min and cur >= max(8, 3 * med) else None

def vod_section(login, user_id, started_at, t_spike, before=32, after=6):
    """The window around a chat spike from the running stream's live VOD (no login needed)."""
    vids = helix("videos", {"user_id": user_id, "type": "archive", "first": 1}).get("data") or []
    if not vids or vids[0]["created_at"][:13] < started_at[:13]:
        return None                                   # VODs off for this channel
    start = datetime.fromisoformat(started_at.replace("Z", "+00:00")).timestamp()
    off = t_spike - start
    return f"https://www.twitch.tv/videos/{vids[0]['id']}", max(0.0, off - before), off + after

def cmd_live(a):
    """
    Launch conveyor belt (Aleš: be the fastest, clip it right away): every 2 min the biggest
    live English streams in the GTA VI category; their chats are watched live; a chat explosion
    -> ~70 s later (the live VOD lags about a minute) the window 32 s before .. 6 s after the
    spike is cut from the running stream (the payoff is often ~20 s before the chat reacts),
    rendered, put on the live-inbox (free Netlify draft deploy) and the GitHub publish run is
    started, so the hot path can post it within ~25 min.
    """
    cfg = load_json(SOURCES, {})
    start_at = cfg.get("live_from", "2026-11-17T23:00:00Z")
    if datetime.now(timezone.utc) < datetime.fromisoformat(start_at.replace("Z", "+00:00")) and not a.ted:
        log(f"live: starts {start_at} (use --ted to test now)")
        return
    if not all(helix_keys()):
        sys.exit("live: TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET missing")
    game_name = a.hra or cfg.get("twitch_game", "Grand Theft Auto VI")
    games = helix("games", {"name": game_name}).get("data") or []
    if not games:
        sys.exit(f"live: category '{game_name}' not found")
    blocked = {b.lower() for b in cfg.get("blocklist", [])}
    meter, streams, queue, cooldown = ChatMeter(), {}, [], {}
    end, next_poll, made = time.time() + a.hours * 3600, 0.0, 0
    log(f"live: watching '{games[0]['name']}' for {a.hours} h")
    while time.time() < end:
        if time.time() >= next_poll:                  # who is live right now
            try:
                data = helix("streams", {"game_id": games[0]["id"], "first": 50}).get("data") or []
                top = [x for x in data if (x.get("language") or "en").startswith("en")
                       and x["viewer_count"] >= a.min_viewers and x["user_login"].lower() not in blocked][: a.n_streams]
                streams = {x["user_login"].lower(): x for x in top}
                meter.join(list(streams))
                log(f"live: {len(streams)} streams: " + ", ".join(f"{k} {v['viewer_count']}" for k, v in list(streams.items())[:8]))
            except Exception as e:
                log(f"live: stream list failed ({str(e)[-100:]})")
            next_poll = time.time() + 120
        for lg in list(streams):                      # chat explosions
            if time.time() < cooldown.get(lg, 0):
                continue
            hit = meter.spike(lg, a.z, a.cool)
            if hit:
                cooldown[lg] = time.time() + a.cool
                queue.append((time.time() - 2.5, lg, streams[lg], hit))
                log(f"live: SPIKE {lg} z={hit[0]} msgs={hit[1]} -> cut in ~70 s")
        while queue and time.time() >= queue[0][0] + 70:   # the live VOD has caught up
            t_spike, lg, st, (z, msgs) = queue.pop(0)
            try:
                sec = vod_section(lg, st["user_id"], st["started_at"], t_spike)
                if not sec:
                    log(f"live: {lg} has no live VOD, skipped")
                    continue
                url, s0, s1 = sec
                clip = {"id": f"live_{lg}_{int(t_spike)}", "url": url, "title": st.get("title") or "",
                        "views": int(st["viewer_count"]), "duration": s1 - s0, "created": int(t_spike),
                        "channel": lg, "platform": "twitch", "category": games[0]["name"],
                        "live": True, "spike_z": z, "cluster": 2}     # a chat explosion = a strong moment
                d = OUT / lg / f"{datetime.fromtimestamp(clip['created'], tz=timezone.utc):%Y%m%d}_{clip['id']}"   # = render()'s folder
                d.mkdir(parents=True, exist_ok=True)
                src = d / "source.mp4"
                run(["yt-dlp", "-q", "-f", "best[height<=1080]/best", "--download-sections", f"*{s0:.0f}-{s1:.0f}",
                     "--force-overwrites", "-o", str(src), url], timeout=300)
                render(clip, src=src)
                mark(clip["id"], True)
                made += 1
                log(f"live: short #{made} from {lg}")
                hand_over_live()
            except NotGameplay as e:
                log(f"live: {lg} skipped: {e}")
            except Exception as e:
                log(f"live: {lg} failed: {str(e)[-200:]}")
        time.sleep(5)
    log(f"live: done, {made} shorts")

def hand_over_live():
    """Put the live shorts on the live-inbox and start the GitHub publish run right away."""
    subprocess.run([sys.executable, str(HERE / "publikuj.py"), "schranka", "--nazev", "live-inbox", "--prefix", "live_"],
                   timeout=900)
    tok, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY", "aleshrani/gta6bananaclips")
    if not tok:
        return
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/actions/workflows/denne.yml/dispatches",
                                 data=b'{"ref":"main","inputs":{"jen_publikace":"ano"}}', method="POST",
                                 headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
    try:
        urllib.request.urlopen(req, timeout=30)
        log("live: publish run started")
    except Exception as e:
        log(f"live: could not start the publish run ({e})")

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
    b.add_argument("--jen-yt", action="store_true", help="only YouTube (the worker PC; GitHub does Twitch + Kick)")
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

    y = sub.add_parser("yt", help="moments from long YouTube videos (home PC only)")
    y.add_argument("urls", nargs="+")
    y.add_argument("--n", type=int, default=3, help="moments per video")
    y.set_defaults(fn=cmd_yt)

    c = sub.add_parser("cam", help="detect the facecam box of a streamer and save it")
    c.add_argument("channel")
    c.add_argument("source", help="clip URL or local video file")
    c.set_defaults(fn=cmd_cam)

    lv = sub.add_parser("live", help="launch: live chat spikes of GTA VI streams -> instant shorts")
    lv.add_argument("--hours", type=float, default=5.7, help="how long to watch (GitHub job limit is 6 h)")
    lv.add_argument("--streamu", dest="n_streams", type=int, default=15, help="watch the chat of the N biggest streams")
    lv.add_argument("--min-viewers", type=int, default=1500)
    lv.add_argument("--z", type=float, default=6.0, help="how far the chat must jump over its last 5 min")
    lv.add_argument("--cool", type=int, default=180, help="seconds between two spikes of one channel")
    lv.add_argument("--hra", help="other Twitch category (test before the launch, e.g. 'Grand Theft Auto V')")
    lv.add_argument("--ted", action="store_true", help="start now even before live_from (test)")
    lv.set_defaults(fn=cmd_live)
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
