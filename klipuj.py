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
import math
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

class CorruptJson(RuntimeError):
    """A state/config file did not parse: kept as <name>.bad, nothing is written over it."""

def _strict_json(path):
    """Files whose silent default would be written back and wipe them (state, facecams, sources, brands)."""
    try:
        p = Path(path).resolve()
        return p in {Path(x).resolve() for x in (STATE, STREAMERS, SOURCES, BRANDS, HERE / "state.json")}
    except OSError:
        return False

def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        if not _strict_json(path):
            return default
        # a silent default here would be saved back by the next mark()/save_json and lose the whole
        # file (done clips, facecam boxes, sources): keep a copy and stop loudly instead
        bad = Path(path).with_name(Path(path).name + ".bad")
        try:
            shutil.copy(path, bad)
        except OSError:
            pass
        raise CorruptJson(f"{Path(path).name} is corrupt ({e}); copy kept as {bad.name}, nothing overwritten")

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
    bad = (load_json(SOURCES, {}).get("exclude_titles") or "").strip().strip("|")
    # only non-empty alternatives: "" + "|trailer" would be an empty branch that matches every title
    return "|".join(p for p in [bad] + (["trailer", "extended look", "showcase"] if launched() else []) if p)

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

# A real facecam is a STATIC overlay rectangle: same spot and size the whole clip, and what is inside
# it (the streamer's room) does not move with the game. A face of a GTA character in a close-up can
# also sit still for a while (garek, 17. 11.: an RP conversation went into the top band as "webcam"),
# but it moves with the scene: when the game camera pans or cuts, the pixels around that face change
# together with the rest of the frame. So a box is only a facecam when, on the samples where the game
# clearly moved, the room around the face stayed put. Low confidence = no facecam.
CAM_SAMPLES = 24        # frames over the whole clip
CAM_MIN_FRAC = 0.25     # face at that spot on at least this share of the samples ...
CAM_MIN_MOVING = 4      # ... and the game must have moved on at least this many sample pairs to judge
CAM_DECOUPLED = 0.6     # share of those pairs on which the room around the face stayed still
CAM_ALIVE = 5.0         # median change of the face pixels between samples: webcams 11-26, avatar 0.6, posters 2-2.6
_cam_cache = {}

def cam_samples(src, n=CAM_SAMPLES):
    """
    ~n frames spread over the whole clip: gray 960x540 (for motion) and Haar faces found on the full
    1920x1080 picture (a small webcam face is ~26 px at 960 wide and Haar missed it), in 960x540 px.
    Cached per file: render asks several times (saved box, wider box, content box).
    """
    global _cascade
    key = (str(src), os.path.getmtime(src))
    if key in _cam_cache:
        return _cam_cache[key]
    if _cascade is None:
        _cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    grays, faces = [], []
    for _, img in frames(src, n / max(duration(src), 1.0), SW, SH):
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        found = _cascade.detectMultiScale(g, scaleFactor=1.1, minNeighbors=5, minSize=(44, 44))
        faces.append([tuple(v / 2 for v in f) for f in found])
        grays.append(cv2.resize(g, (960, 540), interpolation=cv2.INTER_AREA).astype(np.float32))
    _cam_cache.clear()
    _cam_cache[key] = (np.stack(grays) if grays else np.zeros((0, 540, 960), np.float32), faces)
    return _cam_cache[key]

def cam_evidence(grays, faces, hits):
    """
    Is the face cluster `hits` ({sample index: (x, y, w, h)} in 960x540 px) a webcam in the overlay?
    Returns ("yes" | "no" | "unsure", reason).
    """
    n = len(faces)
    if not n or len(hits) < max(3, CAM_MIN_FRAC * n):
        return "no", f"face on {len(hits)}/{n} samples"
    idx = sorted(hits)
    if len({i * 3 // n for i in idx}) < 2:                   # only in one third of the clip: a passing face
        return "no", "face only in one part of the clip"
    arr = np.array([hits[i] for i in idx], np.float32)
    fx, fy, fw, fh = (float(v) for v in np.median(arr, axis=0))
    cx, cy = fx + fw / 2, fy + fh / 2
    size_cv = float(arr[:, 2].std() / max(arr[:, 2].mean(), 1))
    pos_sd = float(max((arr[:, 0] + arr[:, 2] / 2).std(), (arr[:, 1] + arr[:, 3] / 2).std()))
    # (a streamer leans and turns inside his webcam; the strong test is the motion one below)
    if size_cv > 0.25:
        return "no", f"face size changes (cv {size_cv:.2f})"
    if pos_sd > max(8.0, 0.3 * fw):
        return "no", f"face moves around ({pos_sd:.0f} px)"
    if 0.3 * 960 < cx < 0.7 * 960 and 0.3 * 540 < cy < 0.7 * 540:
        return "no", "face in the middle of the picture (cutscene / close-up)"
    # the room around the face (inside the webcam: wall behind the streamer), in 4 patches: above,
    # left, right of the head and below the chin. The streamer's own hair/hands may move one or two of
    # them, so per sample pair the 2nd stillest patch counts; a game camera move changes all four.
    def patch(y0, y1, x0, x1):
        y0, y1, x0, x1 = int(max(0, y0)), int(min(540, y1)), int(max(0, x0)), int(min(960, x1))
        return (y0, y1, x0, x1) if (y1 - y0) * (x1 - x0) >= 60 else None
    sides = [p for p in (patch(fy - 0.6 * fh, fy - 0.05 * fh, fx - fw, fx + 2 * fw),
                         patch(fy - 0.6 * fh, fy + 1.8 * fh, fx - fw, fx - 0.15 * fw),
                         patch(fy - 0.6 * fh, fy + 1.8 * fh, fx + 1.15 * fw, fx + 2 * fw),
                         patch(fy + 1.2 * fh, fy + 1.8 * fh, fx - fw, fx + 2 * fw)) if p]
    game = np.ones((540, 960), bool)                         # the rest of the frame, well away from the cam
    game[int(max(0, fy - 2 * fh)):int(min(540, fy + 4 * fh)), int(max(0, fx - 3 * fw)):int(min(960, fx + 4 * fw))] = False
    if len(sides) < 2 or game.sum() < 20000:
        return "unsure", "box too big to compare with the game"
    d = np.abs(np.diff(grays, axis=0))
    # two still patches needed (one flat sky / HUD next to an NPC must not be enough)
    per = np.sort([d[:, y0:y1, x0:x1].mean(axis=(1, 2)) for y0, y1, x0, x1 in sides], axis=0)
    c_ring = per[1] if len(sides) >= 3 else per[0]
    c_game = d[:, game].mean(axis=1)
    # alive: a streamer talks, blinks, turns - the face pixels change. A profile picture in a
    # browser tab, an avatar or a HUD icon Haar took for a face is static AND still "decoupled".
    y0, y1, x0, x1 = int(max(0, fy)), int(min(540, fy + fh)), int(max(0, fx)), int(min(960, fx + fw))
    alive = float(np.median(d[:, y0:y1, x0:x1].mean(axis=(1, 2)))) if y1 > y0 and x1 > x0 else 0.0
    if alive < CAM_ALIVE:
        return "no", f"face does not move ({alive:.1f}) - a picture, not a webcam"
    moving = c_game > 8                                      # the game clearly changed between samples
    if moving.sum() < CAM_MIN_MOVING:
        return "unsure", f"game too still to tell ({int(moving.sum())} moving samples)"
    dec = float(np.mean(c_ring[moving] < 0.5 * c_game[moving]))   # test clips: webcams 67-96 %, in-game faces <= 38 %
    if dec < CAM_DECOUPLED:
        return "no", f"moves with the game ({dec:.0%} of {int(moving.sum())} moving samples still) - in-game face"
    return "yes", f"static box, face {len(hits)}/{n} alive {alive:.1f}, still while the game moved {dec:.0%}"

def face_clusters(faces):
    """Face hits grouped by spot (centres within 25 px), biggest group first, one group per spot."""
    hits = [(i, f) for i, found in enumerate(faces) for f in found]
    groups = []
    for _, (x, y, w, h) in hits:
        cx, cy = x + w / 2, y + h / 2
        agree = {}
        for i, f in hits:
            if abs(f[0] + f[2] / 2 - cx) < 25 and abs(f[1] + f[3] / 2 - cy) < 25:
                if i not in agree or abs(f[0] - x) < abs(agree[i][0] - x):
                    agree[i] = f
        groups.append(agree)
    groups.sort(key=len, reverse=True)
    out, spots = [], []
    for g in groups:
        m = np.median(np.array(list(g.values())), axis=0)
        spot = (m[0] + m[2] / 2, m[1] + m[3] / 2)
        if all(abs(spot[0] - s[0]) > 25 or abs(spot[1] - s[1]) > 25 for s in spots):
            spots.append(spot)
            out.append(g)
    return out

def detect_cam(src, mult=3.0):
    """
    Find the facecam box: among the spots where a face repeats, the first (biggest) one that passes
    cam_evidence. Then grow the median face into a box with the aspect of the top band.
    Returns [x, y, w, h] in percent of the frame, or None (also when unsure).
    """
    grays, faces = cam_samples(src)
    best, why = None, []
    for hits in face_clusters(faces)[:6]:
        verdict, reason = cam_evidence(grays, faces, hits)
        why.append(reason)
        if verdict == "yes":
            best = list(hits.values())
            break
    if best is None:
        log(f"  facecam: none ({'; '.join(why[:3]) or 'no face'})")
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
    log(f"  facecam: {why[-1]}")
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

def box_evidence(src, cam):
    """cam_evidence for a saved box: the faces whose centre is inside it."""
    grays, faces = cam_samples(src)
    bx, by, bw, bh = cam[0] * 9.6, cam[1] * 5.4, cam[2] * 9.6, cam[3] * 5.4
    hits = {}
    for i, found in enumerate(faces):
        inside = [f for f in found if bx <= f[0] + f[2] / 2 <= bx + bw and by <= f[1] + f[3] / 2 <= by + bh]
        if inside:
            hits[i] = max(inside, key=lambda f: f[2])
    return cam_evidence(grays, faces, hits) + (len(hits), len(faces))

def face_in_box(src, cam):
    """Kept for callers outside: is the saved box a real facecam on this clip?"""
    return box_evidence(src, cam)[0] == "yes"

def get_cam(channel, src):
    """
    Facecam box for this clip. A hand-set box is trusted. An auto box (streamers.json "auto": true)
    is re-validated on EVERY clip - a wrong cached box must not survive:
      yes    -> used, and counted as proven ("ok": n)
      unsure -> (game too still to judge) used only if this box was proven on an earlier clip and
                the face is in it on most samples of this one
      no     -> dropped, detection again (and nothing at all when that is not sure either)
    """
    db = load_json(STREAMERS, {})
    cfg = db.get(channel, {})
    if cfg.get("no_cam"):
        return None
    if cfg.get("cam") and not cfg.get("auto"):
        return cfg["cam"]
    wrong = False
    if cfg.get("cam"):
        verdict, reason, k, n = box_evidence(src, cfg["cam"])
        # proven not a webcam (moves, changes size, centre, moves with the game); a face simply missing
        # on this clip (streamer away, just chatting) keeps the box for the next one
        wrong = verdict == "no" and not reason.startswith("face on") and "one part" not in reason
        if verdict == "yes" or (verdict == "unsure" and cfg.get("ok", 0) >= 1 and n and k >= 0.5 * n):
            if verdict == "yes":
                db = load_json(STREAMERS, {})
                db[channel] = {**cfg, "ok": cfg.get("ok", 0) + 1}
                save_json(STREAMERS, db)
            return cfg["cam"]
        log(f"  saved facecam of {channel} not confirmed on this clip ({reason})")
    cam = detect_cam(src)
    db = load_json(STREAMERS, {})
    if cam:
        db[channel] = {"cam": cam, "auto": True, "ok": 1}
        log(f"  facecam auto-detected for {channel}: {cam} (check it, then remove 'auto')")
    elif wrong and db.get(channel, {}).get("auto"):
        db.pop(channel)                           # the cached guess is an in-game face: never again
        log(f"  saved facecam of {channel} removed from streamers.json")
    save_json(STREAMERS, db)
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
            # a bar ends in a visible edge: the picture right after it is clearly brighter. A night scene
            # that fades into black (the fanpage vignette crushes its bottom rows to 0) is not a bar
            # (17. 11. audit: every dark fanpage clip failed QA twice and was thrown away)
            nxt = (g[i: i + 10] if step > 0 else g[max(0, i - 9): i + 1]) if 0 <= i < g.shape[0] else g[:0]
            if run and nxt.size and nxt.mean() < 12:
                run = 0
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

class RenderFailed(NotGameplay):
    """The short could not be made good enough this time (boring, QA, quality, cuts). Not a property
    of the stream category: counted as a failed try (MAX_TRIES), never written to other_game -
    a render bug must not lose a good clip forever."""

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

# Gameplay gate (18. 11., launch day: a fanpage short of DarkViperAU talking about GT5 - his face
# full-frame, then his YouTube channel page, no game at all - passed boredom and QA). talking_head
# wants ONE big still person on >= 60 % of the frames: a video that switches between a face cam and a
# web page never reaches that. This gate looks at what the game band would show, per window of the
# clip, at 2 fps: a big face (Haar, the streamer's camera full-frame), a static screen (browser,
# YouTube page, desktop, menu: most pixels unchanged, like kategorie.static_share) and no motion.
GP_FPS = 2.0
GP_WIN = 6.0             # s per window: each window is game / not game
GP_FACE_W = 0.14         # a face this wide (share of the band's width) = a camera, not a facecam overlay
GP_FACE_MAX = 0.30       # big face on more than this share of the samples = not gameplay
GP_STATIC_PX = 4.0       # pixel std (0-255) under this over a window = the pixel did not change
GP_STATIC_MAX = 0.5      # window with more unchanged pixels than this = static screen
GP_STILL = 0.8           # median picture change (0-255, 2 fps) under this = nothing moves
GP_BAD_MAX = 0.4         # more than this share of the windows not gameplay = not gameplay

def gameplay_check(path, a=None, b=None, crop=None, mask=None):
    """
    Is [a, b] of `path` gameplay of a 3D game? crop = ffmpeg crop of the game band ('w:h:x:y', the
    finished short), mask = facecam box in % of the frame (x, y, w, h) left out of the static / motion
    measure (a webcam background stands still). Returns (problems, numbers); problems name 'not gameplay'.
    """
    global _cascade
    if _cascade is None:
        _cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    W_, H_ = 640, 360
    ss = ["-ss", f"{a:.3f}"] if a else []
    tt = ["-t", f"{b - (a or 0):.3f}"] if b else []
    vf = (f"crop={crop}," if crop else "") + f"fps={GP_FPS},scale={W_}:{H_},format=gray"
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", *ss, *tt, "-i", str(path), "-vf", vf,
                        "-f", "rawvideo", "-"], capture_output=True, timeout=600)
    raw = r.stdout
    n = len(raw) // (W_ * H_)
    if r.returncode != 0 or n < 4:                           # a failed probe is never a pass (fail closed)
        raise RuntimeError(f"gameplay probe failed (ffmpeg {r.returncode}, {n} frames): "
                           f"{r.stderr.decode('utf-8', 'replace').strip()[-200:]}")
    fr = np.frombuffer(raw[: n * W_ * H_], np.uint8).reshape(n, H_, W_)
    keep = np.ones((H_, W_), bool)
    if mask:
        x0, y0 = int(mask[0] / 100 * W_), int(mask[1] / 100 * H_)
        keep[y0: y0 + int(mask[3] / 100 * H_) + 1, x0: x0 + int(mask[2] / 100 * W_) + 1] = False
    face = np.zeros(n, bool)
    for i in range(n):
        found = _cascade.detectMultiScale(fr[i], scaleFactor=1.1, minNeighbors=6,
                                          minSize=(int(GP_FACE_W * W_), int(GP_FACE_W * W_)))
        face[i] = len(found) > 0
    small = fr[:, ::4, ::4].astype(np.float32)               # 160x90 for static / motion
    km = keep[::4, ::4]
    motion = np.abs(np.diff(small, axis=0))[:, km].mean(axis=1) if n > 1 else np.zeros(1)
    per = max(2, int(GP_WIN * GP_FPS))
    wins = []
    for s in range(0, n, per):
        seg = small[s: s + per]
        if len(seg) < max(3, per // 2) and wins:
            break                                            # a short tail is judged with the window before
        static = float((seg.std(axis=0)[km] < GP_STATIC_PX).mean())
        mo = float(np.median(motion[s: s + per - 1])) if len(motion[s: s + per - 1]) else 0.0
        fc = float(face[s: s + per].mean())
        bad = static > GP_STATIC_MAX or fc > 0.5 or mo < GP_STILL
        wins.append({"static": round(static, 2), "motion": round(mo, 1), "face": round(fc, 2), "bad": bad})
    face_share = float(face.mean())
    bad_share = float(np.mean([w["bad"] for w in wins]))
    med_motion = float(np.median(motion))
    probs = []
    if face_share > GP_FACE_MAX:
        probs.append(f"not gameplay: a big face on {face_share:.0%} of the frames (a camera, not the game)")
    if bad_share > GP_BAD_MAX:
        why = sorted({k for w in wins if w["bad"] for k, v in (("static screen", w["static"] > GP_STATIC_MAX),
                                                                ("face", w["face"] > 0.5),
                                                                ("no motion", w["motion"] < GP_STILL)) if v})
        probs.append(f"not gameplay: {bad_share:.0%} of the clip is {', '.join(why)}")
    if med_motion < GP_STILL:
        probs.append(f"not gameplay: nothing moves (median change {med_motion:.1f})")
    return probs, {"face": round(face_share, 2), "bad": round(bad_share, 2), "motion": round(med_motion, 1),
                   "windows": wins}

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

_detections = {}   # (source, size, mtime, cam) -> player positions; both brands' crops reuse one detection pass

def track_path(src, cam, crop_w, dur, xlim=None):
    """
    Per-output-frame crop x (left edge, source pixels) that follows the player.
    Returns (xs, lo, hi, cuts): the path, the allowed range of x, the cuts in the source (s).
    """
    global _yolox
    # keyed by the file's identity too: the old-look re-cut is a new file (a stale detection of the
    # first cut would steer the crop to where the player was in a different edit)
    st = Path(src).stat()
    key = (str(src), st.st_size, st.st_mtime_ns, tuple(cam) if cam else None)
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
BUSY = "~"               # text of a word Whisper heard but we dropped (no caption) - still nobody cuts into it

class Words(list):
    """Whisper's caption words [(start, end, text)]; .busy = the words of segments dropped from the
    captions (doubtful, repetitive, sung) as (start, end, BUSY): real sound someone may be saying, so
    a cut must not land inside them either (cut_words)."""
    def __init__(self, items=(), busy=()):
        super().__init__(items)
        self.busy = list(busy)

def transcribe(src):
    global _whisper
    if not has_audio(src):                            # a video without sound: nothing to caption
        return Words()
    import whisper
    if _whisper is None:
        WHISPER_DIR.mkdir(parents=True, exist_ok=True)
        _whisper = whisper.load_model(WHISPER_MODEL, download_root=str(WHISPER_DIR))
    res = _whisper.transcribe(str(src), word_timestamps=True, condition_on_previous_text=False,
                              fp16=_whisper.device.type == "cuda", verbose=None)
    audio = whisper.load_audio(str(src))              # 16 kHz mono, for the singing check
    words, busy, dropped = [], [], 0
    for seg in res["segments"]:
        # captions = what the STREAMER says (Aleš: they were lyrics of the in-game radio):
        # drop segments Whisper itself doubts, repetitive ones (choruses) and sung ones
        sung = singing(audio[int(seg["start"] * 16000): int(seg["end"] * 16000)])
        ws = [(w["start"], w["end"], w["word"].strip()) for w in seg.get("words", []) if w["word"].strip()]
        if (seg.get("no_speech_prob", 0) > 0.5 or seg.get("avg_logprob", 0) < -0.9
                or seg.get("compression_ratio", 1) > 2.2 or sung > 0.45):
            dropped += 1
            # no caption, but it may still be the streamer talking: keep it as occupied time for the cuts
            busy += [(s, e, BUSY) for s, e, _ in ws] or [(seg["start"], seg["end"], BUSY)]
            continue
        words += ws
    if dropped:
        log(f"  captions: {dropped} segment(s) dropped as music/lyrics or unsure (kept as occupied time for cuts)")
    return Words(words, busy)

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
# matched on one token (punctuation stripped): a prefix where every longer word is a swear too
# (fucking, shitty, bitches, cunts), exact forms where a prefix would hit clean words (cockpit, Dickens)
SWEAR_RE = re.compile(r"^(?:[a-z]*fuck|[a-z]*shit|bitch|asshole|cunt|whore|slut|twat|wank|cocksucker"
                      r"|dick(?:head)?s?$|puss(?:y|ies)$|bastards?$|motherf|[a-z]*\*)", re.I)   # f***, sh*t: Whisper's own censoring

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

def swear_plan(words, card_at, sound_end=None):
    """Captions (masked, only before the score card) and the bleeps: every swear in the clip's sound,
    also under the card / fanpage tail where the clip keeps playing, up to sound_end (where the clip's
    own sound stops; None = to its end)."""
    masked, mute = mask_swears(words)
    if sound_end is not None:
        mute = [(m0, min(m1, sound_end)) for m0, m1 in mute if m0 < sound_end]
    return [w for w in masked if w[0] < card_at], mute

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

BRAND_NEED = {}                         # batch --brand-need: {brand: [catch-up now, other slot posts now, hot room]}

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
    # just-in-time render (batch --brand-need): the brand that can post it in this run - first the one
    # that owes catch-up posts (any score: the catch-up clips are rendered first), then the one with
    # other open slots now, then the one with room for a hot clip; a clip for a brand that cannot post
    # it now would be lost (the runner's out/ is gone by the next run)
    for part in (0, 1, 2):
        open_ = [n for n in names if (BRAND_NEED.get(n) or [0, 0, 0])[part] > 0]
        if open_:
            nxt = max(open_, key=lambda n: BRAND_NEED[n][part])
            BRAND_NEED[nxt][part] -= 1
            clip["_need"] = (nxt, part)              # process() gives it back if the render fails
            return [nxt]
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
                 cam_h=CAM_H, push=None, replay=None, cam_punch=None):
    """
    Filter graph ending in [v]. Without ass/mark it returns the clean 9:16 base.
    hook = (kind, T): the cold-start hook ends at T s. 'flash' = slow white flash there (the
    whole picture, under captions and watermarks), 'cut' = hard cut with a 0.2 s whip zoom 1.15 -> 1.0.
    push = shot edges (s): a slow push-in 1.0 -> 1.06 of the game band within every shot (fanpage,
    instead of the alternating punch and the beat pulses).
    replay = (main_end, r0, r1): the short ends at main_end, then r0..r1 again at 0.6x speed with
    a slight zoom - an instant replay of the payoff (fanpage, instead of the flash + shake).
    cam_punch = (tp, T): HOOK v2 - the facecam snaps to 1.15x on the streamer's peak reaction at
    tp s and eases back before the hook ends at T (only inside the hook).
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
        cz = ""
        if cam_punch and mark_file and cam_punch[1] > 0:
            tp, T = cam_punch
            # a visible snap (never already zoomed on frame 0): in over 0.08 s at the reaction (>= 0.25 s),
            # hold ~0.9 s, out over 0.25 s, all before the hook's transition at T
            a0 = min(max(0.25, tp - 0.04), max(0.25, T - 1.3))
            e0 = min(a0 + 0.9, T - 0.3)
            cz = (f",zoompan=z='1+0.15*clip((it-{a0:.3f})/0.08\\,0\\,1)*clip(({e0 + 0.25:.3f}-it)/0.25\\,0\\,1)'"
                  f":d=1:x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':s={W}x{cam_h}:fps={FPS}")
        f += f"[a]crop={w}:{h}:{x}:{y},scale={W}:{cam_h}:force_original_aspect_ratio=increase,crop={W}:{cam_h}{cz}[cam];"
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

def live_rating(z):
    """Live chat clips have no clip views: the banana score from the chat explosion instead
    (z 6 -> 5, 12 -> 7 (6.5 rounds up), 24 -> 8, 48+ -> 9; never the golden 10)."""
    return int(min(9, math.floor(5 + 1.5 * math.log2(max(z or 6, 6) / 6) + 0.5)))

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
              clip_end=None, outro_idx=None, follow_idx=None, follow_at=0.0, extras=(), hook_gain=None,
              voice_gain=0.0, music_env=None, voice_end=None, stems=False):
    """
    The short's sound: the clip's own audio + our music at one steady level (Aleš: always the
    same volume) + the score card's landing 'tups' + the hook flash whoosh (sfx, at sfx_at s).
    A limiter keeps the sum from clipping. replay = (main_end, r0, r1): the clip's sound ends at
    main_end, then r0..r1 again slowed to 0.6x (atempo) and quieter, under the music.
    Tension (HPC): a quiet riser in the 1.2 s before the payoff (peak), a deep hit on it, and the
    music drops to 20 % for 0.6 s there (sudden silence). mute = (start, end) of swears -> silent.
    clip_end = the clip's own sound stops there (an outro follows; its sound = outro_idx, from
    clip_end on). follow_idx = the Follow card's soft click at follow_at. Music runs to `total`.
    extras = [(input idx, at s, volume)]: more of our sounds (Heat whooshes).
    The limiter keeps the true peak under -1 dBTP (qa_short checks it).
    Speech first (Aleš 18. 11.: the music was too loud over quiet streams): voice_gain (dB, from
    mix_levels) brings the streamer's speech to SPEECH_DB before anything is added; music_env =
    the music's volume expression (ducked under every sentence, a bit up in the gaps) and a
    sidechain compressor keyed on the voice pushes it further down while anyone talks.
    voice_end = the clip's own sound fades out there (the next sentence would be cut under the
    card). stems = return the graph with [vo] (voice) and [mu] (music) unmixed (mix_check).
    """
    parts, f = [], ""
    fmt = "aformat=sample_rates=48000:channel_layouts=stereo"
    fmt0 = fmt + (f",volume={voice_gain:.2f}dB" if voice_gain else "")
    if mute:
        fmt0 += ",volume=0:enable='" + "+".join(f"between(t\\,{a:.2f}\\,{b:.2f})" for a, b in mute) + "'"
    if hook_gain:                               # (hook end s, dB): the teaser no louder than HOOK_MAX_LU over the clip
        fmt0 += f",volume={hook_gain[1]:.1f}dB:enable='lt(t\\,{hook_gain[0]:.2f})'"
    if clip_audio and replay:
        m_end, r0, r1 = replay
        f += (f";[0:a]{fmt0},asplit[ram][rar];[ram]atrim=0:{m_end:.3f},asetpts=PTS-STARTPTS[ram1];"
              f"[rar]atrim={r0:.3f}:{r1:.3f},asetpts=PTS-STARTPTS,atempo=0.6,volume=0.6,{fmt}[rar1];"
              f"[ram1][rar1]concat=n=2:v=0:a=1,apad=whole_dur={total:.3f}[a0]")
        parts.append("[a0]")
    elif clip_audio:
        end = f",atrim=0:{clip_end:.3f},afade=t=out:st={max(0.0, clip_end - 0.05):.3f}:d=0.05" if clip_end else ""
        if voice_end is not None and (not clip_end or voice_end < clip_end):
            end = f",atrim=0:{voice_end + 0.02:.3f},afade=t=out:st={max(0.0, voice_end - 0.1):.3f}:d=0.12"
        f += f";[0:a]{fmt0}{end},apad=whole_dur={total:.3f}[a0]"
        parts.append("[a0]")
    key = None
    if parts and parts[0] == "[a0]" and music_idx is not None:   # the voice also keys the music's compressor
        f += ";[a0]asplit[a0v][a0k]"
        parts[0], key = "[a0v]", "[a0k]"
    if outro_idx is not None and clip_end:
        f += f";[{outro_idx}:a]{fmt},adelay={int(clip_end * 1000)}:all=1,apad=whole_dur={total:.3f}[ou]"
        parts.append("[ou]")
    if follow_idx is not None:
        f += f";[{follow_idx}:a]volume=0.6,{fmt},adelay={int(follow_at * 1000)}:all=1,apad=whole_dur={total:.3f}[fo]"
        parts.append("[fo]")
    if music_idx is not None:                  # adeclick: the generated track has a little crackle
        duck = (f",volume='if(between(t\\,{peak:.2f}\\,{peak + 0.6:.2f})\\,0.2\\,1)':eval=frame"
                if peak is not None and hit_idx is not None else "")
        vol = f"volume='{music_env}':eval=frame" if music_env else f"volume={music_vol}"
        f += f";[{music_idx}:a]atrim=0:{total:.3f},adeclick,{vol}{duck},{fmt},apad=whole_dur={total:.3f}"
        f += (f"[mu0];[mu0]{key}sidechaincompress=threshold=0.03:ratio=4:attack=15:release=300:makeup=1[mu]"
              if key else "[mu]")
        parts.append("[mu]")
    if stems:                                   # mix_check: voice and music separately
        return f + (f";{parts[0]}anull[vo]" if parts and parts[0] in ("[a0]", "[a0v]") else "")
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
    for k, (ix, at, vol) in enumerate(extras):
        f += f";[{ix}:a]volume={vol},adelay={int(at * 1000)}:all=1,{fmt},apad=whole_dur={total:.3f}[ex{k}]"
        parts.append(f"[ex{k}]")
    if not parts:
        return None
    f += f";{''.join(parts)}amix=inputs={len(parts)}:duration=longest:normalize=0,atrim=0:{total:.3f},alimiter=limit=0.7:level=0:attack=2:release=40[aud]"
    return f

# Speech-first mix (Aleš 18. 11.: "on some streams the music is too loud compared to the voice").
SPEECH_DB = -20.0        # the streamer's speech (RMS over the words) is brought here before music is added
MUSIC_TALK_LU = -23.0    # music under a sentence: this far below that sentence (rule: >= 20 below)
MUSIC_GAP_LU = -12.0     # music between sentences: rises a bit, still well under the voice (brand 0.18 = this)
VOICE_GAIN = (-12.0, 18.0)   # voice gain limits, dB
MIX_MIN_DIFF = 15.0      # qa_short: speech must be at least this much over the music during words

def pcm(path, ss=0.0, t=None, sr=8000):
    """Mono float samples (-1..1) of a file's sound, `sr` Hz; empty when there is none."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error"] + (["-ss", f"{ss:.3f}"] if ss else []) + \
          (["-t", f"{t:.3f}"] if t else []) + ["-i", str(path), "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    try:
        raw = subprocess.run(cmd, capture_output=True, timeout=300).stdout
    except subprocess.TimeoutExpired:
        raw = b""
    return np.frombuffer(raw, np.int16).astype(np.float64) / 32768

def _db(x):
    return 10 * math.log10(max(float((x * x).mean()), 1e-10)) if x.size else -100.0

def talk_spans(words, until, pad=(0.1, 0.15), join=0.5):
    """Where anyone talks: word spans padded and joined when the pause between them is < `join` s."""
    out = []
    for w0, w1, _ in sorted(words):
        if w0 >= until:
            break
        s, e = max(0.0, w0 - pad[0]), min(until, w1 + pad[1])
        if out and s - out[-1][1] < join:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out

def mix_levels(src, words, total, mpath=None, mstart=0.0, mvol=0.18, voice_end=None, hook_gain=None, sr=8000):
    """
    Speech-first levels for audio_mix. Measures the clip's speech (RMS where Whisper heard words)
    and returns (voice gain dB, music volume expression or None, info):
      - voice gain brings the speech to SPEECH_DB (quiet and loud streams sound the same);
      - music under every talk span sits MUSIC_TALK_LU below THAT span's speech (measured on both
        tracks, so a loud bar of the song never beats a quiet sentence); between sentences it
        rises to MUSIC_GAP_LU (+ the brand's taste: music_volume relative to 0.18), 0.12 s ramps.
    """
    x = pcm(src, sr=sr)
    until = min(total, voice_end if voice_end is not None else total, x.size / sr)
    spans = talk_spans(words, until)
    seg = lambda a, s, e: a[int(s * sr): int(e * sr)]
    if spans:
        speech = _db(np.concatenate([seg(x, s, e) for s, e in spans]))
    else:                                      # nobody talks: the loud half of the clip stands in
        fr = x[: x.size // sr * sr].reshape(-1, sr) if x.size >= sr else x.reshape(1, -1)
        e_ = (fr ** 2).mean(axis=1)
        speech = 10 * math.log10(max(float(np.median(e_[e_ >= np.median(e_)])) if e_.size else 1e-10, 1e-10))
    gain = SPEECH_DB - speech
    peak = float(np.percentile(np.abs(x), 99.9)) if x.size else 0.0
    if peak > 0:                               # never more than ~6 dB into the limiter on the shouts
        gain = min(gain, -20 * math.log10(peak) + 5.0)
    gain = max(VOICE_GAIN[0], min(VOICE_GAIN[1], gain))
    info = {"speech_db": round(speech, 1), "voice_gain": round(gain, 1), "spans": len(spans)}
    if not mpath:
        return gain, None, info
    m = pcm(mpath, ss=mstart, t=total, sr=sr)
    if m.size < sr:
        return gain, None, info
    taste = 20 * math.log10(max(mvol, 1e-3) / 0.18)
    g_gap = 10 ** ((SPEECH_DB + MUSIC_GAP_LU + taste - _db(m)) / 20)
    terms, R = [], 0.12
    for s, e in spans:
        hg = hook_gain[1] if hook_gain and s < hook_gain[0] else 0.0  # the teaser is turned down in audio_mix
        sp = max(_db(seg(x, s, e)) + gain + hg, SPEECH_DB - 12)  # a near-silent 'word' does not mute the song
        g = min(g_gap, 10 ** ((sp + MUSIC_TALK_LU - _db(seg(m, s, e))) / 20))
        if g < g_gap:
            terms.append(f"{g - g_gap:.5f}*clip(min((t-{s - R:.3f})/{R}\\,({e + R:.3f}-t)/{R})\\,0\\,1)")
    env = f"{g_gap:.5f}" + "".join("+" + t_ for t_ in terms)
    info.update(music_gap_db=round(20 * math.log10(g_gap), 1))
    return gain, env, info

def mix_check(src, words, total, mpath, mstart, gain, env, voice_end=None, d=None, hook_gain=None, sr=8000):
    """
    Measured speech vs music while anyone talks (the same audio_mix graph, voice and music kept
    apart): {'mix_speech_db', 'mix_music_db', 'diff' (power means over the words), 'diff_min' (worst talk
    span with real speech)}. None when there is no music.
    """
    if not mpath or not env:
        return None
    flt = audio_mix(True, total, music_idx=1, voice_gain=gain, music_env=env, voice_end=voice_end, stems=True,
                    hook_gain=hook_gain)
    tmp = Path(d or Path(src).parent)
    vo, mu = tmp / "_mix_vo.raw", tmp / "_mix_mu.raw"
    run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(src), "-ss", f"{mstart:.2f}", "-i", str(mpath),
         "-filter_complex", flt.lstrip(";"), "-map", "[vo]", "-ac", "1", "-ar", str(sr), "-f", "s16le", str(vo),
         "-map", "[mu]", "-ac", "1", "-ar", str(sr), "-f", "s16le", str(mu)], timeout=300)
    a = np.fromfile(vo, np.int16).astype(np.float64) / 32768
    b = np.fromfile(mu, np.int16).astype(np.float64) / 32768
    vo.unlink(missing_ok=True)
    mu.unlink(missing_ok=True)
    until = min(total, voice_end if voice_end is not None else total)
    ws = [(max(0.0, w0), min(until, w1)) for w0, w1, _ in words if w0 < until and w1 - w0 > 0.05]
    if not ws:
        return None
    seg = lambda x, s, e: x[int(s * sr): int(e * sr)]
    sp, ms = np.concatenate([seg(a, s, e) for s, e in ws]), np.concatenate([seg(b, s, e) for s, e in ws])
    worst = []
    for s, e in talk_spans(words, until, pad=(0, 0), join=0.5):
        if e - s >= 0.3 and _db(seg(a, s, e)) >= SPEECH_DB - 12:
            worst.append(_db(seg(a, s, e)) - _db(seg(b, s, e)))
    return {"mix_speech_db": round(_db(sp), 1), "mix_music_db": round(_db(ms), 1), "diff": round(_db(sp) - _db(ms), 1),
            "diff_min": round(min(worst), 1) if worst else None}

HOOK_MAX_LU = 4.0        # the hook teaser (the payoff's scream) may be this much louder than the clip, not more
_hook_lvl = {}

def hook_level(src, hook_t, end):
    """
    (hook_t, gain dB) when the teaser [0, hook_t] is more than HOOK_MAX_LU louder than the clip
    [hook_t, end] (power mean): the whole short is -14 LUFS, but a quiet stream with one scream
    would otherwise jump from loud hook to quiet clip (Aleš: same level everywhere). Else None.
    """
    key = (str(src), round(hook_t, 2), round(end, 2))
    if key in _hook_lvl:
        return _hook_lvl[key]
    res = None
    if hook_t >= 0.5 and end - hook_t >= 3:
        raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-t", f"{end:.3f}", "-i", str(src), "-vn", "-ac", "1",
                              "-ar", "8000", "-f", "s16le", "-"], capture_output=True, timeout=300).stdout
        x = np.frombuffer(raw, np.int16).astype(np.float64) / 32768
        k = int(hook_t * 8000)
        if x.size > k + 8000:
            db = lambda a: 10 * math.log10(max(float((a * a).mean()), 1e-9))
            diff = db(x[:k]) - db(x[k:])
            if diff > HOOK_MAX_LU:
                res = (hook_t, -min(diff - HOOK_MAX_LU, 10.0))
    _hook_lvl[key] = res
    return res

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

# -----------------------------------------------------------------------------
# launch layer (17. 11., before GTA 6 on 19. 11.): HOOK v2, the fanpage model's reaction
# stickers, HEAT 1-5 (fanpage), the golden 10/10 (bananas) and an automatic QA of every short.
# Each sits behind a brands.json flag (hook_v2, model_sticker, heat, golden10, qa) and can be
# switched off without touching the code. Restraint (Aleš 17. 11.: "nepřeplácat, struktura
# musí být bezchybná"): hook -> progression -> climax (+ one sticker on the payoff) -> >= 3 s
# after it -> score card (bananas) / Heat then the outro (fanpage). At most ONE extra element
# on screen at a time, never over the face, the payoff action or the captions. A short that
# fails QA is re-rendered in the proven old look; if even that fails it is not published.

MODEL_DIR = Path(os.environ.get("KLIPY_MODEL_REAKCE") or HERE / "assets" / "model_reakce")
STICKER_DUR = 0.9                      # s on screen, spring pop-in with overshoot, silent
STICKER_W, STICKER_H = 300, 340        # the still is fitted into this box
SLOT_X, SLOT_BOTTOM = 40, 1516         # bottom-left corner of the game band (the banana's place):
                                       # under the captions, left of TikTok's buttons, above its caption bar
HEAT_W, HEAT_H = 470, 150
HEAT_DUR = 2.5                         # s before the outro
HEAT_OUTRO_DUR = 2.0                   # no room before the outro: over its first ~2 s instead...
HEAT_OUTRO_Y = 40                      # ...at the top (the model's head starts ~y 200 in the outro)
HEAT_GROW, HEAT_EXIT, HEAT_SLIDE = 0.015, 0.3, 70   # slow growth over the badge's life; exit: s, px down
GOLD_TOP = 97                          # golden 10/10 / Heat 5: top 3 % of recently rendered clips (views)
GOLD_MIN_HIST = 30                     # fewer rendered clips than this: only a raw 10 (500k views) is gold
GOLD_MIN_RAW = 7                       # ... and never for a small clip on a quiet day
GOLD_CARD = "score_10_gold.webm"
LAUNCH_FLAGS = ("hook_v2", "model_sticker", "heat", "golden10", "league_chip")

def spring_pop(t, freq=1.8, damp=8.0):
    """0 -> overshoot ~1.1 -> 1 (closed-form spring, no timers)."""
    return 0.0 if t <= 0 else 1 - math.exp(-damp * t) * math.cos(2 * math.pi * freq * t)

def ease_crit(t, w=14.0):
    """0 -> 1 without overshoot (critically damped spring, closed form): 1 - e^(-wt)(1 + wt).
    Motion audit 17. 11.: our graphics never overshoot; w=14 is ~99 % there after 0.47 s."""
    return 0.0 if t <= 0 else 1 - math.exp(-w * t) * (1 + w * t)

SLIDE_IN, SLIDE_OUT = 0.32, 0.26       # the model's reaction video slides in / out (s)

def golden_rating(clip, raw):
    """
    (rating, gold): 10/10 is rare (~1 in 30): only a clip in the top 3 % of the last 300 rendered
    clips by views (the number the score comes from) gets 10 - the golden one; every other clip
    is capped at 9. YouTube windows carry the whole video's views: never gold.
    """
    if clip.get("platform") == "youtube" or clip.get("live"):   # live: no clip views either
        return min(raw, 9), False
    v = clip.get("views") or 0
    hist = [x for x in load_json(STATE, {}).get("rating_hist") or [] if isinstance(x, (int, float))]
    if len(hist) >= GOLD_MIN_HIST:
        top = v > 0 and raw >= GOLD_MIN_RAW and sum(x >= v for x in hist) <= len(hist) * (100 - GOLD_TOP) / 100
    else:
        top = raw >= 10
    return (10, True) if top else (min(raw, 9), False)

def record_rating(clip):
    """Remember the clip's views for the golden 10/10 percentile (state.json rating_hist, last 300)."""
    if clip.get("platform") == "youtube" or not clip.get("views"):
        return
    state = load_json(STATE, {"done": {}, "failed": {}})
    state["rating_hist"] = (state.get("rating_hist") or [])[-299:] + [int(clip["views"])]
    save_json(STATE, state)

def heat_level(raw, gold):
    """Fanpage HEAT 1-5 from the same score as the banana rating; 5 only for the golden top 3 %."""
    return 5 if gold else max(1, min(4, 1 + (raw - 1) * 4 // 9))

# the model's reaction for a moment: what was said around it -> fitting stills (file stems:
# gasp, laugh, wink, shocked, smirk, eyeroll; laugh_2.png etc. counts as 'laugh')
MODEL_KINDS = [
    (r"\b(lmao|lmfao|lol|ha(ha)+|i'?m dead|crying)\b", ["laugh", "smirk"]),
    (r"\b(no way|what|wait|huh|how|oh my god|omg|wtf)\b", ["shocked", "gasp"]),
    (r"\b(fuck(ing)?|shit|bruh|why|are you serious)\b", ["eyeroll", "smirk"]),
    (r"\b(let'?s go|yes+|clutch|easy|gg|w)\b", ["wink", "smirk"]),
]

# the model's reaction is a short VIDEO (Aleš 17. 11.: "not photos, short videos like the banana, face
# and belly visible"): assets/model_reakce/<kind>.webm, VP9 with alpha, 1.8 s, 618x1100, played whole
# in the banana's place and size. Picked by the moment's tag; the PNG still is only a fallback.
MODEL_DUR = 1.8
MODEL_BY_TAG = [("funny", ["laugh"]), ("crash", ["shocked", "omg"]), ("chase", ["shocked", "omg"]),
                ("glitch", ["eyeroll"]), ("wow", ["gasp"])]
MODEL_ELSE = ["wink", "smirk"]

def model_kinds(tags):
    """Reaction kinds for a moment's tags (moment_tags order = strongest first)."""
    for t in tags or []:
        for tag, kinds in MODEL_BY_TAG:
            if t == tag:
                return kinds
    return MODEL_ELSE

def model_reaction(kinds, seed, avoid=()):
    """(kind, file): the reaction video of one of `kinds` (not in avoid), else its PNG; None if nothing."""
    rnd = random.Random(seed)
    for pool in (kinds, [k for k in kinds + MODEL_ELSE + ["gasp", "omg", "shocked"] if k not in kinds]):
        opts = [k for k in pool if k not in avoid]
        rnd.shuffle(opts)
        for k in opts:
            for ext in (".webm", ".png"):
                f = MODEL_DIR / f"{k}{ext}"
                if f.is_file():
                    return k, f
    return None

def model_stills():
    """The model's keyed reaction stills (transparent PNG); none -> no stickers, silently."""
    try:
        return sorted(p for p in MODEL_DIR.glob("*.png") if p.is_file())
    except OSError:
        return []

def pick_still(stills, words, t, seed, avoid=None, prefer=None):
    rnd = random.Random(f"{seed}:{t:.1f}")
    near = " ".join(w.lower() for s0, e0, w in words if t - 3 <= e0 <= t + 2)
    kinds = prefer or next((k for rx, k in MODEL_KINDS if re.search(rx, near)), ["shocked", "gasp", "smirk"])
    by_kind = lambda k: [p for p in stills if p.stem.lower().split("_")[0] == k and p != avoid]
    for k in kinds:
        if by_kind(k):
            return rnd.choice(by_kind(k))
    rest = [p for p in stills if p != avoid] or stills
    return rnd.choice(rest)

def sticker_frames(png, out_dir):
    """
    The still as a silent 0.9 s sprite: spring pop-in with overshoot (anchored bottom centre),
    fades out over the last 0.15 s. Returns (frame pattern, x, y) for the overlay.
    """
    from PIL import Image
    im = Image.open(png).convert("RGBA")
    im.thumbnail((STICKER_W, STICKER_H), Image.LANCZOS)
    w, h = im.size
    cw, ch = int(STICKER_W * 1.25), int(STICKER_H * 1.25)          # room for the overshoot
    out_dir.mkdir(parents=True, exist_ok=True)
    n = int(round(STICKER_DUR * FPS))
    for i in range(n):
        t = i / FPS
        s = max(0.02, ease_crit(t, 16.0))                 # (fallback still) motion audit: no overshoot
        a = max(0.0, min(1.0, (STICKER_DUR - t) / 0.15))
        sp = im.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
        if a < 1:
            sp.putalpha(sp.getchannel("A").point(lambda v: int(v * a)))
        canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        canvas.alpha_composite(sp, (max(0, (cw - sp.width) // 2), max(0, ch - sp.height)))
        canvas.save(out_dir / f"{i:03d}.png")
    x = SLOT_X + STICKER_W // 2 - cw // 2
    return str(out_dir / "%03d.png"), max(0, x), SLOT_BOTTOM - ch

def flame(size, glow=True, dim=False):
    """A procedural flame icon (Pillow): neon pink outside, orange, yellow core; dim = empty slot."""
    from PIL import Image, ImageDraw, ImageFilter
    S = size * 2
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    def drop(scale, dy):
        pts = []
        for k in range(64):
            t = 2 * math.pi * k / 64
            x = math.sin(t) * math.sin(t / 2) ** 1.4 * 0.62
            y = -math.cos(t)
            x += 0.12 * math.sin(3 * (y + 1)) * (1 - y) / 2           # a small curl at the tip
            pts.append((S / 2 + x * size * 0.5 * scale, S / 2 + 0.1 * size + dy + y * size * 0.55 * scale))
        return pts

    d = ImageDraw.Draw(img)
    if dim:
        d.polygon(drop(1.0, 0), outline=(255, 255, 255, 90), width=max(2, size // 22))
        return img.crop(img.getbbox())
    if glow:
        g = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        ImageDraw.Draw(g).polygon(drop(1.08, 0), fill=(255, 40, 170, 200))
        img.alpha_composite(g.filter(ImageFilter.GaussianBlur(size / 9)))
    d.polygon(drop(1.0, 0), fill=(255, 61, 168, 255))                  # neon pink
    d.polygon(drop(0.72, size * 0.1), fill=(255, 128, 31, 255))        # orange
    d.polygon(drop(0.42, size * 0.2), fill=(255, 214, 77, 255))        # yellow core
    return img.crop(img.getbbox())

def heat_frames(n, dur, out_dir):
    """
    HEAT badge (a data graphic: the label + N of 5 flames) for the last `dur` s before the
    outro (or over the outro's start): the dark pill eases in (no overshoot), the flames light one by
    one, each with a soft rising tick; the badge keeps growing slowly and leaves by sliding down.
    Writes PNG frames + heat.wav (the ticks). Returns (pattern, x, y, wav, pop times).
    """
    from PIL import Image, ImageDraw, ImageFont
    out_dir.mkdir(parents=True, exist_ok=True)
    font = ImageFont.truetype(str(CAP_FONTS / "anton.ttf"), 64)
    fl = flame(70)
    empty = flame(70, dim=True)
    nf = int(round(dur * FPS))
    gap = min(0.28, max(0.12, (dur - 0.9) / max(n, 1)))
    pops = [0.35 + i * gap for i in range(n)]
    pad = 40
    cw, ch = HEAT_W + 2 * pad, HEAT_H + 2 * pad + HEAT_SLIDE         # room below for the exit slide
    out_t = min(HEAT_EXIT, dur / 4)
    for i in range(nf):
        t = i / FPS
        canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        # motion audit 17. 11.: no overshoot, never standing still, no hard end. In: from 80 % size and
        # transparent, critically damped. Hold: a slow ~1.5 % growth the whole time. Out: slides down.
        e_in = ease_crit(t, 16.0)
        s = (0.8 + 0.2 * e_in) * (1 + HEAT_GROW * t / dur)
        u_out = max(0.0, t - (dur - out_t)) / out_t                    # 0 -> 1 over the exit
        drop = int(round(HEAT_SLIDE * u_out * u_out))                  # eases in: starts slow, leaves fast
        a = min(e_in, 1 - u_out)
        pill = Image.new("RGBA", (HEAT_W, HEAT_H), (0, 0, 0, 0))
        pd = ImageDraw.Draw(pill)
        pd.rounded_rectangle((0, 0, HEAT_W - 1, HEAT_H - 1), radius=HEAT_H // 2, fill=(14, 10, 22, 175),
                             outline=(255, 61, 168, 230), width=4)
        pd.text((36, HEAT_H // 2), "HEAT", font=font, fill=(255, 255, 255, 255), anchor="lm")
        for k in range(5):
            cx = 196 + k * 58
            u = t - pops[k] if k < n else -1
            if k < n and u > 0:
                fs = max(0.02, ease_crit(u, 18.0))
                f = fl.resize((max(1, int(fl.width * 0.92 * fs)), max(1, int(fl.height * 0.92 * fs))), Image.LANCZOS)
            else:
                f = empty.resize((int(empty.width * 0.92), int(empty.height * 0.92)), Image.LANCZOS)
            pill.alpha_composite(f, (max(0, min(HEAT_W - f.width, cx - f.width // 2)),
                                     max(0, min(HEAT_H - f.height, HEAT_H // 2 - f.height // 2))))
        pill = pill.resize((max(1, int(HEAT_W * s)), max(1, int(HEAT_H * s))), Image.LANCZOS)
        if a < 1:
            pill.putalpha(pill.getchannel("A").point(lambda v: int(v * max(0.0, a))))
        px = pad + (HEAT_W - pill.width) // 2
        py = pad + (HEAT_H - pill.height) // 2 + drop
        canvas.alpha_composite(pill, (max(0, px), max(0, min(ch - pill.height, py))))
        canvas.save(out_dir / f"{i:03d}.png")
    # a soft tick per flame, each a little higher (a flame does not move, so no whoosh): a short
    # sine with its octave, 3 ms attack, ~90 ms decay; synthesized, no samples
    sr = 48000
    buf = np.zeros(int(sr * (dur + 0.5)))
    m = int(sr * 0.12)
    tt = np.arange(m) / sr
    env = np.minimum(1.0, tt / 0.003) * np.exp(-tt / 0.03)
    for k, p in enumerate(pops):
        f0 = 880 * 1.122 ** k                                          # +2 semitones per flame
        tick = (np.sin(2 * np.pi * f0 * tt) + 0.3 * np.sin(4 * np.pi * f0 * tt)) * env
        i0 = max(0, int(p * sr))
        buf[i0: i0 + m] += tick[: buf.size - i0]
    buf = buf / (np.abs(buf).max() + 1e-9) * 0.25
    import wave
    wav = out_dir / "heat.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((buf * 32767).astype(np.int16).tobytes())
    return str(out_dir / "%03d.png"), SLOT_X - pad, SLOT_BOTTOM - HEAT_H - pad, wav, pops

CHIP_Y = 690                           # top of the league chip: just under the card's big N/10

def league_rank(channel):
    """The streamer's Banana League rank this week (liga.table, real data) or None."""
    try:
        import liga
        return next((r["rank"] for r in liga.table(7) if r["channel"] == (channel or "").lower()), None)
    except Exception as e:
        log(f"  league rank: {str(e)[-80:]}")
        return None

def chip_frames(text, out_dir, dur):
    """
    The league chip on the banana score card ('#2 THIS WEEK', data only): the card's own dark
    label style. Motion audit 17. 11.: comes out of a blur with a small lift (no spring overshoot),
    then drifts up slowly to the end of the card (never standing still). Returns (pattern, x, y).
    """
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    font = ImageFont.truetype(str(CAP_FONTS / "montserrat-900.ttf"), 44)
    tw = int(font.getlength(text))
    w, h = tw + 72, 86
    pill = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(pill)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=18, fill=(46, 50, 64, 240), outline=(96, 102, 118, 255), width=4)
    d.text((w / 2, h / 2 + 1), text, font=font, fill=(255, 255, 255, 255), anchor="mm")
    lift, drift, blur = 16, 8, 10                     # px up during the entrance, px up over the rest, max blur
    m = blur * 2                                      # room for the blur halo
    cw, ch = w + 2 * m, h + 2 * m + lift + drift
    out_dir.mkdir(parents=True, exist_ok=True)
    padded = Image.new("RGBA", (w + 2 * m, h + 2 * m), (0, 0, 0, 0))
    padded.alpha_composite(pill, (m, m))
    nf = max(1, int(round(dur * FPS)))
    for i in range(nf):
        t = i / FPS
        e = ease_crit(t, 12.0)
        sp = padded if e > 0.985 else padded.filter(ImageFilter.GaussianBlur(blur * (1 - e)))
        if e < 1:
            sp = sp.copy()
            sp.putalpha(sp.getchannel("A").point(lambda v: int(v * e)))
        y = drift + int(round(lift * (1 - e) - drift * (t / max(dur, 0.1))))   # top of the padded sprite
        canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        canvas.alpha_composite(sp, (0, max(0, min(ch - sp.height, y))))
        canvas.save(out_dir / f"{i:03d}.png")
    return str(out_dir / "%03d.png"), (W - cw) // 2, CHIP_Y - m - drift

def sprite_inputs(sprites):
    """ffmpeg inputs of the sprites: PNG frame sequences with alpha, or a VP9+alpha .webm (t0, path, x, y, dur, w, h)."""
    args = []
    for t0, pattern, x, y, *rest in sprites:
        if str(pattern).endswith(".webm"):              # the model's reaction video: libvpx keeps the alpha
            args += ["-c:v", "libvpx-vp9", "-t", f"{rest[0]:.3f}", "-i", str(pattern)]
        else:
            args += ["-framerate", str(FPS), "-i", pattern]
    return args

def sprite_overlay(f, sprites, base):
    """Overlay sprite sequences (inputs base, base+1 ...) at their times onto a graph ending in [v]."""
    if not sprites:
        return f
    f = f[: -len("[v]")] + "[s_in]"
    prev = "s_in"
    for i, (t0, pattern, x, y, *rest) in enumerate(sprites):
        size = f"scale={rest[1]}:{rest[2]}:flags=lanczos," if len(rest) >= 3 else ""
        xe = x
        if len(rest) >= 3:
            # the model's reaction video (motion audit 17. 11.: no pop): slides in from the left edge
            # of the frame (ease-out, no overshoot) and leaves the same way, like kompilace's sticker
            t1, w = t0 + rest[0], rest[1]
            ti, to = min(SLIDE_IN, rest[0] / 3), min(SLIDE_OUT, rest[0] / 3)
            e = (f"if(lt(t,{t0 + ti:.3f}),1-pow(1-max(0,t-{t0:.3f})/{ti:.3f},3),"
                 f"if(gt(t,{t1 - to:.3f}),1-pow(1-max(0,{t1:.3f}-t)/{to:.3f},2),1))")
            xe = f"'{x + w}*({e})-{w}'"
        f += (f";[{base + i}:v]{size}format=rgba,setpts=PTS-STARTPTS+{t0:.3f}/TB[sp{i}];"
              f"[{prev}][sp{i}]overlay={xe}:{y}:eof_action=pass[so{i}]")
        prev = f"so{i}"
    return f + f";[{prev}]null[v]"

def loud_start(src, peak, lo=0.9, hi=0.05):
    """HOOK v2: start of the hook in [peak-lo, peak-hi] whose first 300 ms are the loudest (the scream)."""
    s0 = max(0.0, peak - lo - 0.1)
    try:
        raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{s0:.3f}", "-t", f"{lo + 0.6:.3f}", "-i", str(src),
                              "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"], capture_output=True, timeout=120).stdout
    except subprocess.TimeoutExpired:
        return max(0.0, peak - 0.3)
    a = np.frombuffer(raw, np.int16).astype(np.float64)
    if a.size < 8000 * 0.6:
        return max(0.0, peak - 0.3)
    best, best_e = max(0.0, peak - 0.3), -1.0
    for s in np.arange(max(0.0, peak - lo), peak - hi + 1e-6, 0.05):
        i = int((s - s0) * 8000)
        seg = a[i: i + 2400]
        if seg.size < 2000:
            continue
        e = float(np.sqrt((seg * seg).mean()))
        if e > best_e * 1.03:                  # an earlier start wins only if clearly louder
            best, best_e = float(s), e
    return best

def loud_stats(path):
    """Integrated loudness (LUFS) and true peak (dBTP) of a file's audio, ebur128; None = not measured."""
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "info", "-i", str(path), "-vn", "-af", "ebur128=peak=true",
                        "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=600)
    tail = r.stderr[r.stderr.rfind("Summary:"):] if "Summary:" in r.stderr else r.stderr
    li = re.search(r"I:\s*(-?[\d.]+|-inf)\s*LUFS", tail)
    tp = re.search(r"True peak:\s*Peak:\s*(-?[\d.]+|-inf)\s*dBFS", tail)
    num = lambda m: (float("-inf") if m.group(1) == "-inf" else float(m.group(1))) if m else None
    return num(li), num(tp)

def ff_stats(path, until=None):
    """ffmpeg probes of a finished short: black/frozen stretches (video up to `until`), loudness, true peak."""
    vt = ["-t", f"{until:.3f}"] if until else []
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "info", *vt, "-i", str(path), "-vf",
                        "blackdetect=d=1.0:pix_th=0.06,freezedetect=n=0.003:d=2.5", "-an", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    black = re.findall(r"black_start:\s*([\d.]+)", r.stderr)
    frozen = re.findall(r"freeze_start:\s*([\d.]+)", r.stderr)
    return (black, frozen) + loud_stats(path)

def hook_is_loud(path, main_end):
    """The first 300 ms are at least as loud as the short's median (50 ms windows up to main_end)."""
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-t", f"{main_end:.3f}", "-i", str(path), "-vn",
                          "-ac", "1", "-ar", "8000", "-f", "s16le", "-"], capture_output=True, timeout=300).stdout
    a = np.frombuffer(raw, np.int16).astype(np.float64)
    if a.size < 8000:
        return False
    fr = np.sqrt((a[: a.size // 400 * 400].reshape(-1, 400) ** 2).mean(axis=1))
    first = float(np.sqrt((a[:2400] ** 2).mean()))
    return first >= float(np.median(fr))

LUFS_TARGET = -14.0      # every short (some came out at -22 LUFS, 17. 11.); YouTube/TikTok/IG play ~-14
LUFS_TOL = 2.0           # qa_short: outside target +- this = a problem
# True peak: loudnorm aims at TP_TARGET, but the AAC re-encode adds 1-3 dB on explosions and gunshots
# (broadband bursts; measured -2 -> -0.3 / +0.5 dBTP, 18. 11.: both looks failed QA and the clip was
# lost). So the result is MEASURED after the encode; above TP_SAFE it is redone from the original with a
# limiter after loudnorm (at 48 kHz, right before the AAC encode), then with lower targets, and as the
# last resort the best version is turned down by the measured excess (a static gain, no pumping).
TP_TARGET = -3.0
TP_SAFE = -1.2           # measured after the encode; qa_short wants <= -1 dBTP
TP_RETRY = (-3.0, -4.5, -6.0)    # targets of the limiter passes after the plain one

def _loudnorm_pass(path, out, tp, limit):
    """One two-pass loudnorm of `path` into `out` with true peak target `tp` (+ a true-peak limiter).
    Returns loudnorm's measurement of the input (dict) or None (no audio / silent / nothing to do)."""
    probe = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(path), "-vn", "-af",
                            f"loudnorm=I={LUFS_TARGET}:TP={tp}:LRA=11:print_format=json", "-f", "null", "-"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", probe.stderr)
    if not m:
        return None
    js = json.loads(m.group(0))
    if js["input_i"] in ("-inf", "inf") or float(js["input_i"]) < -60:
        return None                                        # silent: qa_short reports it
    af = (f"loudnorm=I={LUFS_TARGET}:TP={tp}:LRA=11:linear=true:measured_I={js['input_i']}:"
          f"measured_TP={js['input_tp']}:measured_LRA={js['input_lra']}:measured_thresh={js['input_thresh']}:"
          f"offset={js['target_offset']},aresample=48000")
    if limit:                                              # on what the encoder gets (measured better than at 192 kHz)
        af += f",alimiter=limit={10 ** (tp / 20):.4f}:level=0:attack=1:release=60"
    run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:v", "-map", "0:a",
         "-c:v", "copy", "-af", af, "-c:a", "aac", "-b:a", "128k", "-cutoff", "18000", "-movflags", "+faststart",
         str(out)], timeout=600)
    return js

def loudness_fix(path):
    """
    Two-pass loudnorm of a finished short to LUFS_TARGET integrated, true peak <= TP_TARGET, then the
    encoded result's true peak is measured: above TP_SAFE the pass is redone from the original with
    the TP_RETRY targets (+ limiter). linear=true: one steady gain (no pumping); loudnorm falls back
    to dynamic only when that gain would push the peaks over the limit. Video is copied, audio
    re-encoded like encode(). Returns (before LUFS, after LUFS, after dBTP), or None when there is
    nothing to fix (no audio / silent).
    """
    lufs0, tp0 = loud_stats(path)
    if lufs0 is None or lufs0 == float("-inf") or lufs0 < -60:
        return None
    if abs(lufs0 - LUFS_TARGET) < 0.5 and tp0 is not None and tp0 <= TP_SAFE:
        return lufs0, lufs0, tp0                           # already right
    best = None                                            # (tp, lufs, file)
    tmps = []
    for k, (tp, limit) in enumerate([(TP_TARGET, False)] + [(t, True) for t in TP_RETRY]):
        tmp = Path(path).with_name(Path(path).stem + f"_ln{k}.mp4")
        tmps.append(tmp)
        if _loudnorm_pass(path, tmp, tp, limit) is None:
            break
        lufs1, tp1 = loud_stats(tmp)
        if tp1 is None or lufs1 is None:
            continue
        if best is None or tp1 < best[0]:
            best = (tp1, lufs1, tmp)
        if tp1 <= TP_SAFE:
            break
        log(f"  loudness: true peak {tp1:.1f} dBTP after the encode (target {tp}{', limiter' if limit else ''})"
            " -> next pass")
    if best and best[0] > TP_SAFE:                         # last resort: the best one, turned down by the excess
        cut = best[0] - (TP_SAFE - 0.3)
        tmp = Path(path).with_name(Path(path).stem + "_lnv.mp4")
        tmps.append(tmp)
        run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", str(best[2]), "-map", "0:v",
             "-map", "0:a", "-c:v", "copy", "-af", f"volume=-{cut:.2f}dB", "-c:a", "aac", "-b:a", "128k",
             "-cutoff", "18000", "-movflags", "+faststart", str(tmp)], timeout=600)
        lufs1, tp1 = loud_stats(tmp)
        log(f"  loudness: still {best[0]:.1f} dBTP -> turned down {cut:.1f} dB: {lufs1} LUFS, {tp1} dBTP")
        if tp1 is not None and lufs1 is not None and tp1 < best[0]:
            best = (tp1, lufs1, tmp)
    if best:
        os.replace(best[2], path)
    for t in tmps:
        t.unlink(missing_ok=True)
    return (lufs0, best[1], best[0]) if best else None

# Boredom gate (Aleš 17. 11.: no clip where nothing happens, every short must hook). Conservative on
# purpose: it only drops what is clearly flat - the payoff no louder and no busier than the rest of the
# clip and nobody reacting - or a dead stretch the jump cuts left (quiet AND still for > 2.5 s).
BORE_LOUD_DB = 6.0       # payoff 0.6 s loudness over the clip's median, dB
BORE_MOTION_X = 3.0      # payoff picture change over the clip's median
BORE_REACT = 0.4         # a laugh / shout word near the payoff adds this to the hook score
BORE_DEAD_S = 2.5        # quiet + still for longer than this = dead air
BORE_BUSY_MOTION = 12.0   # non-stop action: median picture change at least this (pool clips: 0.2-17) ...
BORE_BUSY_DB = -26.0     # ... and median loudness at least this dBFS
REACT_RE = re.compile(r"\b(ha(ha)+|lmao|lol|bro|what|wtf|no+|yo+|oh+|omg|holy|dude|run|go+)\b|!", re.I)

def activity(path, a, b, crop=None):
    """Every 0.2 s of [a, b]: audio loudness (dBFS, 0.6 s windows) and picture change (0-255)."""
    n = max(1, int((b - a) / 0.2))
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(path),
                          "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"], capture_output=True, timeout=300).stdout
    x = np.frombuffer(raw, np.int16).astype(np.float64) / 32768
    db = np.full(n, -90.0)
    if x.size:
        c = np.concatenate([[0.0], np.cumsum(x * x)])
        for i in range(n):
            lo, hi = max(0, int((i * 0.2 - 0.2) * 8000)), min(x.size, int((i * 0.2 + 0.4) * 8000))
            if hi > lo:
                db[i] = 10 * math.log10(max((c[hi] - c[lo]) / (hi - lo), 1e-9))
    vf = (f"crop={crop}," if crop else "") + "fps=5,scale=160:90,format=gray"
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(path),
                          "-vf", vf, "-f", "rawvideo", "-"], capture_output=True, timeout=300).stdout
    fr = np.frombuffer(raw, np.uint8)[: len(raw) // 14400 * 14400].reshape(-1, 90, 160).astype(np.float32)
    mo = np.abs(np.diff(fr, axis=0)).mean(axis=(1, 2)) if len(fr) > 1 else np.zeros(1)
    mo = np.interp(np.arange(n) * 0.2, np.arange(len(mo)) * 0.2 + 0.2, mo)   # frame i-1 -> i ends at 0.2 i
    return db, mo

def boredom(path, words, peak, a, b, viral=False, crop=None):
    """
    Problems (empty = it hooks) and the numbers, for the part [a, b] of `path` (s); peak = payoff
    in the same timeline. viral = viewers already pushed it hard (virality/score): the payoff
    contrast may be smaller, a dead stretch still is not allowed.
    """
    if b - a < 4:
        return [], {}
    db, mo = activity(path, a, b, crop)
    t = a + np.arange(len(db)) * 0.2
    base_db, base_mo = float(np.median(db)), float(np.median(mo))
    smooth = lambda x: np.convolve(np.pad(x, 1, mode="edge"), np.ones(3) / 3, "valid")   # 0.6 s, no fake edge peaks
    sdb, smo = smooth(db), smooth(mo)
    win = (t >= (peak if peak is not None else a) - 1.5) & (t <= (peak if peak is not None else b) + 1.5)
    if not win.any():
        win = np.ones_like(t, bool)
    loud_c = float(sdb[win].max() - base_db)
    mot_c = float(smo[win].max() / max(base_mo, 0.5))
    near = [w for w in words if peak is not None and peak - 2.5 <= w[0] <= peak + 2.5]
    react = any(REACT_RE.search(w[2]) for w in near)
    # one hook score from the three signals: 1.0 = e.g. +6 dB alone, or picture change x3 alone, or
    # a reaction word with +3.6 dB (pool clips 17. 11.: real moments 1.2-6, flat talk 0.8)
    hook = (max(0.0, loud_c) / BORE_LOUD_DB + min(max(0.0, mot_c - 1) / (BORE_MOTION_X - 1), 1.5)
            + (BORE_REACT if react else 0.0))
    # non-stop action (a chase, a shootout) has no contrast but is not boring: busy picture AND sound
    busy = base_mo >= BORE_BUSY_MOTION and base_db >= BORE_BUSY_DB
    payoff = busy or hook >= (0.6 if viral else 1.0)
    # dead stretch: quiet (well under the clip's level, or near silence) AND still AND nobody talking
    quiet = (db < max(base_db - 12, -45)) | (db < -50)
    still = mo < max(0.35 * base_mo, 0.4)
    talk = np.zeros_like(quiet)
    for w0, w1, _ in words:
        talk |= (t >= w0 - 0.2) & (t <= w1 + 0.2)
    dead_mask, run_, dead = quiet & still & ~talk, 0, 0.0
    for v in dead_mask:
        run_ = run_ + 1 if v else 0
        dead = max(dead, run_ * 0.2)
    probs = []
    if not payoff:
        probs.append(f"flat payoff (hook {hook:.2f}: loudness +{loud_c:.1f} dB, motion x{mot_c:.1f}, "
                     f"reaction {'yes' if react else 'no'})")
    if dead > BORE_DEAD_S:
        probs.append(f"dead stretch {dead:.1f} s (quiet and still)")
    return probs, {"hook": round(hook, 2), "loud_db": round(loud_c, 1), "motion_x": round(mot_c, 2), "react": react,
                   "busy": busy, "base_motion": round(base_mo, 1), "base_db": round(base_db, 1),
                   "dead_s": round(dead, 1), "viral": viral}

def boxes_hit(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]

def qa_short(path, plan):
    """
    Automatic QA of a finished short (Aleš 17. 11.: no room for mistakes). Returns problems:
      - duration as planned and in 6..95 s
      - no black / frozen stretch before the end card or outro (intentional ones are after it)
      - sound: not silent (integrated > -35 LUFS), -14 LUFS +- 2, true peak <= -1 dBTP
      - boredom: the gate before render passed, no dead stretch (> 2.5 s quiet + still) left
      - one extra element at a time; none over the face band or the captions (timeline boxes)
      - HOOK v2: the first 300 ms are at least as loud as the short's median
      - the end card / Heat planned is really there
    """
    probs = []
    try:
        d = duration(path)
    except Exception as e:
        return [f"unreadable ({str(e)[-80:]})"]
    if abs(d - plan["total"]) > 0.4:
        probs.append(f"duration {d:.1f} s, planned {plan['total']:.1f} s")
    if not 6 <= d <= 95:
        probs.append(f"duration {d:.1f} s out of range")
    black, frozen, lufs, tp = ff_stats(path, max(1.0, plan["main_end"] - 0.3))
    if black:
        probs.append(f"black frames at {', '.join(black[:3])} s")
    if frozen:
        probs.append(f"frozen picture at {', '.join(frozen[:3])} s")
    if lufs is None or lufs < -35:
        probs.append(f"audio silent ({lufs} LUFS)")
    if tp is None and lufs is not None:
        probs.append("true peak not measured")
    if tp is not None and tp > -1.0:
        probs.append(f"true peak {tp:.1f} dBTP (> -1)")
    if lufs is not None and lufs >= -35 and abs(lufs - LUFS_TARGET) > LUFS_TOL:
        probs.append(f"loudness {lufs:.1f} LUFS (target {LUFS_TARGET:.0f})")
    # boredom: the gate before render must have passed, and the finished short (what the viewer gets,
    # with our music) must have no dead stretch left between the hook and the end card / outro
    if plan.get("bore_probs"):
        probs.append(f"boring: {'; '.join(plan['bore_probs'])}")
    if plan.get("main_end", 0) - plan.get("hook_end", 0) >= 4:
        dead = boredom(path, [(w0, w1 - 0.6, "") for w0, w1 in plan.get("cap_times") or []], None,
                       plan.get("hook_end", 0.0), plan["main_end"], crop=plan.get("game_crop"))[1].get("dead_s", 0)
        if dead > BORE_DEAD_S:
            probs.append(f"dead stretch {dead:.1f} s in the short")
    els = plan.get("elements") or []
    for i, (t0, t1, name, box) in enumerate(els):
        # a full-frame end card after the story (experiments' tier board) may cover the face band
        if box[1] < plan["face_bottom"] and t0 < plan["main_end"] - 0.05:
            probs.append(f"{name} over the face band")
        # (the score card is the approved full-frame end card; the last caption may fade under it)
        if (name != "score card" and any(t0 < e1 and e0 < t1 for e0, e1 in plan.get("cap_times") or [])
                and boxes_hit(box, plan["cap_box"])):
            probs.append(f"{name} over the captions")
        for u0, u1, other, _ in els[i + 1:]:
            if t0 < u1 - 0.02 and u0 < t1 - 0.02:
                probs.append(f"{name} and {other} on screen at once")
    if plan.get("loud_hook") and not hook_is_loud(path, plan["main_end"]):
        probs.append("hook does not start with the loud part")
    if plan.get("card") and not (Path(plan["card"]).exists() and d >= plan["card_at"] + RATING_DUR - 0.3):
        probs.append("score card missing")
    # structure (CLAUDE.md): hook <= 2.5 s; after the climax >= 3 s of the clip before the card / the end
    if (plan.get("hook_end") or 0) > HOOK_MAX + 0.05:
        probs.append(f"hook {plan['hook_end']:.2f} s (max {HOOK_MAX})")
    if plan.get("peak") is not None:
        after = (plan["card_at"] if plan.get("card") else plan["main_end"]) - plan["peak"]
        if after < AFTER_PEAK - 0.05:
            probs.append(f"only {after:.1f} s after the climax (min {AFTER_PEAK:.0f})")
    if plan.get("heat_on") and not plan.get("heat"):
        probs.append("heat missing")
    # the game band of the finished short must show the game (18. 11.: a talk + YouTube page passed)
    if "game_crop" in plan and plan.get("main_end", 0) - plan.get("hook_end", 0) >= 4:
        probs += gameplay_check(path, plan.get("hook_end") or None, plan["main_end"], crop=plan.get("game_crop"))[0]
    # Aleš 18. 11.: no cut inside a sentence (edit_cut checked and re-snapped; what is left fails)
    for p in (plan.get("cuts") or {}).get("problems") or []:
        probs.append(f"cut: {p}")
    # ... and the music clearly under the speech while anyone talks (measured on the mix's stems)
    mx = plan.get("mix") or {}
    if mx.get("diff_min") is not None and mx["diff_min"] < MIX_MIN_DIFF:
        probs.append(f"music only {mx['diff_min']:.1f} dB under the speech (min {MIX_MIN_DIFF:.0f})")
    return probs

def extras_ass(path, reactions, rating, dur, cam, seed):
    """
    Our own layer on top of the captions (Aleš 15. 11.): a short speech bubble next to every
    banana reaction, and the banana's score for the clip in the last 1.6 s.
    """
    rnd = random.Random(seed)
    y0 = (CAM_H if cam else 600) + 520                   # banana's top edge (see reaction_overlay)
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
    y = (CAM_H if cam else 600) + 520                # left side of the game band, under the captions (no facecam: they sit at y 600)
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
    if os.environ.get("KLIPY_DEBUG"):
        (Path(os.environ["KLIPY_DEBUG"]) / f"cmd_{Path(out_path).stem}_{int(time.time())}.txt").write_text(
            "\n".join(map(str, cmd_base)), encoding="utf-8")
    # -cutoff 18000: ffmpeg's AAC at 128k with its default low-pass overshot a -3 dB limited mix to
    # +0.3 dBTP (measured 17. 11.); with the cutoff it stays at -2.4 dBTP (qa_short wants <= -1)
    for venc in (["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "23", "-b:v", "0"] + cap,
                 ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22"] + cap):
        try:
            run(cmd_base + venc + ["-c:a", "aac", "-b:a", "128k", "-cutoff", "18000", "-movflags", "+faststart", str(out_path)], timeout=900)
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

# Sentence-safe cuts (Aleš 18. 11.: "it's forbidden to cut in the middle of a sentence"). Every cut of a
# short and of a long-form moment lands between sentences, judged from Whisper's word times:
#   - a sentence ENDS after a word with . ? ! (Whisper's punctuation) or a pause >= END_GAP after it
#   - a sentence STARTS at the first word, after a sentence end, or after a pause >= START_GAP
#   - a cut point is never inside a word
# The hook teaser is the only intentional excerpt; its in/out points only have to sit between words.
START_GAP = 0.35         # pause before a word that lets a clip start there
END_GAP = 0.5            # pause after a word that ends the utterance (no punctuation needed)
JUMP_GAP = 1.6           # a jump cut without . ? ! before it needs a pause at least this long
SNAP_BACK = 4.0          # a start may move this much earlier to reach a sentence start
SNAP_FWD = 8.0           # an end may move this much later to reach a sentence end
WORD_TOL = 0.02
EDGE_TOL = 0.25          # a word this close to the source's start / end = the viewer's clip cut into speech
AFTER_PEAK = 3.0         # rule: at least this many seconds of the clip after the climax before the card / end
BUILDUP = 5.0            # skipping a partial first sentence must leave this much run-up before the payoff
HOOK_MAX = 2.5           # rule: the hook teaser is at most this long

def _punct_end(text):
    """Whisper ended the word with . ? ! (an ellipsis '...' is trailing off, not an end)."""
    t = text.rstrip("\"')]")
    return bool(t) and t[-1] in ".?!" and not t.endswith("..")     # ('' in '.?!' is True: a bare quote is no end)

# A pause after one of these is the speaker hesitating, not a finished sentence (18. 11.: a long-form
# moment ended on "...you got it. I" - the 0.5 s pause after "I" counted as an end).
DANGLING = {"i", "a", "an", "the", "and", "but", "or", "so", "to", "of", "for", "with", "my", "your", "his",
            "her", "our", "their", "like", "because", "if", "that", "this", "is", "was", "are", "gonna", "wanna",
            "im", "i'm", "it's", "we", "you", "he", "she", "they"}

def _dangling(text):
    """The word leaves the sentence open (a function word: 'I', 'the', 'and' ...) - punctuation aside."""
    return re.sub(r"[^\w']", "", text.lower().replace("’", "'")).strip("'") in DANGLING

def sent_end(words, i):
    """Word i ends a sentence: . ? !, or a pause >= END_GAP after it / the last word - but never after a
    dangling function word without punctuation ('... you got it. I' + pause is a cut sentence)."""
    if _punct_end(words[i][2]):
        return True
    if _dangling(words[i][2]):
        return False
    return i == len(words) - 1 or words[i + 1][0] - words[i][1] >= END_GAP

def sent_start(words, i):
    """Word i starts a sentence: the first word, after a sentence end or after a pause >= START_GAP
    (not after a dangling word: 'I ... think so' is one sentence)."""
    return (i == 0 or sent_end(words, i - 1)
            or (words[i][0] - words[i - 1][1] >= START_GAP and not _dangling(words[i - 1][2])))

def in_word(words, t, tol=WORD_TOL):
    """The word t falls inside (None = t is between words)."""
    return next((w for w in words if w[0] + tol < t < w[1] - tol), None)

def ok_start(words, t):
    """A clip may start at t: not inside a word and the next word starts a sentence."""
    if in_word(words, t):
        return False
    i = next((k for k, w in enumerate(words) if w[0] >= t - WORD_TOL), None)
    return i is None or sent_start(words, i)

def ok_end(words, t):
    """A clip may end at t: not inside a word and the last word before t ends a sentence."""
    if in_word(words, t):
        return False
    i = next((k for k in range(len(words) - 1, -1, -1) if words[k][1] <= t + WORD_TOL), None)
    return i is None or sent_end(words, i)

def _end_point(words, i, limit):
    """Cut point after sentence-end word i: 0.3 s of air, never into the next word."""
    w1 = words[i][1]
    nxt = words[i + 1][0] if i + 1 < len(words) else limit
    return min(limit, max(w1, min(w1 + 0.3, nxt - 0.03)))

def _gaps(words, lo, hi):
    """Pauses between words (g0, g1) that touch [lo, hi], longest first."""
    gs = [(w1[1], w2[0]) for w1, w2 in zip(words, words[1:]) if w2[0] > w1[1] and w2[0] >= lo and w1[1] <= hi]
    return sorted(gs, key=lambda g: g[1] - g[0], reverse=True)

def snap_start(words, t, back=SNAP_BACK, lo=0.0):
    """
    Start point at or before t that begins a sentence (the previous sentence start or a pause
    >= START_GAP, up to `back` s earlier). Nothing that close: the longest pause around t, else the
    start of the word t is in - never mid-word.
    """
    if not words or ok_start(words, t):
        return t
    for i in range(len(words) - 1, -1, -1):
        w0 = words[i][0]
        if w0 > t + WORD_TOL:
            continue
        if w0 < t - back:
            break
        if sent_start(words, i):
            prev = words[i - 1][1] if i else lo
            return max(lo, w0 - 0.15, (prev + w0) / 2 if w0 - prev < 0.3 else w0 - 0.15)
    for g0, g1 in _gaps(words, t - back, t + 1.0):
        return max(lo, g0, g1 - 0.15)
    w = in_word(words, t)
    return max(lo, w[0]) if w else t

def snap_end(words, t, limit, fwd=SNAP_FWD, back=0.0):
    """
    End point at or after t where a sentence ends (up to `fwd` s later, never past `limit`). None
    there: the last sentence end up to `back` s earlier, then the longest pause in the window, then
    the end (or start) of the word t is in - never mid-word.
    """
    t = min(t, limit)
    if not words or ok_end(words, t):
        return t
    for i, w in enumerate(words):
        if w[1] < t - WORD_TOL:
            continue
        if w[1] > min(limit, t + fwd):
            break
        if sent_end(words, i):
            return _end_point(words, i, limit)
    for i in range(len(words) - 1, -1, -1):
        w1 = words[i][1]
        if w1 > t:
            continue
        if w1 < t - back:
            break
        if sent_end(words, i):
            return _end_point(words, i, limit)
    for g0, g1 in _gaps(words, t - max(back, 1.0), min(limit, t + fwd)):
        if g0 <= limit:
            return min(limit, g1 - 0.03, g0 + min(0.3, (g1 - g0) / 2))
    w = in_word(words, t)
    if w:
        return w[1] if w[1] <= limit else w[0]
    return t

def cut_words(words, dur=None):
    """
    The words every cut decision is made on: the captions plus the dropped (BUSY) ones - Whisper
    heard something there, a cut must not land inside it - sorted by time. When the last word
    touches the source's end (< EDGE_TOL s before it) without . ? ! the source cut that sentence:
    a marker after the end keeps it from counting as a sentence end.
    """
    ws = sorted(list(words) + list(getattr(words, "busy", None) or []))
    if (dur is not None and ws and ws[-1][0] <= dur and ws[-1][1] > dur - EDGE_TOL
            and not _punct_end(ws[-1][2])):
        ws.append((dur + 0.05, dur + 0.3, BUSY))
    return ws

def skip_partial_start(words, a, peak, b, lo=8.0):
    """
    A viewer's clip that starts mid-speech (first word < EDGE_TOL s in): its first 'sentence' is a
    fragment of one. Start at the next sentence start instead - unless that cuts into the payoff's
    run-up (< BUILDUP s before the peak) or leaves less than `lo` s. Returns the new start (or a).
    """
    if not words or words[0][0] >= EDGE_TOL or a > words[0][0] + WORD_TOL:
        return a
    i = next((k for k in range(1, len(words)) if sent_start(words, k)), None)
    if i is None:
        return a
    w0, prev = words[i][0], words[i - 1][1]
    na = (prev + w0) / 2 if w0 - prev < 0.3 else w0 - 0.15
    if (peak is not None and na > peak - BUILDUP) or b - na < lo:
        return a
    return na

def end_from(words, t, floor, limit):
    """
    Sentence-safe end point >= floor (payoff + AFTER_PEAK), at or after t, never past limit; None
    when there is none (the speech runs on to the source's end).
    """
    t = min(max(t, floor), limit)
    if ok_end(words, t):
        return t
    for i, w in enumerate(words):
        if w[1] < t - WORD_TOL:
            continue
        if w[0] > limit:
            break
        if sent_end(words, i):
            p = _end_point(words, i, limit)
            if p >= floor - 1e-6 and ok_end(words, p):
                return p
    return limit if ok_end(words, limit) else None

def word_edge(words, t, lo=0.0, hi=1e9):
    """t moved out of a word to the nearer word edge inside [lo, hi] (hook teaser in/out points)."""
    w = in_word(words, t)
    if not w:
        return t
    before, after = w[0] - 0.02, w[1] + 0.02
    opts = [x for x in (before, after) if lo <= x <= hi] or [before]
    return min(opts, key=lambda x: abs(x - t))

def voice_stop(words, b, e):
    """
    Under the score card the clip keeps running from b to e (live footage). Where must the clip's
    own sound stop so no sentence is cut at e? None = it may run to e (e is between sentences).
    Else the end of the last whole sentence in [b, e], or b (the clip's end, a sentence end).
    """
    if e - b < 0.05 or ok_end(words, e):
        return None
    best = None
    for i, w in enumerate(words):
        if b - WORD_TOL <= w[1] <= e and sent_end(words, i):
            p = _end_point(words, i, e)
            if p < e - 0.05:
                best = p
    if best is not None:
        return best
    return b if ok_end(words, b) else snap_end(words, b, e, fwd=e - b, back=2.0)

def voice_new(words, b, card, end, dur):
    """voice_stop for a short whose clip sound runs to `end` (new timeline; the source b..dur plays
    continuously from `card` on). Returns the stop in the new timeline or None."""
    vs = voice_stop(words, b, min(dur, b + max(0.0, end - card)))
    return None if vs is None else card + (vs - b)

def cut_problems(words, a, b, segs=(), hook=None):
    """
    The cut check (qa): every boundary of the clip between sentences, judged on the source's word
    times. a/b = clip start/end, segs = kept pieces (jump cuts between them), hook = (in, out) of
    the payoff teaser. Returns a list of problems (empty = clean).
    """
    probs = []
    if not ok_start(words, a):
        w = in_word(words, a)
        probs.append(f"start {a:.2f} s " + (f"inside the word '{w[2]}'" if w else "inside a sentence"))
    if not ok_end(words, b):
        w = in_word(words, b)
        probs.append(f"end {b:.2f} s " + (f"inside the word '{w[2]}'" if w else "inside a sentence"))
    for (_, x), (y, _) in zip(segs, segs[1:]):
        gone = [w for w in words if w[0] < y - WORD_TOL and w[1] > x + WORD_TOL]
        if gone or not ok_end(words, x) or not ok_start(words, y):
            probs.append(f"jump cut {x:.2f}-{y:.2f} s inside a sentence" + (f" (removes '{gone[0][2]}')" if gone else ""))
    if hook:
        for name, t in zip(("in", "out"), hook):
            w = in_word(words, t)
            if w:
                probs.append(f"hook {name} {t:.2f} s inside the word '{w[2]}'")
    return probs

def fix_cuts(words, a, b, segs, hook, dur, peak=None):
    """
    Repair what cut_problems finds by re-snapping: start/end to sentence borders, a jump cut inside
    a sentence is dropped (its pieces joined), hook edges to word edges. peak = the payoff: the end
    never snaps to less than AFTER_PEAK s after it - no sentence end there = the end moves forward
    (up to the source's end). Returns (a, b, segs, hook, fixed, left): fixed = problems found and
    repaired, left = still there.
    """
    found = cut_problems(words, a, b, segs, hook)
    floor = min(dur, peak + AFTER_PEAK) if peak is not None else None
    if floor is not None and b < floor - 0.01:
        found.append(f"end {b:.2f} s only {b - peak:.1f} s after the payoff")
    if hook and hook[1] - hook[0] > HOOK_MAX + 0.02:
        found.append(f"hook {hook[1] - hook[0]:.2f} s long")
    if not found:
        return a, b, segs, hook, [], []
    if not ok_start(words, a):
        a = snap_start(words, a)
    if not ok_end(words, b):
        b = snap_end(words, b, dur, back=4.0)
    if floor is not None and (b < floor - 0.01 or not ok_end(words, b)):
        nb = end_from(words, b, floor, dur)
        b = nb if nb is not None else max(b, floor)    # no sentence end after the payoff: cut check flags it
    keep = []
    for x, y in segs:
        x, y = max(x, a), min(y, b)
        if y - x <= 0.05:
            continue
        if keep:
            px = keep[-1][1]
            gone = [w for w in words if w[0] < x - WORD_TOL and w[1] > px + WORD_TOL]
            if gone or not ok_end(words, px) or not ok_start(words, x):
                keep[-1] = (keep[-1][0], y)            # jump inside a sentence: keep it continuous
                continue
        keep.append((x, y))
    if not keep:
        keep = [(a, b)]
    keep[0] = (a, keep[0][1])
    keep[-1] = (keep[-1][0], b)
    if hook:
        h0 = word_edge(words, hook[0], 0.0, hook[1] - 0.5)
        hook = (h0, word_edge(words, min(hook[1], h0 + HOOK_MAX), h0 + 0.5, min(dur, h0 + HOOK_MAX)))
    left = cut_problems(words, a, b, keep, hook)
    if floor is not None and b < floor - 0.01:
        left.append(f"end {b:.2f} s only {b - peak:.1f} s after the payoff (the source ends)")
    if hook and hook[1] - hook[0] > HOOK_MAX + 0.02:
        left.append(f"hook {hook[1] - hook[0]:.2f} s long (max {HOOK_MAX})")
    return a, b, keep, hook, found, left

def snap_cut(words, s, e, total, lo=15.0, hi=40.0):
    """
    Cut points in the padded download: start just before a sentence begins near s, end just
    after a sentence ends a few seconds past the peak (the window centre), 15-40 s long.
    Both points are then snapped to sentence borders (snap_start / snap_end).
    """
    if not words:
        return s, min(total, e)
    a, b = _snap_cut(words, s, e, total, lo, hi)
    a = snap_start(words, a)
    return a, snap_end(words, max(b, a + 1.0), total, back=4.0)

def _snap_cut(words, s, e, total, lo, hi):
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
    a, b = snap_cut(cut_words(words), s - ps, e - ps, duration(raw))
    encode(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{a:.2f}", "-to", f"{b:.2f}",
            "-i", str(raw), "-map", "0:v:0", "-map", "0:a?"], dst)
    raw.unlink(missing_ok=True)
    clip["section"] = [round(float(ps + a), 1), round(float(ps + b), 1)]
    log(f"  cut on sentence pauses: {clip['section']} ({b - a:.0f} s)")
    shift = lambda ws: [(w0 - a, w1 - a, t) for w0, w1, t in ws if w0 >= a and w1 <= b]
    return Words(shift(words), shift(getattr(words, "busy", [])))

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
        end_sent = next((w[1] for w in words if w[0] >= peak - 0.5 and _punct_end(w[2])), None)
        end_word = next((w[1] for w in words if w[1] >= peak + 2.5), None)       # no punctuation: end of a word
        # Aleš 17. 11.: after the climax leave a few seconds - the reaction and the sentence that is
        # running finish, never cut mid-sentence (the xQc test ended just barely)
        end = end_sent if end_sent and end_sent <= peak + 6 else (end_word if end_word and end_word <= peak + 6 else peak + AFTER_PEAK)
        b = min(dur, max(end + 0.5, peak + AFTER_PEAK))
    elif words and dur - words[-1][1] > 2:
        b = words[-1][1] + 0.8
    if b - a > hi:                                 # too long: keep the 35 s around the payoff, never cut it off
        a = max(a, min(peak - 8, b - hi)) if peak is not None else b - hi
        b = min(b, a + hi)
    if b - a < lo:
        return 0.0, dur
    # Aleš 18. 11.: never start or end inside a sentence - start on a sentence start (<= 4 s
    # earlier), end on a sentence end (<= 8 s later), never less than AFTER_PEAK s after the payoff
    a = snap_start(words, a)
    a = skip_partial_start(words, a, peak, b, lo)     # the viewer's clip began mid-sentence
    floor = min(dur, peak + AFTER_PEAK) if peak is not None else a + lo
    nb = snap_end(words, b, dur, back=max(0.0, b - floor))
    if peak is not None and (nb < floor - 0.01 or not ok_end(words, nb)):
        fe = end_from(words, b, floor, dur)           # no sentence end around b: the next one after the payoff
        nb = fe if fe is not None else max(nb, floor)
    return a, nb

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
    Only BETWEEN sentences (Aleš 18. 11.): the word before the pause ends with . ? ! or the pause
    is >= JUMP_GAP; a hesitation inside a sentence stays.
    """
    ts, rms = rms_curve(src)
    if not words or rms.size == 0:
        return [(a, b)]
    med = float(np.median(rms))
    keep, cur = [], a
    inside = [w for w in words if a <= w[0] and w[1] <= b]
    for w1, w2 in zip(inside, inside[1:]):
        g0, g1 = w1[1] + 0.2, w2[0] - 0.15
        # never near the payoff, and never in the AFTER_PEAK s after it (the reaction must stay whole):
        # a pause reaching into that stretch is only cut after it
        if peak is not None and g0 - 1.0 < peak < g1 + 1.0:
            continue
        if peak is not None and peak < g0 < peak + AFTER_PEAK + 0.5:
            g0 = peak + AFTER_PEAK + 0.5
        if g1 - g0 < 0.6:
            continue
        if not _punct_end(w1[2]) and (w2[0] - w1[1] < JUMP_GAP or _dangling(w1[2])):
            continue                                   # a pause inside a sentence: never cut there
        m = (ts >= g0) & (ts <= g1)
        if m.any() and rms[m].mean() < 0.6 * med:
            keep.append((cur, g0))
            cur = g1
    keep.append((cur, b))
    return [(x, y) for x, y in keep if y - x > 0.05]

def edit_cut(src, words, d, clip, hpc=None, v2=False, info=None):
    """
    Cut the source before rendering: trim (Twitch/Kick; YouTube windows are already cut on
    sentences) and a COLD START - the payoff flashes for 1 s first, then the clip plays from
    its start (Aleš 15. 11.). Up to RATING_DUR s of the source after the cut are kept, so the
    clip keeps running under the banana score card (Aleš: the background must not freeze).
    Returns (source, words, payoff time, where the card starts, jump cuts, T) in the new
    timeline; T = where the hook ends and the clip starts (0 = no hook). The transition itself
    (flash or whip cut) is the brand's and is added in the final encode (build_filter).
    v2 = HOOK v2 (brands.json hook_v2): frame 0 is already the most intense moment - the hook
    starts where the next 300 ms are the loudest (the payoff reaction), just before the payoff,
    and lasts <= 2.3 s; info['hook_peak'] = the payoff inside the hook (facecam punch there).
    """
    dur = duration(src)
    peak = audio_peak(src)
    info = info if info is not None else {}
    cw = cut_words(words, dur)                        # + dropped speech, + a sentence the source cut off
    a, b = (0.0, dur) if clip.get("section") else trim_points(cw, peak, dur)
    tease = peak is not None and peak - a >= 4 and b - peak >= 0.3
    segs = dead_air(cw, src, a, b, peak)
    # hook (Aleš: 1 s looked like a cut by mistake; the rewind he did not like): ~2.5 s of the
    # payoff (rule: <= HOOK_MAX), then the transition (bananas: white FLASH + whoosh) and the clip
    # from its start. The teaser is the one intentional excerpt; its in/out points sit between words.
    ps, pe = (max(0.0, peak - 1.6), min(dur, peak + 0.9)) if tease else (0, 0)
    if tease and v2:
        ps = loud_start(src, peak)
        pe = min(dur, ps + 2.3, b + RATING_DUR)
    if tease:
        ps = word_edge(cw, ps, 0.0, peak - 0.05)
        pe = word_edge(cw, min(pe, ps + HOOK_MAX), max(ps + 1.0, peak + 0.05), min(dur, ps + HOOK_MAX))
    # cut check (Aleš 18. 11.): no boundary inside a word or a sentence; repaired by re-snapping;
    # the end stays >= AFTER_PEAK s after the payoff
    a, b, segs, hook, fixed, left = fix_cuts(cw, a, b, segs, (ps, pe) if tease else None, dur, peak)
    if peak is not None and dur - peak < AFTER_PEAK - 0.05:
        # the source ends too soon after the payoff: no look can give the 3 s (rule) - fail before encoding
        raise RenderFailed(f"only {dur - peak:.1f} s after the climax before the source ends (min {AFTER_PEAK:.0f})")
    if hook:
        ps, pe = hook
    if tease and v2:
        info["hook_peak"] = round(peak - ps, 3)
    sent = [w for w in words if a - WORD_TOL <= w[0] and w[1] <= b + WORD_TOL]
    info["cuts"] = {"start": round(a, 2), "end": round(b, 2), "jumps": len(segs) - 1, "fixed": fixed, "problems": left,
                    "first": " ".join(w[2] for w in sent[:6]), "last": " ".join(w[2] for w in sent[-6:])}
    if fixed or left:
        log(f"  cut check: {'; '.join(fixed)}" + (f" -> still: {'; '.join(left)}" if left else " -> repaired"))
    log(f"  cut starts '{info['cuts']['first']}' ... ends '{info['cuts']['last']}'")
    if hpc is not None:                               # long-form keeps the cut on the 16:9 source
        hpc.update(peak=peak, cut=[round(a, 2), round(b, 2)], dur=round(dur, 2))
    e = b + min(RATING_DUR, dur - b)                  # live footage for under the card
    segs[-1] = (segs[-1][0], e)                       # the tail under the card stays continuous
    if not tease and a < 0.5 and b > dur - 0.5 and len(segs) == 1:
        # untouched: the card covers the last seconds, but only if the payoff is safely before them
        late = peak is not None and dur - peak >= RATING_DUR + AFTER_PEAK
        card = dur - RATING_DUR if late else dur
        info["voice_at"] = lambda end, card=card: voice_new(cw, card, card, end, dur)
        return src, words, peak, card, [], 0.0
    if not has_audio(src):
        return src, words, peak, dur, [], 0.0
    out = d / ("edit_v2.mp4" if v2 else "edit_old.mp4")    # each look its own file (and player detection)
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
    info["voice_at"] = lambda end, b=b, card=card: voice_new(cw, b, card, end, dur)
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

def zoom_jumps(words, jumps, dur, hook=None):
    """
    Alternating punch-in (1.0 <-> 1.08) for the game band: switches on every jump cut and on
    new phrases, at most every 1.6 s - the picture never sits still (ADHD-proof pacing).
    hook = end of a HOOK v2 (s): nothing inside the hook (its facecam punch is the change there),
    then a forced switch every 1.2 s until 5 s. Returns the zoomed-in intervals.
    """
    marks = set(jumps)
    prev_end = None
    for w0, w1, _ in words:
        if prev_end is None or w0 - prev_end > 0.35:
            marks.add(round(w0, 2))
        prev_end = w1
    forced = set()
    if hook:
        marks = {m for m in marks if m >= hook + 0.3}
        forced = {round(hook + 1.2 * k, 2) for k in range(1, 4) if hook + 1.2 * k < 5.0}
    out, last = [], (hook if hook else -9.0)
    for t in sorted(marks | forced):
        if t - last >= (1.0 if t in forced else 1.6) and 0.3 < t < dur - 0.3:
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
        raise NotGameplay("not gameplay: streamer's face fills the frame")
    gp_probs, gp_stats = gameplay_check(src)  # every source: a face cam / web page / desktop is not the game
    log(f"  gameplay: face {gp_stats.get('face')}, not-game windows {gp_stats.get('bad')}, "
        f"motion {gp_stats.get('motion')}" + (f" -> {'; '.join(gp_probs)}" if gp_probs else " ok"))
    if gp_probs:
        raise NotGameplay("; ".join(gp_probs))
    if words is None:
        words = transcribe(src)
    speech = " ".join(t for _, _, t in words)
    if SLUR_RE.search(speech):
        raise NotGameplay("slur in the speech - not worth a strike or a ban")
    (d / "transcript.txt").write_text(speech, encoding="utf-8")
    (d / "mark.txt").write_text(f"@{clip['channel']}", encoding="utf-8")
    orig = src                                         # facecam is found on the uncut source (the edit fooled it)
    words0, hpc = words, {}
    # which accounts get it (docs/dva-ucty.md); each gets its own final encode in its own style,
    # everything above (download, Whisper) and the player detection are done once
    bcfg = brands()
    blist = [brand_cfg(bcfg, n) for n in assign_brands(clip, bcfg)]
    log(f"  brands: {', '.join(b['key'] for b in blist)}")
    raw = (int(round(5 + 5 * clip["signal_score"])) if clip.get("platform") == "youtube" and "signal_score" in clip
           else live_rating(clip.get("spike_z")) if clip.get("live")
           else banana_rating(clip.get("views")))
    rating, gold = raw, False
    if any(b.get("golden10") or b.get("heat") for b in blist):
        try:                                           # golden 10/10 = top 3 %, every other clip max 9
            r10, gold = golden_rating(clip, raw)
            if any(b.get("golden10") for b in blist):
                rating = r10
        except Exception as e:
            log(f"  golden rating failed ({str(e)[-80:]}), plain score")
    heat_n = heat_level(raw, gold)
    rank = league_rank(clip["channel"]) if any(b.get("league_chip") for b in blist) else None
    rank = rank if rank and rank <= 10 else None       # the chip only for this week's top 10
    _detections.clear()
    whoosh = HERE / "assets" / "sfx" / "flash.wav"
    whip = HERE / "assets" / "sfx" / "whip.wav"
    riser, hit = HERE / "assets" / "sfx" / "riser.wav", HERE / "assets" / "sfx" / "hit.wav"
    cfg = load_json(SOURCES, {})
    # YouTube: video views are not clip popularity, and a window without a facecam is someone's
    # commentary / trailer re-upload (18. 11.: three such picks) -> YouTube needs the streamer's face
    nocam_ok = clip.get("platform") != "youtube" and (
        (clip.get("views") or 0) >= cfg.get("nocam_min_views", 5000) or (clip.get("cluster") or 1) >= 2)

    # Look passes: first the launch look (brands with any LAUNCH_FLAGS on), then - only for the
    # brands whose short failed QA - the proven old look; a short failing even that is dropped.
    shorts, qa_log, todo, not_game = {}, {}, list(blist), []
    looks = [True, False] if any(b.get(k) for b in blist for k in LAUNCH_FLAGS) else [False]
    cuts, bore, mtags = {}, None, {}
    for v2 in looks:
        if not todo:
            break
        hv2_cut = v2 and all(b.get("hook_v2") for b in todo)   # one cut for all: any brand off -> proven hook
        if hv2_cut not in cuts:
            info = {}
            try:                                          # a crash in the launch cut -> the old look, never a lost clip
                cuts[hv2_cut] = (edit_cut(orig, words0, d, clip, hpc, v2=hv2_cut, info=info), info)
            except NotGameplay:
                raise
            except Exception as e:
                if v2 and False in looks:
                    log(f"  launch cut crashed ({str(e)[-160:]}) -> old look")
                    continue
                raise
        (src, words, peak, card_at, jumps, hook_t), info = cuts[hv2_cut]
        words_cut = words                                  # all words of the cut (the mix ducks under them)
        dur = duration(src)
        card_at = min(card_at, dur)
        words = [w for w in words if w[0] < card_at]      # no captions / banana under the score card
        if bore is None:                                  # boredom gate, once per clip, on the cut it will show
            viral = bool(virality(clip) >= cfg.get("extra_score", 5000) or (clip.get("cluster") or 1) >= 2
                         or (clip.get("live") and (clip.get("spike_z") or 0) >= 10))
            try:
                bore_probs, bore = boredom(src, words, peak, hook_t, card_at, viral=viral)
            except Exception as e:                        # a broken probe is not a boring clip
                bore_probs, bore = [], {"error": str(e)[-120:]}
            if os.environ.get("KLIPY_NO_BORE"):          # manual test renders only
                bore_probs = []
            log(f"  boredom: {bore}" + (f" -> {'; '.join(bore_probs)}" if bore_probs else " ok"))
            if bore_probs:
                raise RenderFailed(f"boring: {'; '.join(bore_probs)}")
        # swears anywhere in the clip's sound - also under the score card / fanpage tail, where the clip
        # keeps playing - are bleeped; captions (F***) stay before the card only
        words, mute = swear_plan(words_cut, card_at)
        reactions = pick_reactions(words, card_at, clip["id"], peak) if (BANANA / "reakce1.mp4").exists() else []
        if hv2_cut and hook_t:                            # hook = teaser -> flash -> start, nothing else in it
            reactions = [r for r in reactions if r[0] >= hook_t + 0.2]
        ff = ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)]

        # Render, check, and REPAIR instead of giving up (Aleš: a cut face or black bars must be fixed):
        #   1) normal: saved/auto facecam box, detected bars and subtitles cropped
        #   2) facecam detected again with a wider box, bars found with looser limits, 6 % extra zoom
        #   3) only for a clip with viral potential: no facecam band, the game fills the short
        # No facecam = no short, unless the clip is big (Aleš 15. 11.: "pustit i bez kamery, pokud má
        # potenciál být virální"): sources.json nocam_min_views, or several viewers clipped it.
        #   (between 2 and 3) "content": big black areas of any size cut off by the ever-not-black box
        #   (Aleš: a viral clip is a shame to throw away because of a black bar)
        modes = [("saved", False, 0.0), ("again", True, 0.06), ("content", True, 0.03)] + ([("none", True, 0.0)] if nocam_ok else [])
        problems, made, plans = [], {}, {}
        try:                                          # a crash in the launch look -> the old look, never a lost clip
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
                problems, made, plans = [], {}, {}
                for bi, b in enumerate(todo):
                    fan = b.get("style", "fanpage" if b["key"] == "fanpage" else "bananas") == "fanpage"
                    sfx = "" if b["key"] == "bananas" else f"_{b['key']}"
                    # the launch elements of this brand in this pass (brands.json flags)
                    hv2 = v2 and hv2_cut and bool(b.get("hook_v2")) and hook_t > 0
                    use_sticker = v2 and bool(b.get("model_sticker")) and fan
                    use_heat = v2 and bool(b.get("heat")) and fan
                    use_gold = v2 and gold and bool(b.get("golden10")) and rating == 10
                    out = d / b["short"]
                    # fanpage (Aleš 17. 11.: a different edit, not a recolour): taller facecam, one-word
                    # captions low in the game band, slow push-in per shot, instant replay of the payoff
                    cam_h = 720 if fan else CAM_H
                    game_h = H - cam_h if cam else H
                    crop_w = round(W * crop_h / game_h)           # source width that fills the game band at full height
                    xs, lo, hi, cuts_ = track_path(src, cam, crop_w, dur, xlim)
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
                    cap_y = cam_h if cam else 600
                    if fan:   # Anton white, neon green highlight, thin dark outline (sleek, not the banana look)
                        n_chunks = captions_ass(words, ass, y=cap_y,     # on the seam (Aleš)
                                                cap={**(b.get("caption") or {}), "outline": 3, "shadow": 0},
                                                per=1, anim="bounce")
                    else:
                        cap_y = CAM_H if cam else 600
                        n_chunks = captions_ass(words, ass, y=cap_y, cap=b.get("caption"))   # face/game seam
                    card = RATING / (GOLD_CARD if use_gold and (RATING / GOLD_CARD).exists() else f"score_{rating}.webm")
                    use_card = bool(b.get("score_card")) and card.exists()
                    if b.get("score_card"):
                        extras_ass(ass, reactions, None if card.exists() else rating, card_at, cam, clip["id"])   # (+ text score)
                    rx = reactions if b.get("banana") else []
                    args = (cam, ff_path(cmds), int(xs[0]), crop_w, game_h, crop_h, top)
                    replay, push = None, None
                    main_end = dur if b.get("score_card") else min(dur, card_at + 0.4)   # no card: ends with the story
                    # HOOK v2 pacing: a visual change at least every ~1.2 s in the first 5 s
                    pace = [round(hook_t + 1.2 * k, 2) for k in range(1, 4) if hook_t + 1.2 * k < 5.0] if hv2 else []
                    if fan:
                        r0, r1 = (max(0.0, peak - 1.0), min(card_at, peak + 0.5)) if peak is not None else (0.0, 0.0)
                        if False and r1 - r0 >= 0.8:              # replay off (Aleš 17. 11.: looked like the hook again + a freeze)
                            main_end = card_at
                            replay = (main_end, r0, r1)
                        edges = sorted({0.0, round(hook_t, 2), *jumps, *(round(c, 2) for c in cuts_), *pace, round(main_end, 2)})
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
                    # the timeline of extra elements: one at a time, each in the bottom-left slot
                    elements, sprites, heat_plan = [], [], None
                    y_ban = (CAM_H if cam else 600) + 520
                    for t, name in rx:
                        elements.append((t, t + REACTIONS[name][2] - REACTIONS[name][1], f"banana {name}",
                                         (BANANA_X, y_ban, BANANA_X + BANANA_W, y_ban + BANANA_H)))
                    if use_card:
                        elements.append((card_at, total, "score card", (0, cam_h if cam else 0, W, H)))
                    sticker_box = (SLOT_X, SLOT_BOTTOM - STICKER_H, SLOT_X + STICKER_W, SLOT_BOTTOM)
                    plan_stickers = []
                    heat_end = main_end if outro or not follow else min(main_end, total - FOLLOW_DUR - 0.1)
                    if use_sticker:
                        # the model's reaction VIDEO in the banana's place and size (bottom-left of the
                        # game band, under the captions), whole 1.8 s; in the hook (cut before the
                        # transition) and on the payoff; the kind from the moment's tag
                        if "tags" not in mtags:
                            try:
                                mtags["tags"] = moment_tags(words0, orig, hpc.get("peak"), clip.get("title"),
                                                            clip.get("created") or 0)[0]
                            except Exception:
                                mtags["tags"] = []
                        kinds = model_kinds(mtags["tags"])
                        hook_k = None
                        if hv2 and hook_t >= 1.0:             # in the hook, on the scream
                            r = model_reaction(kinds, f"{clip['id']}:hook")
                            if r:
                                hook_k = r[0]
                                plan_stickers.append((0.12, r[1], min(MODEL_DUR, hook_t - 0.17)))
                        if peak is not None and peak >= hook_t + 0.3:
                            t0 = peak - 0.1
                            room = min(main_end - 0.3, heat_end - 0.2) - t0
                            r = model_reaction(kinds, f"{clip['id']}:peak", avoid=(hook_k,))
                            if r and room >= 1.2:             # the payoff reaction (Aleš) goes before Heat
                                plan_stickers.append((t0, r[1], min(MODEL_DUR, room)))
                    rx_end = max([t0 + dur_ for t0, _, dur_ in plan_stickers] + [0.0])
                    if use_heat:                                  # HEAT over the last 2.5 s before the outro
                        hs = max(heat_end - HEAT_DUR, (peak + 0.6) if peak is not None else 0.0, hook_t + 0.5,
                                 rx_end + 0.2 if rx_end else 0.0)
                        if heat_end - hs >= 1.6:
                            heat_plan = (hs, heat_end - hs)
                        elif outro:
                            # Aleš 17. 11.: the payoff reaction pushed Heat out -> the badge goes over the
                            # first ~2 s of the outro instead, at the top (her face sits lower in the outro),
                            # and is gone before the Follow card comes at the outro's end: one at a time
                            o0 = clip_total + 0.15
                            o1 = min(o0 + HEAT_OUTRO_DUR, (total - FOLLOW_DUR if follow else total) - 0.25)
                            if o1 - o0 >= 1.6:
                                heat_plan = (o0, o1 - o0, "outro")
                                log(f"  heat: no room before the outro ({heat_end - hs:.1f} s) -> over the outro's start")
                            else:
                                log("  heat: no room before or in the outro, skipped")
                                use_heat = False
                        else:
                            log(f"  heat: no room after the climax / the model's reaction ({heat_end - hs:.1f} s), skipped")
                            use_heat = False
                    if use_sticker:
                        for k, (t0, f_, dur_) in enumerate(plan_stickers):
                            try:
                                if f_.suffix == ".webm":
                                    sy = SLOT_BOTTOM - BANANA_H
                                    sprites.append((t0, str(f_), BANANA_X, sy, dur_, BANANA_W, BANANA_H))
                                    box = (BANANA_X, sy, BANANA_X + BANANA_W, SLOT_BOTTOM)
                                else:                         # fallback: the still with a spring pop
                                    dur_ = STICKER_DUR
                                    pattern, sx, sy = sticker_frames(f_, d / f"_sticker{sfx}_{k}")
                                    sprites.append((t0, pattern, sx, sy))
                                    box = sticker_box
                                elements.append((t0, t0 + dur_, f"model {f_.stem}", box))
                            except Exception as e:
                                log(f"  model reaction {f_.name} failed ({str(e)[-80:]}), skipped")
                    if v2 and use_card and rank and b.get("league_chip") and total - (card_at + 1.75) > 0.4:
                        try:                                  # part of the score card (same element), after the score lands
                            cp, cx, cy = chip_frames(f"#{rank} THIS WEEK", d / f"_sticker{sfx}_chip", total - (card_at + 1.75))
                            sprites.append((card_at + 1.75, cp, cx, cy))
                        except Exception as e:
                            log(f"  league chip failed ({str(e)[-80:]})")
                    heat_wav = heat_pops = None
                    if heat_plan:
                        try:
                            hp, hx, hy, heat_wav, heat_pops = heat_frames(heat_n, heat_plan[1], d / f"_heat{sfx}")
                            hb = (SLOT_X, SLOT_BOTTOM - HEAT_H, SLOT_X + HEAT_W, SLOT_BOTTOM)
                            if heat_plan[2:] == ("outro",):           # same badge, moved to the top band
                                hy, hb = hy - (SLOT_BOTTOM - HEAT_H - HEAT_OUTRO_Y), (SLOT_X, HEAT_OUTRO_Y, SLOT_X + HEAT_W,
                                                                                     HEAT_OUTRO_Y + HEAT_H)
                            sprites.append((heat_plan[0], hp, hx, hy))
                            elements.append((heat_plan[0], heat_plan[0] + heat_plan[1], f"heat {heat_n}", hb))
                        except Exception as e:
                            log(f"  heat failed ({str(e)[-80:]})")
                            heat_plan = None
                    hook_peak = info.get("hook_peak") if hv2 else None
                    flt = reaction_overlay(build_filter(
                        *args, ass_file=ff_path(ass), zoom_at=None if fan else peak, beat=beat,
                        punch=None if fan else zoom_jumps(words, jumps, card_at, hook=hook_t if hv2 else None),
                        mark_file=ff_path(d / "mark.txt"), nf=src_norm(src),
                        grade=",".join(x for x in (b.get("grade"), FAN_LOOK if fan else "") if x),
                        punch_z=float(b.get("punch") or 1.08) - 1, wm_text=b.get("watermark") or BRAND,
                        hook=(b.get("hook") or "flash", hook_t) if hook_t else None, cam_h=cam_h, push=push,
                        replay=replay, cam_punch=(hook_peak, hook_t) if hook_peak is not None else None), rx, cam)
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
                    elif hv2 and (b.get("hook") or "flash") == "cut" and whip.exists():
                        inputs += ["-i", str(whip)]               # HOOK v2: a short air swish on the hard cut
                        sfx_idx, sfx_at = idx, max(0.0, hook_t - 0.12)
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
                        follow_idx, nxt = nxt, nxt + 1
                    extra_audio = []
                    if sprites:                                   # model stickers + Heat (times < the outro)
                        flt = sprite_overlay(flt, sprites, nxt)
                        inputs += sprite_inputs(sprites)
                        nxt += len(sprites)
                    if heat_plan and heat_wav:
                        inputs += ["-i", str(heat_wav)]
                        extra_audio.append((nxt, heat_plan[0], 0.8))
                        nxt += 1
                    # the clip's own sound: never ends inside a sentence (fades before the next one), speech
                    # normalised first, music ducked under every sentence (Aleš 18. 11.)
                    a_end = clip_total if outro else total
                    v_end = info["voice_at"](a_end) if info.get("voice_at") and not replay else None
                    v_end = v_end if v_end is not None and v_end < a_end - 0.05 else None
                    vgain, menv, mixinfo = (0.0, None, {})
                    hgain = hook_level(src, hook_t, card_at) if hook_t else None
                    if has_audio(src):
                        try:
                            vgain, menv, mixinfo = mix_levels(src, words_cut, a_end, mpath, mstart, mvol, v_end, hgain)
                        except Exception as e:            # a broken probe: the old steady mix
                            log(f"  mix levels failed ({str(e)[-120:]}), steady music")
                    # bleep only while the clip's own sound plays (not over a faded tail or the outro)
                    mute_b = swear_plan(words_cut, card_at, v_end if v_end is not None else a_end)[1]
                    mix = audio_mix(has_audio(src), total, card_idx, card_at, music_idx, mvol, sfx_idx, sfx_at, replay,
                                    mute_b, riser_idx, hit_idx, peak, clip_end=clip_total if outro else None,
                                    outro_idx=outro_idx, follow_idx=follow_idx, follow_at=total - FOLLOW_DUR,
                                    extras=extra_audio, hook_gain=hgain, voice_gain=vgain, music_env=menv, voice_end=v_end)
                    if menv:
                        try:
                            chk = mix_check(src, words_cut, a_end, mpath, mstart, vgain, menv, v_end, d, hgain)
                            mixinfo.update(chk or {})
                        except Exception as e:
                            log(f"  mix check failed ({str(e)[-120:]})")
                    log(f"  {b['key']}: mix {mixinfo}" + (f", clip voice stops at {v_end:.2f} s" if v_end else ""))
                    maps = inputs + ["-filter_complex", flt + (mix or ""), "-map", "[v]"] + (["-map", "[aud]"] if mix else [])
                    if not outro and not use_card and not replay and total < dur - 0.01:
                        maps += ["-t", f"{total:.3f}"]
                    enc = encode(ff + reaction_inputs(rx) + maps, out)
                    problems = [x for x in quality_check(out, cam, cam_h, until=card_at)
                                if not (x == "no facecam" and cam_mode == "none")]
                    if problems:
                        break
                    try:                                  # -14 LUFS / TP <= -1.2 measured (qa_short checks it)
                        ln = loudness_fix(out)
                        if ln:
                            log(f"  {b['key']}: loudness {ln[0]:.1f} -> {ln[1]:.1f} LUFS, true peak {ln[2]:.1f} dBTP")
                    except Exception as e:
                        log(f"  {b['key']}: loudness fix failed ({str(e)[-120:]})")
                    made[b["key"]] = str(out)
                    plans[b["key"]] = dict(
                        total=total, main_end=card_at if use_card else clip_total, card_at=card_at,
                        card=str(card) if use_card else None, heat_on=use_heat, heat=bool(heat_plan),
                        loud_hook=hv2, elements=sorted(elements), face_bottom=cam_h if cam else 0,
                        cap_times=[(w0, w1 + 0.6) for w0, w1, _ in words],
                        cap_box=(40, cap_y - int(int((b.get("caption") or {}).get("size", CAP_SIZE)) * 0.65),
                                 1040, cap_y + int(int((b.get("caption") or {}).get("size", CAP_SIZE)) * 0.65)),
                        look="launch" if v2 else "old", gold=use_gold, heat_n=heat_n if heat_plan else None, peak=peak,
                        hook_end=hook_t, bore=bore, bore_probs=bore_probs, game_crop=(f"{W}:{game_h}:0:{cam_h}" if cam else None),
                        stickers=[Path(p).stem for _, p, _ in plan_stickers], mix=mixinfo,
                        cuts=info.get("cuts"))
                    log(f"  {b['key']}: captions {len(words)} words, {n_chunks} chunks; "
                        f"banana: {', '.join(f'{n} @{t:.1f}s' for t, n in rx) or 'none'}"
                        + (f"; replay {replay[1]:.1f}-{replay[2]:.1f} s" if replay else "")
                        + (f"; look {'launch' if v2 else 'old'}" if looks != [False] else "")
                        + (f"; stickers {', '.join(f'{Path(p).stem} @{t:.1f}s' for t, p, _ in plan_stickers)}" if plan_stickers else "")
                        + (f"; heat {heat_n} @{heat_plan[0]:.1f}s" if heat_plan else "") + ("; GOLDEN 10" if use_gold else ""))
                if not problems:
                    break
                log(f"  quality check: {'; '.join(problems)} -> repair attempt {attempt + 1}")
        except NotGameplay:
            raise
        except Exception as e:
            for b in todo:
                (d / b["short"]).unlink(missing_ok=True)
            if v2 and False in looks:
                log(f"  launch look crashed ({str(e)[-160:]}) -> old look")
                continue
            if shorts:                                # another brand's version already passed: keep it
                log(f"  {', '.join(b['key'] for b in todo)}: render failed ({str(e)[-160:]}), dropped")
                break
            raise
        if problems:
            for b in todo:
                (d / b["short"]).unlink(missing_ok=True)
            if v2 and False in looks:
                log(f"  launch look failed the quality check ({'; '.join(problems)}) -> old look")
                continue
            if shorts:                                    # another brand's version already passed: keep it
                log(f"  {', '.join(b['key'] for b in todo)}: quality check failed ({'; '.join(problems)}), dropped")
                break
            raise RenderFailed(f"quality check failed after repairs: {'; '.join(problems)}")
        # QA of every finished short (brands.json 'qa'): a failing launch look -> the old look; a
        # failing old look -> not published (the file is deleted, the reason logged in meta 'qa')
        retry = []
        for b in todo:
            if b["key"] not in made:
                continue
            probs = []
            if b.get("qa", True):
                try:
                    probs = qa_short(made[b["key"]], plans[b["key"]])
                except Exception as e:                    # fail closed: an unchecked short is not published
                    probs = [f"qa probe failed ({str(e)[-120:]})"]
            look = plans[b["key"]]["look"]
            if not probs:
                shorts[b["key"]] = made[b["key"]]
                qa_log[b["key"]] = {"ok": True, "look": look, "gold": plans[b["key"]]["gold"],
                                    "heat": plans[b["key"]]["heat_n"], "stickers": plans[b["key"]]["stickers"],
                                    "peak": plans[b["key"]]["peak"], "cuts": plans[b["key"]].get("cuts"),
                                    "mix": plans[b["key"]].get("mix")}
                continue
            log(f"  QA {b['key']} ({look} look): {'; '.join(probs)}")
            if os.environ.get("KLIPY_DEBUG"):
                shutil.copy(made[b["key"]], Path(os.environ["KLIPY_DEBUG"]) / f"qa_failed_{b['key']}_{look}_{int(time.time())}.mp4")
            Path(made[b["key"]]).unlink(missing_ok=True)
            qa_log[b["key"]] = {"ok": False, "look": look, "problems": probs, "cuts": plans[b["key"]].get("cuts"),
                                "mix": plans[b["key"]].get("mix")}
            if any(p.startswith("not gameplay") for p in probs):
                not_game.append(f"{b['key']}: " + "; ".join(p for p in probs if p.startswith("not gameplay")))
            elif v2 and False in looks:              # (no game in the band: the old look would show the same)
                retry.append(b)
        todo = retry
    for tmp in list(d.glob("_sticker*")) + list(d.glob("_heat*")):   # sprite frames: only needed for the encode
        shutil.rmtree(tmp, ignore_errors=True)
    if not shorts and not_game:                  # the finished short shows no game: never again (other_game)
        raise NotGameplay("; ".join(not_game))
    if not shorts:
        raise RenderFailed(f"QA failed: {qa_log}")
    base = d / "base_9x16.mp4"               # clean, no captions: input for the editor (OpenMotion)
    if clean:
        encode(ff + ["-filter_complex", build_filter(*base_args, nf=src_norm(src)), "-map", "[v]", "-map", "0:a?"], base)
    out = Path(shorts.get("bananas") or next(iter(shorts.values())))
    try:
        if any(b.get("golden10") or b.get("heat") for b in blist):
            record_rating(clip)
    except Exception as e:
        log(f"  rating history: {str(e)[-80:]}")

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
        "peaks": {k: v.get("peak") for k, v in qa_log.items() if v.get("ok")},   # per brand (the hook differs)
        "card_at": round(card_at, 2),        # where the banana score card starts
        "rating": rating,
        "golden": bool(qa_log.get("bananas", {}).get("gold")),   # the rare golden 10/10 card is in the bananas short
        "heat": qa_log.get("fanpage", {}).get("heat"),           # fanpage HEAT 1-5 shown (None = none)
        "qa": qa_log,                        # QA per brand: look (launch / old), problems of a dropped look
        "bore": bore,                        # boredom gate numbers (payoff loudness/motion contrast, dead air)
        "caption": f"🎮 {home_url(clip)}\n#gta6 #{BRAND}",
        "rendered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "checklist": [
            "titulky sedí na řeč (jména, slang)",
            "bez hudby z rádia ve hře (Content ID)",
            "bez spoilerů: konec hry, velké cutscény",
        ],
    }
    try:                                     # long-form (docs/long-form.md): 16:9 source, payoff, tags
        tags, scores = moment_tags(words0, orig, hpc.get("peak"), clip.get("title"), clip.get("created") or 0)
        meta.update(source_16x9=str(orig), peak_src=hpc.get("peak"), cut_src=hpc.get("cut"), tags=tags, tag_scores=scores)
        cut = hpc.get("cut") or [None, None]
        row = pool_row(clip, source=str(orig), peak=hpc.get("peak"), cut=hpc.get("cut"), dur=hpc.get("dur"),
                       tags=tags, tag_scores=scores, quote=meta["quote"], rendered=list(shorts),
                       **classify_moment(words0, orig, hpc.get("peak"), clip, cut[0], cut[1]))
        pool_add(row, replace=True)
        if clip.get("platform") == "youtube" and cut[0] is not None:
            try:                             # the 24/7 PC: GitHub can't download YouTube -> the inbox carries it
                meta.update(long_segment(orig, d, row))
            except Exception as e:           # never costs the short
                log(f"  long-form source failed: {str(e)[-120:]}")
    except Exception as e:                   # tags are a bonus: never lose a finished short over them
        log(f"  tags failed: {str(e)[-120:]}")
    save_json(d / "meta.json", meta)
    log(f"  done -> {out} ({meta['layout']}, {enc})")
    return meta

# -----------------------------------------------------------------------------
# 6) modes

def mark(clip_id, ok, why=None):
    """Record a finished clip, or count a failure (re-read the file: another process may write too).
    why = the reason of a failure, kept in state 'failed_why' (the last one per clip)."""
    state = load_json(STATE, {"done": {}, "failed": {}})
    state.setdefault("done", {})
    state.setdefault("failed", {})
    if ok:
        state["done"][clip_id] = int(time.time())
        state["failed"].pop(clip_id, None)
        state.get("failed_why", {}).pop(clip_id, None)
    else:
        state["failed"][clip_id] = state["failed"].get(clip_id, 0) + 1
        if why:
            state.setdefault("failed_why", {})[clip_id] = str(why)[:300]
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

def need_back(c):
    """A render failed after assign_brands took a brand's need (--brand-need): give it back."""
    b = c.pop("_need", None)
    if b and b[0] in BRAND_NEED:
        BRAND_NEED[b[0]][b[1]] += 1

def process(clips, clean=False, limit=None, tried=None):
    """Render clips in order until `limit` succeed. `tried` (a set shared by several calls in one run)
    keeps a clip that failed once from being tried again in the same run."""
    n = 0
    for c in clips:
        if limit is not None and n >= limit:
            break
        if tried is not None:
            if c["id"] in tried:
                continue
            tried.add(c["id"])
        if skip(c["id"]) or moment_done(c) or not is_game(c):
            continue
        log(f"render {c['platform']}/{c['channel']} '{c['title'][:60]}' {c['views']} views")
        try:
            render(c, clean=clean)
            mark(c["id"], True)
            save_moment(c)                       # other viewers' clips of this moment: skip them later
            n += 1
        except RenderFailed as e:                # boring / QA / quality / cuts this time: a failed try (MAX_TRIES)
            need_back(c)
            mark(c["id"], False, e)
            log(f"  failed: {e}")
            continue
        except NotGameplay as e:                 # right category, wrong picture: never again
            need_back(c)
            if "no facecam" in str(e):           # detection can miss once: counts as a failure (max 3 tries)
                mark(c["id"], False, e)
                log(f"  skip: {e}")
                continue
            state = load_json(STATE, {"done": {}, "failed": {}})
            state.setdefault("other_game", {})[c["id"]] = str(e)
            save_json(STATE, state)
            log(f"  skip: {e}")
        except CorruptJson:                     # a broken state file: stop loudly, never write over it
            raise
        except Exception as e:  # one broken clip must not stop launch day
            need_back(c)
            mark(c["id"], False, e)
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

SAME_MOMENT_S = 30      # two clips this close (VOD offset, or channel + creation time) = one moment

def same_moment(c, o):
    """Two viewers' clips of one moment: same VOD < 30 s apart, or - when a clip has no VOD data
    (channel-list clips from yt-dlp) - same channel and created < 30 s apart."""
    if has_moment(c) and has_moment(o):
        return c["video_id"] == o["video_id"] and abs(c["vod_offset"] - o["vod_offset"]) < SAME_MOMENT_S
    return (by_created(c) and by_created(o) and c.get("channel") == o.get("channel")
            and abs(c["created"] - o["created"]) < SAME_MOMENT_S)

def by_created(c):
    """The creation time marks the moment only for a viewer's clip (Twitch/Kick/live): every window of
    one YouTube video carries the video's upload time, so those are told apart by their section."""
    return bool(c.get("created")) and not c.get("section") and c.get("platform") != "youtube"

def cluster(clips):
    """Several viewers clipping the same 30 s of the same stream = one moment, a stronger one."""
    groups = []
    for c in sorted(clips, key=lambda c: -(c.get("views") or 0)):
        g = next((g for g in groups if any(same_moment(c, o) for o in g)), None)
        if g:
            g.append(c)
            continue
        groups.append([c])
    for g in groups:
        g[0]["cluster"] = max(len(g), max((o.get("cluster") or 1) for o in g))
    return [g[0] for g in groups]

def has_moment(c):
    return bool(c.get("video_id")) and c.get("vod_offset") is not None

def moment_done(c):
    """The same stream moment already rendered from another viewer's clip: same VOD < 30 s apart, or
    same channel and created < 30 s apart (state 'moments' / 'moments_ch'). Two clips of one moment
    must never become two shorts (they would go to the two accounts)."""
    state = load_json(STATE, {})
    if has_moment(c):
        seen = state.get("moments", {}).get(c["video_id"], [])
        if any(abs(off - c["vod_offset"]) < SAME_MOMENT_S for _, off in seen):
            return True
    if by_created(c) and c.get("channel"):
        seen = state.get("moments_ch", {}).get(c["channel"], [])
        if any(abs(cr - c["created"]) < SAME_MOMENT_S and cid != c["id"] for _, cr, cid in seen):
            return True
    return False

def save_moment(c):
    state = load_json(STATE, {"done": {}, "failed": {}})
    now = int(time.time())
    if has_moment(c):
        m = {v: [x for x in xs if now - x[0] < 3 * 86400] for v, xs in state.get("moments", {}).items()}
        m.setdefault(c["video_id"], []).append([now, c["vod_offset"]])
        state["moments"] = {v: xs for v, xs in m.items() if xs}
    if by_created(c) and c.get("channel"):
        m = {ch: [x for x in xs if now - x[0] < 3 * 86400] for ch, xs in state.get("moments_ch", {}).items()}
        m.setdefault(c["channel"], []).append([now, int(c["created"]), c["id"]])
        state["moments_ch"] = {ch: xs for ch, xs in m.items() if xs}
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
    log(f"candidates: {len(live)} twitch/kick, {len(yt)} youtube -> rendering up to {a.max}"
        + (f" (catch-up {a.owed}, then >= {a.min_score:.0f} / {a.extra_score:.0f}; + up to {a.hot} hot >= {a.hot_score:.0f})"
           if a.min_score is not None else ""))
    # viewer-made clips first (a person picked the moment); YouTube windows only fill the rest
    for part in filter(None, a.brand_need.split(",")):      # bananas=OWED/OTHER/HOT
        name, _, v = part.partition("=")
        nums = [int(x or 0) for x in v.split("/")] + [0, 0, 0]
        BRAND_NEED[name.strip()] = nums[:3]
    if a.min_score is None:
        total = process(live, a.clean, limit=a.max)
    else:
        # just-in-time plan from `publikuj.py zbyva --plan` (same rules as `den`): a rendered clip that
        # is not posted in this run is lost, so render only what can go out now - `owed` catch-up
        # clips of any score, `good_n` that need good_score, the rest of `max` extra_score, and on top
        # up to `hot` clips spreading fast (score >= hot_score, < 3 h old) for the hot path
        tried = set()
        total = process(live, a.clean, limit=min(a.owed, a.max), tried=tried)
        total += process([c for c in live if virality(c) >= a.min_score], a.clean,
                         limit=min(a.good_n, a.max - total), tried=tried)
        total += process([c for c in live if virality(c) >= max(a.extra_score, a.min_score)], a.clean,
                         limit=a.max - total, tried=tried)
        if a.hot:
            now = time.time()
            cfg = load_json(SOURCES, {})
            stars = {x.lower() for x in cfg.get("star_streamers", [])}
            paced = a.hot_score > cfg.get("hot_score", 2000)   # publikuj zbyva: a hot post just went out

            def star(c):                               # Aleš 18. 11.: Kai Cenat / IShowSpeed go out at once
                return (not paced and c["channel"].lower() in stars
                        and virality(c) >= cfg.get("star_min_score", 300))
            hot = [c for c in live if (virality(c) >= a.hot_score or star(c)) and c.get("created")
                   and now - c["created"] <= cfg.get("hot_max_age_h", 3) * 3600]
            hot.sort(key=lambda c: (not star(c), -virality(c)))   # stars first
            total += process(hot, a.clean, limit=a.hot, tried=tried)
    total += process(yt, a.clean, limit=min(a.yt, a.max - total))
    min_pool = load_json(SOURCES, {}).get("long_pool_min_views", 300)
    for c in live:                                     # missed the shorts quota: long-form may still use it
        try:
            if c.get("views", 0) >= min_pool and not skip(c["id"]) and re.search(game_re(), c.get("category") or "", re.I):
                pool_add(pool_row(c))
        except Exception as e:                         # long-form pool must never break the shorts run
            log(f"pool: {e}")
    log(f"batch done: {total} new shorts")
    if total == 0 and a.max > 0 and os.environ.get("GITHUB_ACTIONS") and datetime.now().date() >= date(2026, 11, 19):
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
# 7) long-form pool (docs/long-form.md): every moment keeps its 16:9 source, payoff and tags,
#    so kompilace.py can build 8-15 min YouTube videos from it. Cheap on purpose:
#      - a rendered short adds its row for free (download, Whisper and payoff are already done)
#      - a candidate that missed today's quota adds a listing row (no download at all)
#      - `sber` / kompilace.py fill tags only for moments that may really be used:
#        download the clip (10-20 MB), Whisper, payoff, tags - no 9:16 render

POOL = Path(os.environ.get("KLIPY_POOL") or HERE / "long_pool.json")   # committed by denne.yml
POOL_DIR = OUT / "_pool"               # local cache of 16:9 sources (not committed)
POOL_DAYS = 8
POOL_MAX = 600
POOL_KEYS = ("id", "url", "channel", "name", "platform", "title", "views", "created", "category", "video_id",
             "vod_offset", "cluster", "section", "signal_score", "live")

# what a moment is about: transcript + the viewer's clip title (a person named it) + picture/sound
TAG_WORDS = {
    "funny": r"\b(ha(ha)+h?|lol|lmao|lmfao|bruh|bro what|i can'?t|i'?m crying|crying|dying|funny|hilarious)\b|💀|😂|🤣",
    "glitch": r"\b(bug|bugs|bugged|glitch\w*|broken|clipping|clipped|stuck|physics|t-?pose|floating|how did|what is happening|why is)\b",
    "chase": r"\b(cops?|police|chase|chasing|wanted|helicopter|heli|sirens?|getaway|escape|pursuit|go go|drive|run)\b",
    "crash": r"\b(crash\w*|wreck\w*|flipped|explod\w*|boom|ran (him )?over|run over|hit (me|him|her|us)|splat|launched|flew)\b",
    "rage": r"\b(fuck\w*|shit|bitch|asshole|stupid|hate|why would|are you serious|bro stop)\b",
    "first-reaction": r"\b(vice city|first time|finally|gta ?(6|vi|six)|graphics|so real|looks? (so )?(good|real|insane|crazy)|beautiful|day (one|1))\b",
    "wow": r"\b(wow|insane|crazy|no way|oh my god|omg|holy|what the|yo+|clutch|let'?s go)\b",
}
LAUNCH = date(2026, 11, 19)

def motion_stats(src):
    """Picture change (2 fps, tiny grey frames): (mean, 95th percentile) of the frame difference."""
    diffs, prev = [], None
    for _, img in frames(src, 2, 160, 90):
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
        if prev is not None:
            diffs.append(float(np.abs(g - prev).mean()))
        prev = g
    return (float(np.mean(diffs)), float(np.percentile(diffs, 95))) if diffs else (0.0, 0.0)

def moment_tags(words, src, peak=None, title="", created=0):
    """
    Tags for long-form series: funny, glitch, chase, crash, rage, first-reaction, wow.
    Words of the transcript and the clip title (counted double), emotion colours of the words,
    how loud the payoff is over the rest and how much the picture moves. Returns (tags, scores).
    """
    text = " ".join(t for _, _, t in words).lower()
    tl = (title or "").lower()
    n = max(len(words), 1)
    emo = word_emotions(words)
    win, drama = emo.count("win"), emo.count("drama")
    ts, rms = rms_curve(src)
    loud = float(np.percentile(rms, 95) / (np.median(rms) + 1e-9)) if rms.size else 1.0
    mot, mot_hi = motion_stats(src)
    sc = {}
    for tag, rx in TAG_WORDS.items():
        hits = len(re.findall(rx, text, re.I)) + 2 * len(re.findall(rx, tl, re.I))
        sc[tag] = min(1.0, hits / (2.0 + n / 40))
    sc["funny"] = min(1.0, sc["funny"] + 0.08 * win)
    sc["rage"] = min(1.0, sc["rage"] + 0.05 * drama + (0.25 if loud > 6 else 0))
    sc["wow"] = min(1.0, sc["wow"] + 0.04 * (win + drama))
    sc["chase"] = min(1.0, sc["chase"] + (0.2 if mot > 22 else 0.1 if mot > 14 else 0))
    sc["crash"] = min(1.0, sc["crash"] + (0.3 if mot_hi > 30 and loud > 4 else 0))
    if created and abs((datetime.fromtimestamp(created, tz=timezone.utc).date() - LAUNCH).days) <= 1:
        sc["first-reaction"] = min(1.0, sc["first-reaction"] + 0.2)
    sc = {k: round(v, 2) for k, v in sc.items()}
    tags = [k for k, v in sorted(sc.items(), key=lambda kv: -kv[1]) if v >= 0.3]
    return tags, {**sc, "_loud": round(loud, 1), "_motion": round(mot, 1)}

def pool_load():
    return load_json(POOL, {})

def pool_add(row, replace=False):
    """Add/update a moment in long_pool.json (compact, pruned to POOL_DAYS and POOL_MAX rows)."""
    try:
        pool = pool_load()
        old = pool.get(row["id"])
        if old and not replace:
            return                                        # a listing row never overwrites what we know
        pool[row["id"]] = {**(old or {}), **{k: v for k, v in row.items() if v is not None}}
        now = time.time()
        pool = {k: v for k, v in pool.items() if now - (v.get("created") or v.get("added") or now) < POOL_DAYS * 86400}
        if len(pool) > POOL_MAX:                          # keep the tagged ones and the most viewed
            keep = sorted(pool.values(), key=lambda v: (v.get("tags") is not None, v.get("views") or 0), reverse=True)
            pool = {v["id"]: v for v in keep[:POOL_MAX]}
        tmp = POOL.with_name(f"{POOL.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(dict(sorted(pool.items())), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(POOL)
    except Exception as e:                                # the pool must never break a short
        log(f"  pool: {str(e)[-120:]}")

def pool_row(clip, **kw):
    row = {k: clip.get(k) for k in POOL_KEYS}
    row["added"] = int(time.time())
    row.update(kw)
    return row

LONG_SEG = "long_16x9.mp4"             # YouTube moments from the 24/7 PC: 720p cut of the source for the inbox
LONG_PAD = 2.0                         # s kept on both sides of the HPC cut

def long_segment(src, d, row):
    """
    YouTube moment rendered on the worker PC: GitHub (kompilace) cannot download from YouTube, so
    a 16:9 720p cut of the source (HPC cut +- LONG_PAD s, ~2.5 Mb/s, AAC 96k) goes into the inbox
    with the pool row (cut/peak/dur moved into the cut file's timeline). publikuj.py appends the
    row to long_pool.json with source = the file's Netlify URL when GitHub reads the inbox.
    Returns the meta fields: long_src (file in the clip folder), long_mb, pool_row.
    """
    a, b = row["cut"]
    dur = row.get("dur") or duration(src)
    s, e = max(0.0, a - LONG_PAD), min(dur, b + LONG_PAD)
    out = d / LONG_SEG
    base = ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{s:.3f}", "-to", f"{e:.3f}",
            "-i", str(src), "-vf", f"{src_norm(src)},scale=1280:720", "-map", "0:v:0", "-map", "0:a?"]
    rate = ["-b:v", "2500k", "-maxrate", "2800k", "-bufsize", "5000k"]
    for venc in (["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr"] + rate, ["-c:v", "libx264", "-preset", "veryfast"] + rate):
        try:
            run(base + venc + ["-c:a", "aac", "-b:a", "96k", "-ac", "2", "-movflags", "+faststart", str(out)], timeout=600)
            break
        except RuntimeError as err:
            log(f"  long source: encoder {venc[1]} failed ({str(err)[-120:]})")
    else:
        raise RuntimeError("no encoder for the long-form source")
    mb = round(out.stat().st_size / 1e6, 1)
    pr = {k: v for k, v in row.items() if k not in ("source", "section")}
    pr.update(cut=[round(a - s, 2), round(b - s, 2)], dur=round(e - s, 2),
              peak=None if row.get("peak") is None else round(row["peak"] - s, 2), seg_from=round(s, 2), src_mb=mb)
    log(f"  long-form source for the inbox: {e - s:.0f} s, {mb} MB")
    return {"long_src": LONG_SEG, "long_mb": mb, "pool_row": pr}

def collect(clip, force=False):
    """
    Long-form moment without a short: 16:9 source -> transcript, payoff, HPC cut, tags.
    Writes out/_pool/<id>/{source.mp4, meta.json} and the pool row. Returns the row.
    """
    d = POOL_DIR / clip["id"]
    d.mkdir(parents=True, exist_ok=True)
    mf = d / "meta.json"
    if mf.exists() and not force:
        row = load_json(mf, {})
        if row.get("tags") is not None and Path(row.get("source", "")).exists():
            return row
    src = d / "source.mp4"
    if not src.exists():
        log(f"  download {clip['url']}")
        download(clip, src)
    if talking_head(src):
        raise NotGameplay("streamer's face fills the frame, no gameplay")
    words = transcribe(src)
    if SLUR_RE.search(" ".join(t for _, _, t in words)):
        raise NotGameplay("slur in the speech")
    dur = duration(src)
    peak = audio_peak(src)
    a, b = trim_points(words, peak, dur, hi=40.0)
    tags, scores = moment_tags(words, src, peak, clip.get("title"), clip.get("created") or 0)
    row = pool_row(clip, source=str(src), peak=peak, cut=[round(a, 2), round(b, 2)], dur=round(dur, 2),
                   tags=tags, tag_scores=scores, quote=pick_quote(words, peak))
    row.update(classify_moment(words, src, peak, clip, a, b))
    save_json(mf, {**row, "words": words})
    pool_add(row, replace=True)
    log(f"  pool: {clip['channel']} {clip['id']} tags {tags} peak {peak}")
    return row

def classify_moment(words, src, peak, clip, a=None, b=None):
    """Categories + evidence + entertainment score (kategorie.py) for long-form series; {} on error."""
    try:
        import kategorie
        return kategorie.classify(words, src, peak, clip.get("title"), clip.get("created") or 0,
                                  clip.get("views") or 0, a, b)
    except Exception as e:
        log(f"  categories failed: {str(e)[-120:]}")
        return {}

def game_clips(name, t0, t1, pages=3):
    """Clips of a Twitch category made between t0 and t1 (unix), English, best first (Helix)."""
    games = helix("games", {"name": name}).get("data") or []
    if not games:
        log(f"twitch api: category '{name}' not found")
        return []
    fmt = lambda t: datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out, cursor = [], None
    for _ in range(pages):
        params = {"game_id": games[0]["id"], "started_at": fmt(t0), "ended_at": fmt(t1), "first": 100}
        if cursor:
            params["after"] = cursor
        res = helix("clips", params)
        data = [c for c in res.get("data") or [] if (c.get("language") or "en").split("-")[0] == "en"]
        ids = sorted({c["broadcaster_id"] for c in data})
        users = {u["id"]: u["login"] for u in helix("users", [("id", i) for i in ids[:100]]).get("data") or []} if ids else {}
        for c in data:
            out.append({
                "id": f"twitch_{c['id']}", "url": c["url"], "title": (c.get("title") or "").strip(),
                "views": int(c.get("view_count") or 0), "duration": float(c.get("duration") or 0),
                "created": int(datetime.fromisoformat(c["created_at"].replace("Z", "+00:00")).timestamp()),
                "channel": users.get(c["broadcaster_id"], (c.get("broadcaster_name") or "twitch").lower()),
                "name": c.get("broadcaster_name") or None, "platform": "twitch", "category": games[0]["name"],
                "video_id": c.get("video_id") or None, "vod_offset": c.get("vod_offset"),
            })
        cursor = (res.get("pagination") or {}).get("cursor")
        if not cursor:
            break
    return out

def cmd_sber(a):
    """Collect long-form moments (no shorts): given URLs, or the best clips of a Twitch category."""
    blocked = {b.lower() for b in load_json(SOURCES, {}).get("blocklist", [])}
    if a.urls:
        clips = [clip_info(u) for u in a.urls]
    else:
        if not all(helix_keys()):
            sys.exit("sber needs TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET (Twitch API)")
        name = a.hra or load_json(SOURCES, {}).get("twitch_game", "Grand Theft Auto VI")
        clips = cluster(game_clips(name, time.time() - a.days * 86400, time.time(), pages=a.pages))
        clips = [c for c in clips if c["views"] >= a.min_views and c["channel"] not in blocked]
        clips.sort(key=virality, reverse=True)
    n = 0
    for c in clips:
        if n >= a.max:
            break
        if (pool_load().get(c["id"]) or {}).get("tags") is not None and (POOL_DIR / c["id"] / "source.mp4").exists():
            continue
        try:
            collect(c)
            n += 1
        except NotGameplay as e:
            log(f"  skip {c['id']}: {e}")
        except Exception as e:
            log(f"  FAILED {c['url']}: {str(e)[-200:]}")
    log(f"sber done: {n} moments tagged (pool {POOL})")

# -----------------------------------------------------------------------------
# live: chat explosion on a running GTA VI stream -> the last ~40 s from its live VOD -> short

LAUGH_IRC = re.compile(r"KEKW|OMEGALUL|LUL|LMAO|ICANT|💀|😂|\bW\b|\?\?\?|HOLY|NO WAY|WTF", re.I)

class ChatMeter:
    """
    Anonymous, read-only Twitch chat (irc.chat.twitch.tv, nick justinfan*, no account): messages
    per 5 s for every joined channel, laughs / hype emotes count double.
    """
    def __init__(self):
        import threading
        self.counts, self.raw, self.joined, self.lock = {}, {}, set(), threading.Lock()
        self._connect()
        threading.Thread(target=self._read, daemon=True).start()

    def _connect(self):
        import socket
        self.sock = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=10)
        self.sock.send(f"NICK justinfan{random.randint(10000, 99999)}\r\n".encode())
        for lg in self.joined:                        # after a reconnect: the same chats again
            self.sock.send(f"JOIN #{lg}\r\n".encode())

    def _reconnect(self, why):
        """Twitch drops IRC connections (restarts, RECONNECT): never go deaf for the rest of the 5.7 h job."""
        log(f"live: chat connection lost ({why}), reconnecting")
        try:
            self.sock.close()
        except Exception:
            pass
        for wait in (2, 5, 10, 30, 60, 60, 60):
            time.sleep(wait)
            try:
                self._connect()
                return True
            except Exception as e:
                log(f"live: chat reconnect failed ({str(e)[-80:]})")
        return False

    def join(self, logins):
        try:
            for lg in set(logins) - self.joined:
                self.sock.send(f"JOIN #{lg}\r\n".encode())
            for lg in self.joined - set(logins):
                self.sock.send(f"PART #{lg}\r\n".encode())
        except OSError as e:                          # the reader reconnects and joins self.joined again
            log(f"live: chat join failed ({str(e)[-80:]})")
        self.joined = set(logins)

    def _read(self):
        import socket
        buf = b""
        while True:
            try:
                data, why = self.sock.recv(65536), "closed by Twitch"
            except socket.timeout:
                continue                              # a quiet 10 s: the connection is fine
            except OSError as e:                      # reset / broken: was a silent busy loop before
                data, why = b"", e
            if not data:
                if not self._reconnect(why):
                    log("live: chat gone for good, no more spikes this run")
                    return
                buf = b""
                continue
            buf += data
            *lines, buf = buf.split(b"\r\n")
            for ln in lines:
                if ln.startswith(b"PING"):
                    try:
                        self.sock.send(b"PONG :tmi.twitch.tv\r\n")
                    except OSError:
                        pass                          # the next recv notices it and reconnects
                    continue
                if b" RECONNECT" in ln[:40]:          # Twitch asks for a reconnect (server restart)
                    self._reconnect("RECONNECT")
                    buf = b""
                    break
                if b" PRIVMSG #" not in ln:
                    continue
                head, _, text = ln.decode("utf-8", "replace").partition(" :")
                chan = head.rsplit("#", 1)[-1].strip()
                b5 = int(time.time() // 5)
                w = 2 if LAUGH_IRC.search(text.split(" :", 1)[-1]) else 1
                with self.lock:
                    c = self.counts.setdefault(chan, {})
                    c[b5] = c.get(b5, 0) + w
                    r = self.raw.setdefault(chan, {})       # plain message count (the "chat went N×" line)
                    r[b5] = r.get(b5, 0) + 1

    def spike(self, chan, z_min, cool):
        """
        (z, msgs, x) when a finished 5 s bin since the last check explodes over the channel's 5 minutes
        before it (all bins since the last check: a slow loop turn never misses an explosion; the
        strongest one wins); x = plain messages in that bin / their 5-minute median (for the post text).
        """
        now = int(time.time() // 5) - 1
        if not hasattr(self, "checked"):
            self.checked = {}
        first = max(self.checked.get(chan, now - 1) + 1, now - 24)    # at most the last 2 minutes
        self.checked[chan] = now
        with self.lock:
            c = self.counts.get(chan, {})
            r = self.raw.get(chan, {})
            for old in [k for k in c if k < now - 110]:
                del c[old]
            for old in [k for k in r if k < now - 110]:
                del r[old]
            bins = [(c.get(b, 0), [c.get(i, 0) for i in range(b - 60, b)],
                     r.get(b, 0), [r.get(i, 0) for i in range(b - 60, b)]) for b in range(first, now + 1)]
        best = None
        for cur, hist, rcur, rhist in bins:
            if sum(1 for x in hist if x) < 20:        # not enough chat history yet
                continue
            med = float(np.median(hist))
            mad = float(np.median([abs(x - med) for x in hist])) + 1
            z = (cur - med) / mad
            if z >= z_min and cur >= max(8, 3 * med) and (best is None or z > best[0]):
                rmed = float(np.median(rhist))
                best = (round(z, 1), cur, round(rcur / rmed) if rmed >= 1 else None)
        return best

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
    import queue as Q
    import threading
    meter, streams, cooldown = ChatMeter(), {}, {}
    jobs = Q.Queue()                                  # spikes waiting for the worker (one render at a time)
    end, next_poll = time.time() + a.hours * 3600, 0.0
    made = {"n": 0, "pending": False, "last_hand": 0.0}
    gap = cfg.get("live_handover_min", 10) * 60       # Netlify: <= 3 deploys/min, ~100/day for everything

    def hand_over(force=False):
        """Batch the shorts made since the last hand-over into one live-inbox deploy (at most every `gap`)."""
        if made["pending"] and (force or time.time() - made["last_hand"] >= gap):
            made["pending"], made["last_hand"] = False, time.time()
            try:
                hand_over_live()
            except Exception as e:
                log(f"live: hand-over failed ({str(e)[-120:]})")
                made["pending"] = True

    def worker():
        """Render + hand-over off the main loop, so the chat is watched (and spikes found) all the time."""
        while True:
            try:
                job = jobs.get(timeout=20)
            except Q.Empty:
                hand_over()
                continue
            if job is None:
                break
            t_spike, lg, st, (z, msgs, chat_x) = job
            time.sleep(max(0.0, t_spike + 70 - time.time()))   # the live VOD lags about a minute
            try:
                sec = vod_section(lg, st["user_id"], st["started_at"], t_spike)
                if not sec:
                    log(f"live: {lg} has no live VOD, skipped")
                    continue
                url, s0, s1 = sec
                clip = {"id": f"live_{lg}_{int(t_spike)}", "url": url, "title": st.get("title") or "",
                        # views = 0: the stream's audience is not a clip's views (it inflated the score,
                        # banana rating and the league); the live signal is spike_z (hot path, live_rating)
                        "views": 0, "viewers": int(st["viewer_count"]), "duration": s1 - s0, "created": int(t_spike),
                        "channel": lg, "platform": "twitch", "category": games[0]["name"],
                        "live": True, "spike_z": z, "cluster": 2,     # a chat explosion = a strong moment
                        "chat_x": chat_x}                             # messages vs the 5 min median (post text)
                d = OUT / lg / f"{datetime.fromtimestamp(clip['created'], tz=timezone.utc):%Y%m%d}_{clip['id']}"   # = render()'s folder
                d.mkdir(parents=True, exist_ok=True)
                src = d / "source.mp4"
                run(["yt-dlp", "-q", "-f", "best[height<=1080]/best", "--download-sections", f"*{s0:.0f}-{s1:.0f}",
                     "--force-overwrites", "-o", str(src), url], timeout=300)
                render(clip, src=src)
                mark(clip["id"], True)
                made["n"] += 1
                made["pending"] = True
                log(f"live: short #{made['n']} from {lg}")
            except NotGameplay as e:
                log(f"live: {lg} skipped: {e}")
            except Exception as e:
                log(f"live: {lg} failed: {str(e)[-200:]}")
            finally:
                hand_over()

    th = threading.Thread(target=worker, daemon=True)
    th.start()
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
            try:
                hit = meter.spike(lg, a.z, a.cool)
            except Exception as e:
                log(f"live: spike check {lg} failed ({str(e)[-80:]})")
                continue
            if hit:
                cooldown[lg] = time.time() + a.cool
                if jobs.qsize() >= 6:                 # the worker is far behind: old spikes would be stale anyway
                    log(f"live: SPIKE {lg} z={hit[0]} dropped (render queue full)")
                    continue
                jobs.put((time.time() - 2.5, lg, streams[lg], hit))
                log(f"live: SPIKE {lg} z={hit[0]} msgs={hit[1]} -> cut in ~70 s (queue {jobs.qsize()})")
        time.sleep(5)
    jobs.put(None)                                    # finish what is queued (the job's timeout leaves room)
    th.join(timeout=max(60, getattr(a, "drain_min", 15) * 60))
    hand_over(force=True)
    log(f"live: done, {made['n']} shorts")

def hand_over_live():
    """Put the live shorts on the live-inbox and start the GitHub publish run right away."""
    r = subprocess.run([sys.executable, str(HERE / "publikuj.py"), "schranka", "--nazev", "live-inbox", "--prefix", "live_",
                        "--max", "40"], timeout=900)
    if r.returncode:                                  # the live-inbox could not be updated: no publish run
        log("live: live-inbox not updated, publish run not started")
        return
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
    b.add_argument("--owed", type=int, default=0, help="JIT plan: of --max this many catch-up clips of any score")
    b.add_argument("--good-n", type=int, default=0, help="JIT plan: then this many with virality >= --min-score")
    b.add_argument("--min-score", type=float, default=None, help="JIT plan (publikuj.py zbyva --plan): good_score")
    b.add_argument("--extra-score", type=float, default=0, help="JIT plan: the rest of --max needs this")
    b.add_argument("--hot", type=int, default=0, help="JIT plan: up to this many extra hot clips")
    b.add_argument("--hot-score", type=float, default=2000, help="JIT plan: hot = virality >= this, < 3 h old")
    b.add_argument("--brand-need", default="",
                   help="JIT plan: bananas=OWED/OTHER/HOT,fanpage=... (catch-up, other slots, hot room: which brand gets a clip)")
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

    sb = sub.add_parser("sber", help="long-form: collect + tag moments (16:9 source, payoff, tags), no shorts")
    sb.add_argument("urls", nargs="*", help="clip URLs; default: best clips of the Twitch category")
    sb.add_argument("--hra", help="Twitch category (default sources.json twitch_game; test: 'Grand Theft Auto V')")
    sb.add_argument("--days", type=float, default=1)
    sb.add_argument("--max", type=int, default=40, help="tag at most this many new moments")
    sb.add_argument("--pages", type=int, default=3)
    sb.add_argument("--min-views", type=int, default=300)
    sb.set_defaults(fn=cmd_sber)

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
