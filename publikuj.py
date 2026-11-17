"""
publikuj.py - gta6bananaclips: rendered shorts -> public URL (Netlify) -> Buffer
-> Instagram, TikTok, X, Facebook, YouTube.

Buffer's API has no upload: it takes a PUBLIC video URL and fetches the file at
publish time. So the day's shorts are deployed to a Netlify site
(gta6bananaclips-media.netlify.app/<day>/<clip>.mp4) and must stay there until
they are published. Every deploy replaces the whole site, so videos still waiting
for their slot are carried over into the next deploy; published ones drop out.

Daily flow:
  .venv/Scripts/python klipuj.py batch                  # render fresh moments
  .venv/Scripts/python publikuj.py den --den dnes --n 10   # best 10 -> today's slots in Buffer

By default posts land in Buffer as DRAFTS (nothing goes public). --ostre schedules
them for real. --zkouska prints the plan and the GraphQL without touching anything.

YouTube blocks GitHub's servers, so YouTube moments are rendered on a PC with a home
connection (worker/, every 30 min): `publikuj.py schranka` puts them on a Netlify DRAFT
deploy titled yt-inbox (a deploy preview: 0 credits, production untouched). `den` reads
the newest yt-inbox and treats its shorts like local ones (downloaded only when picked).

One-time setup:
  1. .env (see .env.example): BUFFER_API_KEY_1/_2, NETLIFY_AUTH_TOKEN, NETLIFY_SITE_ID, MEDIA_BASE_URL
  2. .venv/Scripts/python publikuj.py kanaly    # saves your Buffer channels to buffer.json

Second account (brands.json, docs/dva-ucty.md): every command takes --znacka bananas|fanpage
(default bananas). `kanaly --znacka fanpage` uses BUFFER_API_KEY_3/_4 -> buffer_fanpage.json,
`den --znacka fanpage` posts the fanpage versions (short_fanpage.mp4) -> publikovano_fanpage.json.
"""

import argparse
import hashlib
import io
import shutil
import subprocess
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
PUBLISHED = HERE / "publikovano.json"   # clip id -> url, path, time, Buffer post ids
CHANNELS = HERE / "buffer.json"         # Buffer channels we post to (from `kanaly`)
API = "https://api.buffer.com"
NETLIFY = "https://api.netlify.com/api/v1"
TZ = ZoneInfo("Europe/Prague")
# Prague times for a UK + US audience; slots before noon belong to the NEXT calendar day.
#   Prague  UK (GMT)  US East  US West
#   14:00   13:00     08:00    05:00    UK lunch
#   17:00   16:00     11:00    08:00    UK after school
#   18:30   17:30     12:30    09:30    US lunch, UK evening
#   20:00   19:00     14:00    11:00    UK prime time
#   21:30   20:30     15:30    12:30    UK prime, US after school
#   23:00   22:00     17:00    14:00    UK late, US east coast after work
#   00:30   23:30     18:30    15:30    US east prime time
#   02:00   01:00     20:00    17:00    US east prime, west after work
#   03:30   02:30     21:30    18:30    US east late, west prime
#   05:00   04:00     23:00    20:00    US west prime time
# (CET; during summer time everything shifts together, the pattern stays.)
SLOTS = ["14:00", "17:00", "18:30", "20:00", "21:30", "23:00", "00:30", "02:00", "03:30", "05:00"]
# which slots a small quota fills first: spread over UK evening + US prime time (Fable 15. 11.:
# the first 3 in time order were all US morning)
PRIORITY = ["18:30", "23:00", "02:00", "20:00", "00:30", "21:30", "14:00", "03:30", "17:00", "05:00"]

# Two accounts (brands.json, docs/dva-ucty.md): `bananas` = gta6bananaclips (the original, its
# behaviour and files stay as they were), `fanpage` = gta6fanpageclip. Every command takes
# --znacka; set_brand() points PUBLISHED / CHANNELS / the Buffer keys at that brand's files.
BRANDS = HERE / "brands.json"
ZNACKA = "bananas"
BRAND = {}                              # the brand's entry in brands.json ({} = no brands.json: as before)

def brands_cfg():
    return load_json(BRANDS, {})

def set_brand(znacka):
    global ZNACKA, BRAND, PUBLISHED, CHANNELS
    cfg = brands_cfg()
    if znacka != "bananas" and znacka not in cfg:
        sys.exit(f"unknown brand '{znacka}' (brands.json)")
    ZNACKA, BRAND = znacka, cfg.get(znacka) or {}
    PUBLISHED = HERE / BRAND.get("published", "publikovano.json")
    CHANNELS = HERE / BRAND.get("channels", "buffer.json")

def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def save_json(path, data):
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)

def load_env():
    """KEY=VALUE lines from .env into the environment (real env vars win)."""
    env = HERE / ".env"
    for line in env.read_text(encoding="utf-8").splitlines() if env.exists() else []:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if v.strip():
                os.environ.setdefault(k.strip(), v.strip().strip('"'))

def need(*keys):
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        sys.exit(f"missing in .env: {', '.join(missing)} (see .env.example)")

def http(url, data=None, headers=None, method=None, timeout=120):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()

# -----------------------------------------------------------------------------
# Buffer GraphQL. Inputs are written inline: strings quoted, enums bare.

class Enum(str):
    """A GraphQL enum value (written without quotes)."""

def gql_value(v):
    if isinstance(v, Enum):
        return str(v)
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, (int, float)):
        return json.dumps(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(gql_value(x) for x in v) + "]"
    return "{" + ", ".join(f"{k}: {gql_value(x)}" for k, x in v.items()) + "}"

def buffer_keys():
    """Names of the Buffer key variables we have: BUFFER_API_KEY_1, _2 ... (or a single BUFFER_API_KEY)."""
    keys = sorted(k for k in os.environ if k.startswith("BUFFER_API_KEY_") and os.environ[k])
    return keys or (["BUFFER_API_KEY"] if os.environ.get("BUFFER_API_KEY") else [])

def brand_keys():
    """The brand's Buffer keys that are set (brands.json buffer_keys); without brands.json all of them."""
    if BRAND.get("buffer_keys"):
        keys = [k for k in BRAND["buffer_keys"] if os.environ.get(k)]
        if not keys and ZNACKA == "bananas" and os.environ.get("BUFFER_API_KEY"):
            keys = ["BUFFER_API_KEY"]                 # old single-key setup
        return keys
    return buffer_keys()

def gql(query, key="BUFFER_API_KEY"):
    body = json.dumps({"query": query}).encode()
    headers = {"Authorization": f"Bearer {os.environ[key]}", "Content-Type": "application/json"}
    for attempt in range(3):
        try:
            data = json.loads(http(API, body, headers, "POST", 60)[1])
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:      # rate limit: wait as told
                wait = int(e.headers.get("Retry-After") or 60) if (e.headers.get("Retry-After") or "60").isdigit() else 60
                log(f"  Buffer rate limit, waiting {wait}s")
                time.sleep(wait)
                continue
            raise RuntimeError(f"Buffer HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
    if data.get("errors"):
        raise RuntimeError(f"Buffer: {data['errors'][0].get('message')}")
    return data["data"]

# -----------------------------------------------------------------------------
# public hosting: one Netlify deploy per day (zip of all videos still needed)

INDEX = b"<!doctype html><meta name=robots content=noindex><title>gta6bananaclips media</title>"

def netlify_deploy(files):
    """files: {site path: local path}. Deploys them as the whole site and waits until it is live."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:   # mp4 doesn't compress anyway
        z.writestr("index.html", INDEX)
        z.writestr("robots.txt", "User-agent: *\nDisallow: /\n")
        for rel, src in files.items():
            z.write(src, rel)
    auth = {"Authorization": f"Bearer {os.environ['NETLIFY_AUTH_TOKEN']}"}
    site = os.environ["NETLIFY_SITE_ID"]
    log(f"Netlify: deploying {len(files)} videos ({buf.tell() / 1e6:.0f} MB)")
    _, body = http(f"{NETLIFY}/sites/{site}/deploys", buf.getvalue(),
                   {**auth, "Content-Type": "application/zip"}, "POST", 900)
    deploy = json.loads(body)
    for _ in range(120):                                     # up to 10 min
        if deploy.get("state") == "ready":
            return
        if deploy.get("state") == "error":
            raise RuntimeError(f"Netlify deploy failed: {deploy.get('error_message')}")
        time.sleep(5)
        deploy = json.loads(http(f"{NETLIFY}/deploys/{deploy['id']}", headers=auth)[1])
    raise RuntimeError("Netlify deploy did not become ready in 10 min")

def media_url(rel):
    return f"{os.environ['MEDIA_BASE_URL'].rstrip('/')}/{rel}"

def is_public(url):
    try:
        return http(url, method="HEAD", timeout=30)[0] == 200
    except urllib.error.URLError:
        return False

INBOX_TITLE = "yt-inbox"
MEDIA_TITLE = "media"          # draft deploys holding the published videos (permalinks)

def prune_drafts(days=4):
    """Delete our own draft deploys (media / inboxes) older than `days` - Buffer has long fetched them."""
    try:
        site = os.environ["NETLIFY_SITE_ID"]
        for d in netlify(f"/sites/{site}/deploys?per_page=100"):
            if d.get("title") not in (MEDIA_TITLE, INBOX_TITLE, "live-inbox") or d.get("context") == "production":
                continue
            age = datetime.now(timezone.utc) - datetime.fromisoformat(d["created_at"].replace("Z", "+00:00"))
            if age > timedelta(days=days):
                netlify(f"/deploys/{d['id']}", method="DELETE")
    except Exception as e:
        log(f"  prune drafts: {e}")

def safe_name(clip_id):
    return re.sub(r"[^A-Za-z0-9_-]", "_", clip_id)

def netlify(path, data=None, method=None, ctype="application/json", timeout=120):
    h = {"Authorization": f"Bearer {os.environ['NETLIFY_AUTH_TOKEN']}", "Content-Type": ctype}
    return json.loads(http(f"{NETLIFY}{path}", data, h, method, timeout)[1] or b"null")

def inbox_deploy(files, inbox, title=None):
    """
    files: {site path: local path}, inbox: bytes of inbox.json. A draft deploy (deploy preview,
    no credits) by file digest: Netlify asks only for files it doesn't have yet.
    """
    site = os.environ["NETLIFY_SITE_ID"]
    blobs = {"/inbox.json": inbox, "/robots.txt": b"User-agent: *\nDisallow: /\n"}
    digests = {k: hashlib.sha1(v).hexdigest() for k, v in blobs.items()}
    for rel, src in files.items():
        h = hashlib.sha1()
        with open(src, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        digests["/" + rel] = h.hexdigest()
    d = netlify(f"/sites/{site}/deploys", json.dumps({"files": digests, "draft": True, "title": title or INBOX_TITLE}).encode(), "POST")
    for _ in range(60):                                       # Netlify works out what it is missing
        if d["state"] not in ("new", "preparing"):
            break
        time.sleep(2)
        d = netlify(f"/deploys/{d['id']}")
    need_sha = set(d.get("required") or [])
    log(f"inbox: {len(files)} shorts, uploading {len(need_sha)} new files")
    for path, sha in digests.items():
        if sha not in need_sha:
            continue
        need_sha.discard(sha)                                 # same content twice = upload once
        body = blobs[path] if path in blobs else Path(files[path[1:]]).read_bytes()
        netlify(f"/deploys/{d['id']}/files{urllib.parse.quote(path)}", body, "PUT", "application/octet-stream", 900)
    for _ in range(120):
        d = netlify(f"/deploys/{d['id']}")
        if d["state"] == "ready":
            return d["links"]["permalink"]
        if d["state"] == "error":
            raise RuntimeError(f"Netlify inbox deploy failed: {d.get('error_message')}")
        time.sleep(5)
    raise RuntimeError("Netlify inbox deploy did not become ready in 10 min")

def inbox_items():
    """Shorts from the newest yt-inbox (YouTube, worker PC) and live-inbox (live chat spikes) draft deploys."""
    if not (os.environ.get("NETLIFY_AUTH_TOKEN") and os.environ.get("NETLIFY_SITE_ID")):
        return []
    out = []
    try:
        deploys = netlify(f"/sites/{os.environ['NETLIFY_SITE_ID']}/deploys?per_page=50")
    except Exception as e:                                    # no inbox must never stop Twitch/Kick posts
        log(f"inbox: unreadable ({e})")
        return []
    for title in (INBOX_TITLE, "live-inbox"):
        d = next((d for d in deploys if d.get("title") == title and d.get("state") == "ready"), None)
        if not d:
            continue
        try:
            base = d["links"]["permalink"]
            items = json.loads(http(f"{base}/inbox.json", timeout=60)[1])
        except Exception as e:
            log(f"inbox {title}: unreadable ({e})")
            continue
        for m in items:                               # `file` = bananas, `file_fanpage` = fanpage
            m["base"], m["remote"] = base, True
        log(f"inbox {title}: {len(items)} shorts ({d.get('created_at', '')[:16]})")
        out += items
    return out

# -----------------------------------------------------------------------------
# what to post

def brand_short(m, meta_file, znacka):
    """Local path of the brand's version of a rendered clip, or None (meta without `shorts` = bananas only)."""
    if "shorts" in m:
        v = (m.get("shorts") or {}).get(znacka)
    else:
        v = m.get("short") if znacka == "bananas" else None
    if not v:
        return None
    p = Path(v)
    if not p.is_absolute():
        p = HERE / p if (HERE / p).exists() else meta_file.parent / p
    return str(p)

def inbox_short(m, znacka):
    """URL of the brand's version of an inbox short (`file` = bananas, `file_fanpage` = fanpage), or None."""
    f = m.get("file" if znacka == "bananas" else f"file_{znacka}")
    return f"{m['base']}/{f}" if f else None

def candidates(znacka=None):
    """
    Rendered shorts of this brand not yet published by it, best first: virality = views per hour
    of age (softened). Gate 2 of 2: only shorts whose saved stream category / video title matches
    the game in sources.json (GTA 6) can go out - whatever slipped past the render step.
    """
    znacka = znacka or ZNACKA
    game = load_json(HERE / "sources.json", {}).get("game", "Grand Theft Auto VI|GTA ?VI|GTA ?6")
    done = load_json(PUBLISHED, {})
    now = time.time()
    out, local = [], []
    for meta_file in OUT.glob("*/*/meta.json"):
        if meta_file.parts[-3].startswith("_"):
            continue                                 # _ukazka, _media
        m = load_json(meta_file, {})
        if m:
            m["short"] = brand_short(m, meta_file, znacka)
            local.append(m)
    seen = {m.get("id") for m in local}
    load_env()
    for m in [m for m in inbox_items() if m.get("id") not in seen]:
        m["short"] = inbox_short(m, znacka)
        local.append(m)
    for m in local:
        if not m or not m.get("short") or m.get("id") in done or not (m.get("remote") or Path(m["short"]).exists()):
            continue
        if (m.get("platform") == "youtube" and date.today() >= date.fromisoformat(
                (load_json(HERE / "sources.json", {}).get("youtube_prelaunch") or {}).get("until", "2026-11-19"))
                and re.search(r"trailer|extended look|showcase", m.get("title") or "", re.I)):
            continue                                 # from the launch on: gameplay, no trailer reactions
        if not m.get("cam") and m.get("layout") != "game":   # no facecam only for viral-potential clips (render decides)
            continue
        if not re.search(game, m.get("category") or "", re.I):
            log(f"  not published (category '{m.get('category') or '?'}' is not {game}): {m.get('id')}")
            continue
        age_h = (now - m["created"]) / 3600 if m.get("created") else 24      # unknown age = not fresh
        m["age_h"] = age_h
        m["score"] = ((m.get("views") or 0) / (max(age_h, 0) + 1) ** 0.5   # same "virality" as klipuj.batch
                      * (1 + min(m.get("growth") or 0, 3)) * (1 + 0.5 * ((m.get("cluster") or 1) - 1)))
        out.append(m)
    # viewer-made Twitch/Kick clips first: a YouTube window's "views" are the whole video's,
    # not comparable, so YouTube only fills what is left (and at most youtube_per_day)
    return sorted(out, key=lambda m: (m.get("platform") == "youtube", -m["score"]))

# Anti-spam: every post gets its own text (a quote from what the streamer said), no
# @mentions (they would tag strangers / break X's automation rules), few rotating hashtags.
JUNK_RE = re.compile(r"leak|n[i1]gg|f[a@]gg|retard|\bkys\b", re.I)
HASHTAGS = ["#gta", "#gaming", "#twitch", "#gtavi", "#gta6clips", "#vicecity", "#gamingclips", "#streamer"]

def home(m):
    return {"kick": f"kick.com/{m['channel']}", "youtube": f"youtube.com/@{m['channel']}"}.get(
        m.get("platform"), f"twitch.tv/{m['channel']}")

# gta6fanpageclip: a curator of streamer reactions - no bananas, no score, its own hashtags
FAN_HASHTAGS = ["#gtavi", "#gta6gameplay", "#gta6reaction", "#streamerclips", "#twitchclips",
                "#vicecitygta6", "#gamingshorts", "#leonida"]

# Launch captions (17. 11.): built for COMMENTS and SHARES instead of generic hooks.
# BAIT = a question people answer in the comments, SHARE = a reason to send it to someone
# (streamer-fan lines with the name). Rotated without repeats (the brand's post counter),
# every network of one post gets a different line. No @ mentions. SlopMonster lint 5/5 + Codex cleanse.
BAIT = [
    "Guess the banana score before the end 🍌", "S tier or D tier? 👇", "Rate this 1-10 before the bananas drop 🍌",
    "Was {s} right here? Yes or no 👇", "Call the score in the comments before it lands 🍌", "W or L for {s}? 👇",
    "Higher or lower than 7 bananas? 👇", "Which second got you? 👇", "Could you have saved this? Be honest 👇",
    "What score would you give {s}? 👇",
]
SHARE = [
    "Send this to the friend who drives like this", "Your boy needs to see the ending",
    "Send this to whoever you're playing GTA 6 with tonight", "{s} fans, this one is for you",
    "Show this to your {s} friend and watch them defend it", "Send this to someone who still hasn't bought GTA 6",
    "Every group chat has someone who plays like this. Send it to them", "{s} fans will say this was planned",
    "Forward this to your co-op partner. No explanation needed", "Your friend who screams at games needs this one",
]
FAN_BAIT = [
    "Rate the heat 1-5 🔥", "How hot was that on a scale of 1 to 5? 🔥", "Too much or just enough? 👇",
    "Did {s} handle that well? Be honest 👇", "One word for that reaction 👇", "Guess the heat level before the end 🔥",
    "Would you watch this twice? 👀", "Smooth or messy? 👇", "Heat check. What's your number? 🔥",
    "Did that make you rewind? 👇",
]
FAN_SHARE = [
    "Send this to the friend who says GTA is boring", "Your GTA 6 partner needs to see this",
    "{s} fans, you'll want to save this one", "Send this to someone who needs a laugh tonight",
    "Show this to your {s} friend and watch their face", "Save it, then drop it in the group chat",
    "Send this to whoever you'll ride with in Vice City", "{s} fans saw this coming. Did you?",
    "Send this to the friend who rewinds everything", "Your Leonida crew should see this one",
]
NETS = ["instagram", "tiktok", "youtube", "facebook", "twitter", "threads"]

def rot(pool, m, service, salt=0):
    """Line for this post: the brand's post counter moves through the pool (no repeats in a row),
    each network one step further, so one clip's posts never share a line."""
    k = int(m.get("_seq") or 0) + (NETS.index(service) if service in NETS else 0) + salt
    return pool[k % len(pool)]

_league = {}

def league_line(m):
    """'#2 in the Banana League this week' for a top 10 streamer (data line, bananas only)."""
    if ZNACKA != "bananas" or not BRAND.get("league") or m.get("platform") == "top3":
        return ""
    try:
        if "rows" not in _league:
            import liga
            _league["rows"] = {r["channel"]: r["rank"] for r in liga.table(7)}
        rank = _league["rows"].get(m.get("channel"))
    except Exception as e:                        # the league is a bonus: never cost a post
        log(f"  league: {e}")
        return ""
    return f"#{rank} in the Banana League this week" if rank and rank <= 10 else ""

def chat_line(m):
    """Live chat clips: 'Chat went 14× during this' - the measured ratio only, nothing estimated."""
    x = m.get("chat_x")
    return f"Chat went {int(x)}× during this" if isinstance(x, (int, float)) and x >= 3 else ""

def caption_fanpage(m, service):
    rnd = random.Random(f"{m['id']}:{service}:fanpage")
    s = (m.get("channel") or "they")[:1].upper() + (m.get("channel") or "they")[1:]
    hook = rot(FAN_BAIT, m, service).format(s=s)          # comment bait first, a share trigger after
    q = (m.get("quote") or "").strip()
    q = f"\"{q}\"" if q else ""
    cta = rot(FAN_SHARE, m, service, 3).format(s=s)
    chat = chat_line(m)
    src = f"Streamer: {home(m)}"
    if service in ("twitter", "threads"):
        text = "\n\n".join(x for x in (hook, q, chat, cta, f"{src} #GTA6") if x)
        return text if len(text) <= 270 else "\n\n".join(x for x in (hook, f"{src} #GTA6") if x)
    tags = rnd.sample(FAN_HASHTAGS, 2)
    return "\n\n".join(x for x in (hook, q, chat, cta, src, " ".join(["#gta6", "#gta6clips"] + tags)) if x)

# Experiments (experimenty.py, their own slot): one line about the format, every streamer credited.
# SlopMonster lint 5/5 + Codex cleanse. {s} = the first streamer, {k} = how many.
EXP_LINES = {
    "multi_pov": ["Same moment, {k} streamers, {k} different endings. Which was best? 👇",
                  "One spot in GTA 6, {k} streamers. Pick the run you'd copy 👇",
                  "{k} streamers face the same moment. Which ending wins? 👇"],
    "story": ["{s}'s stream in 3 moments. Which one was the peak? 👇",
              "3 moments from {s}'s stream. Rate the night 1-5 🔥",
              "How {s}'s GTA 6 stream went, in 3 clips 👇"],
    "first": ["{s} found it first. Check the timestamps 🍌",
              "{k} streamers found the same thing. {s} got there first 🍌",
              "Same find, {k} streamers. Compare the timestamps 👇"],
    "tier3": ["3 moments from today in one tier list 🍌 Agree with the rankings? 👇",
              "Today's GTA 6 moments, ranked 🍌 Where would you put them? 👇",
              "3 clips on one tier list. Would you move any of them? 👇"],
}
EXP_TITLES = {"multi_pov": "GTA 6: same moment, {k} streamers", "story": "GTA 6: {s}'s stream in 3 moments",
              "first": "GTA 6: {s} found it first", "tier3": "GTA 6 moments of the day, ranked"}

def caption_exp(m, service):
    items = m.get("items") or []
    s = (items[0]["channel"] if items else "they")
    s = s[:1].upper() + s[1:]
    hook = rot(EXP_LINES.get(m.get("concept"), ["GTA 6 👇"]), m, service).format(s=s, k=len(items))
    credits = "\n".join(f"🎮 {home(x)}" for x in items)
    if service in ("twitter", "threads"):
        return f"{hook}\n\n{credits}\n\n#GTA6"
    tags = ["#gta6", "#gta6bananaclips"] if ZNACKA == "bananas" else ["#gta6", "#gta6clips"]
    return f"{hook}\n\n{credits}\n\n" + " ".join(tags + random.Random(f"{m['id']}:{service}").sample(
        HASHTAGS if ZNACKA == "bananas" else FAN_HASHTAGS, 2))

def caption(m, service):
    if m.get("platform") == "exp":
        return caption_exp(m, service)
    if ZNACKA == "fanpage" and m.get("platform") != "top3":
        return caption_fanpage(m, service)
    if m.get("platform") == "top3":                  # daily Top 3: credit every streamer
        lines = [f"#{i} {home(x)}" for i, x in enumerate(m["items"], 1)]
        if service in ("twitter", "threads"):
            return "Top 3 GTA 6 moments of the day 🍌\n\n" + "\n".join(lines) + "\n\n#GTA6"
        return ("Top 3 GTA 6 moments of the day 🍌 Which one wins?\n\n" + "\n".join(lines)
                + "\n\n#gta6 #gta6bananaclips #gaming #gta6clips")
    # Aleš 16. 11.: more entertaining and engaging. hook line + the streamer's own words + banana
    # score + a question for the comments + credit; a different combination per clip AND network
    # 17. 11.: comment bait first, the streamer's own words, banana score + league rank (data),
    # a share trigger, credit; a different line per post AND network (rot)
    rnd = random.Random(f"{m['id']}:{service}")
    s = (m.get("channel") or "they")[:1].upper() + (m.get("channel") or "they")[1:]
    hook = rot(BAIT, m, service).format(s=s)
    q = (m.get("quote") or "").strip()
    q = f"“{q}”" if q else ""
    score = (f"🍌 Banana score: {m['rating']}/10" + (" GOLDEN" if m.get("golden") else "")) if m.get("rating") else ""
    if re.search(r"score|bananas|1-10", hook, re.I):  # a guessing question: never spoil the answer in the text
        score = ""
    data = " · ".join(y for y in (score, league_line(m), chat_line(m)) if y)
    cta = rot(SHARE, m, service, 3).format(s=s)
    src = f"🎮 {home(m)}"
    if service in ("twitter", "threads"):            # short: X counts 280 characters
        text = "\n\n".join(x for x in (hook, q, chat_line(m), cta, f"{src} #GTA6") if x)
        return text if len(text) <= 270 else "\n\n".join(x for x in (hook, f"{src} #GTA6") if x)
    tags = rnd.sample(HASHTAGS, 2)
    return "\n\n".join(x for x in (hook, q, data, cta, src,
                                   " ".join(["#gta6", "#gta6bananaclips"] + tags)) if x)

def youtube_title(m):
    """
    'GTA 6' and the streamer first (that is what people search), then the clip title -
    viewer titles are often junk ('yoooo') or risky ('LEAK'): then the quote.
    """
    if m.get("platform") == "top3":
        return "Top 3 GTA 6 Moments Today 🍌 #shorts"
    if m.get("platform") == "exp":
        items = m.get("items") or [{"channel": "streamers"}]
        t = EXP_TITLES.get(m.get("concept"), "GTA 6 moments").format(s=items[0]["channel"], k=len(items))
        return t[:90] + " #shorts"
    t = (m.get("title") or "").strip()
    if len(t) < 12 or JUNK_RE.search(t) or sum(c.isalpha() for c in t) < len(t) / 2:
        t = (m.get("quote") or "").strip() or ("wild reaction" if ZNACKA == "fanpage" else "insane moment")
    head = f"{m['channel']} GTA 6 moment: " if ZNACKA == "fanpage" else f"GTA 6: {m['channel']} - "
    return head + t[: 100 - len(head) - len(" #shorts")] + " #shorts"

def in_day(rec, day):
    """Is the post due inside this publishing day (12:00 that day to 12:00 the next, Prague)?"""
    try:
        t = datetime.fromisoformat(rec.get("due") or "")
    except ValueError:
        return False
    start = datetime.combine(day, datetime.min.time(), TZ) + timedelta(hours=12)
    return start <= t < start + timedelta(days=1)

def daily_quota(day):
    """Ramp-up for new accounts (brand's / sources.json 'ramp': {date: posts per day}); 10 if not set."""
    ramp = BRAND.get("ramp") or load_json(HERE / "sources.json", {}).get("ramp") or {}
    n = None
    for d, k in sorted(ramp.items()):
        if date.fromisoformat(d) <= day:
            n = k
    return n if n is not None else (min(ramp.values()) if ramp else 10)

def month_gb():
    """
    Netlify bandwidth this month, estimated: every published video is fetched by Buffer for each
    of the 6 networks (+1 download from an inbox). Netlify: 20 credits per GB -> 1000 credits = 50 GB.
    """
    start = datetime.now(TZ).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    mb = 0.0
    files = {HERE / b.get("published", "publikovano.json")      # both accounts share one Netlify site
             for b in brands_cfg().values() if isinstance(b, dict)} | {PUBLISHED}
    for rec in (r for f in files for r in load_json(f, {}).values()):
        try:
            if datetime.fromisoformat(rec.get("due", "")) >= start:
                mb += (rec.get("mb") or 20) * 7
        except ValueError:
            pass
    return mb / 1024

def over_budget(share=0.9):
    """True once the month's Netlify transfer nears the budget (sources.json netlify_gb_month)."""
    budget = load_json(HERE / "sources.json", {}).get("netlify_gb_month", 45)
    return month_gb() >= share * budget

def budget_level():
    """
    0 = fine, 1 = over netlify_gb_soft (no extras over the obligatory quota, no Top 3, both brands),
    2 = over netlify_gb_hard (fanpage stops, bananas only the obligatory quota). No brands.json: as before.
    """
    cfg = brands_cfg()
    if "netlify_gb_soft" not in cfg:
        return 1 if over_budget() else 0
    gb = month_gb()
    return 2 if gb >= cfg.get("netlify_gb_hard", 1e9) else 1 if gb >= cfg["netlify_gb_soft"] else 0

def used_today(day):
    """Posts of the quota today (the Top 3 is an extra, never counted)."""
    return sum(in_day(rec, day) for k, rec in load_json(PUBLISHED, {}).items() if not k.startswith(("top3_", "exp_")))

def extra_score(day, src_cfg):
    """Score a clip needs to go out OVER the obligatory quota: the brand's extra_ramp (launch week
    19.-25. 11.: good_score, every good clip up to max_per_day), else sources.json extra_score."""
    n = None
    for d, k in sorted((BRAND.get("extra_ramp") or {}).items()):
        if date.fromisoformat(d) <= day:
            n = k
    return n if n is not None else src_cfg.get("extra_score", 5000)

def remaining(day):
    """Today's obligatory quota (ramp) minus what earlier runs already scheduled (hot posts count, Top 3 not)."""
    return max(0, daily_quota(day) - used_today(day))

def remaining_max(day):
    """Up to max_per_day (10) when the extra clips are strong enough (Aleš: 8 is a must, 10 if worth it)."""
    top = BRAND.get("max_per_day") or load_json(HERE / "sources.json", {}).get("max_per_day", 10)
    return max(0, max(daily_quota(day), top) - used_today(day))

def post_input(ch, m, video_url, due, draft):
    service = ch["service"].lower()
    inp = {
        "text": caption(m, service),
        "channelId": ch["id"],
        "schedulingType": Enum("automatic"),
        "mode": Enum("addToQueue" if draft else "shareNow" if m.get("now") else "customScheduled"),
        "assets": [{"video": {"url": video_url}}],
    }
    if not draft and not m.get("now"):
        inp["dueAt"] = due.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    else:
        inp["saveToDraft"] = True
    if service in ("instagram", "tiktok"):            # cover = the payoff frame (banana + reaction)
        pk = (m.get("peaks") or {}).get(ZNACKA) or m.get("peak") or 1.0     # the brand's own cut (hooks differ)
        inp["assets"][0]["video"]["metadata"] = {"thumbnailOffset": int(max(0.5, pk) * 1000)}
    if service == "youtube":
        inp["metadata"] = {"youtube": {"title": youtube_title(m), "categoryId": "20"}}   # 20 = Gaming
    elif service == "instagram":
        inp["metadata"] = {"instagram": {"type": Enum("reel"), "shouldShareToFeed": True}}
    elif service == "facebook":
        inp["metadata"] = {"facebook": {"type": Enum("reel")}}
    return inp

def create_post(inp, key):
    q = ("mutation { createPost(input: " + gql_value(inp) + ") { "
         "... on PostActionSuccess { post { id } } ... on MutationError { message } } }")
    res = gql(q, key)["createPost"]
    if res.get("message"):
        raise RuntimeError(res["message"])
    return res["post"]["id"]

def slot_times(day, n):
    """The publishing day runs from 14:00 to 05:00 the next morning (Prague). Base slots: they mark
    which slot is taken (record 'slot'); the post itself goes out at jittered(slot)."""
    out = []
    for s in SLOTS[:n]:
        t = datetime.strptime(s, "%H:%M").time()
        out.append(datetime.combine(day + timedelta(days=1 if t.hour < 12 else 0), t, TZ))
    return out

def jittered(slot, day, znacka=None):
    """
    Aleš 17. 11.: never on the full / half hour and never the same time every day. Each slot moves
    by -25..+25 min, drawn from (day, brand) - the same run always gives the same times.
    """
    rnd = random.Random(f"{day}:{znacka or ZNACKA}:slots")
    shift = {}
    for s in SLOTS:
        shift[s] = rnd.choice([d for d in range(-25, 26)
                               if (datetime.strptime(s, "%H:%M") + timedelta(minutes=d)).minute not in (0, 30)])
    return slot + timedelta(minutes=shift[slot.strftime("%H:%M")])

def net_due(due, day, service, znacka=None):
    """Each network a little later (0-9 min, its own draw per day + brand, so no fixed order),
    still never landing on :00 / :30."""
    order = list(range(10))
    random.Random(f"{day}:{znacka or ZNACKA}:{service}").shuffle(order)
    for d in order:
        t = due + timedelta(minutes=d)
        if t.minute not in (0, 30):
            return t
    return due

# -----------------------------------------------------------------------------
# commands

def cmd_kanaly(a):
    """Channels of every Buffer account we have a key for (free plan = 3 channels per account)."""
    load_env()
    keys = brand_keys()
    if not keys:
        sys.exit(f"missing in .env: {', '.join(BRAND.get('buffer_keys') or ['BUFFER_API_KEY_1', 'BUFFER_API_KEY_2'])}"
                 " (see .env.example)")
    chans = []
    for key in keys:
        orgs = gql("query { account { organizations { id name } } }", key)["account"]["organizations"]
        for o in orgs:
            q = 'query { channels(input: {organizationId: ' + json.dumps(o["id"]) + '}) { id name displayName service isQueuePaused } }'
            for c in gql(q, key)["channels"]:
                chans.append({"id": c["id"], "service": c["service"], "name": c.get("displayName") or c["name"],
                              "paused": c.get("isQueuePaused"), "key": key})
                log(f"{key}: {c['service']:<10} {c.get('displayName') or c['name']}  ({c['id']})")
    save_json(CHANNELS, chans)
    log(f"saved {len(chans)} channels to {CHANNELS.name} (delete the ones we should not post to)")

def cmd_den(a):
    now = datetime.now(TZ)
    day = now.date() if a.den == "dnes" else date.fromisoformat(a.den) if a.den else now.date() + timedelta(days=1)
    # slots still ahead of us (+20 min for the deploy and for Buffer to fetch the video); a slot is
    # taken by its base time ('slot'; older records and hot posts only have 'due')
    taken = {rec.get("slot") or rec.get("due") for rec in load_json(PUBLISHED, {}).values()}   # an earlier run today
    level = budget_level()
    if level >= 2 and ZNACKA == "fanpage":   # Netlify hard limit: the second account waits for next month
        print(f"::warning::Netlify: ~{month_gb():.0f} GB this month, {BRAND.get('name', ZNACKA)} paused")
        return
    must = min(a.n, remaining(day))          # obligatory part of the daily quota (8)
    cap = min(a.n, remaining_max(day))       # with strong extras up to 10
    if level:                                # Netlify < 1000 credits/month: extras pause, the 8 always go out
        cap = must
        print(f"::warning::Netlify: ~{month_gb():.0f} GB this month, extra videos and Top 3 paused")
    quota = must
    free = [t for t in slot_times(day, len(SLOTS))
            if jittered(t, day) > now + timedelta(minutes=20) and t.isoformat() not in taken]
    src_cfg = load_json(HERE / "sources.json", {})
    good, extra = src_cfg.get("good_score", 1000), extra_score(day, src_cfg)
    # Aleš: do not burn the 8 on average clips. An average one only goes out when the 8 would
    # otherwise be missed (no more free slots than posts still owed today).
    owed_now = must >= len(free)
    pre = src_cfg.get("youtube_prelaunch") or {}
    yt_cap = pre.get("per_day", 3) if pre and day < date.fromisoformat(pre.get("until", "2000-01-01")) else src_cfg.get("youtube_per_day", 1)
    yt_left = yt_cap - sum(
        k.startswith("youtube_") and in_day(rec, day) for k, rec in load_json(PUBLISHED, {}).items())
    picks = []
    for m in candidates():
        if len(picks) >= min(len(free), cap):
            break
        if m.get("platform") == "youtube":
            if yt_left <= 0:
                continue
        if len(picks) < must:
            if m["score"] < good and not owed_now:
                continue
        elif m["score"] < extra:
            continue
        if m.get("platform") == "youtube":
            yt_left -= 1
        picks.append(m)
    slots = sorted(sorted(free, key=lambda t: PRIORITY.index(t.strftime("%H:%M")))[: len(picks)])
    times = [jittered(t, day) for t in slots]
    # hot path: a clip from the last 3 h that is exploding goes out in 25 min, not at the next slot
    hot_due = (now + timedelta(minutes=2)).replace(second=0, microsecond=0)   # the deploy below takes ~1 min
    # ... but never eat the evening: `hot_reserve` posts of the day stay for the UK/US prime-time slots
    if (picks and quota > src_cfg.get("hot_reserve", 3) and picks[0].get("platform") != "youtube" and picks[0].get("created")
            and picks[0].get("age_h", 99) <= 3 and picks[0]["score"] >= src_cfg.get("hot_score", 2000)
            and in_day({"due": hot_due.isoformat()}, day) and hot_due < times[0] - timedelta(minutes=30)):
        picks[0]["hot"] = True
        times[0], slots[0] = hot_due, None          # hot post: takes no slot (as before)
        log(f"HOT: {picks[0]['channel']} '{picks[0].get('title', '')[:40]}' -> {hot_due:%H:%M}")
    chans = load_json(CHANNELS, [])
    seq0 = len(load_json(PUBLISHED, {}))     # caption rotation: the brand's post counter (no line twice in a row)
    for i, m in enumerate(picks):
        m["_seq"] = seq0 + i
    mode = "ZKOUŠKA" if a.zkouska else ("NAOSTRO" if a.ostre else "koncepty")
    log(f"{day} [{ZNACKA}]: {len(picks)} shorts (quota {quota}, over it only score >= {extra}, up to {cap})"
        f" x {len(chans) or '?'} channels ({mode})")
    if os.environ.get("GITHUB_ACTIONS") and not a.ostre and not a.zkouska:
        print("::warning::Buffer: jen KONCEPTY, nic nevyjde. Na GitHubu nastav promennou OSTRE=1.")
    if a.zkouska:
        for m, due in zip(picks, times):
            log(f"{due:%H:%M} {m['platform']}/{m['channel']} '{m.get('title', '')[:50]}' ({m.get('views')} views)")
            log("   " + "  ".join(f"{c['service'].lower()} {net_due(due, day, c['service'].lower()):%H:%M}"
                                  for c in chans))
            fake = {"id": "CHANNEL_ID", "service": "youtube"}
            print("   ", gql_value(post_input(fake, m, "https://media.example/clip.mp4", due, not a.ostre))[:400])
        exp_post(a, day, now)                        # experiment of the day: plan only
        return
    if not picks:
        if not a.zkouska and load_json(CHANNELS, []):
            top3_only(a, day, now)                   # the day's quota is full: the Top 3 may still be due
            exp_post(a, day, now)                    # experiment: own slot, outside the quota
        return
    load_env()
    need("NETLIFY_AUTH_TOKEN", "NETLIFY_SITE_ID", "MEDIA_BASE_URL")
    if not brand_keys() or not chans:
        sys.exit(f"no Buffer keys or channels: fill .env and run `publikuj.py kanaly --znacka {ZNACKA}` first")

    # 1) one deploy of this run's videos as a Netlify DRAFT deploy: 0 credits (a production
    # deploy is 15), and its permalink stays forever, so earlier videos need no carry-over
    done = load_json(PUBLISHED, {})
    stage = OUT / "_media"
    stage.mkdir(parents=True, exist_ok=True)
    files = {}
    plan = []
    slot_of = {}
    for m, due, slot in zip(picks, times, slots):
        slot_of[m["id"]] = slot.isoformat() if slot else None
        rel = f"{day:%Y%m%d}/{safe_name(m['id'])}.mp4"
        src = Path(m["short"]) if not m.get("remote") else stage / f"inbox_{safe_name(m['id'])}.mp4"
        if m.get("remote"):                          # YouTube short from the worker PC's inbox
            try:
                src.write_bytes(http(m["short"], timeout=300)[1])
            except Exception as e:
                log(f"  inbox short unavailable, skipped: {m['short']} ({e})")
                continue
        files[rel] = src
        plan.append((m, due, rel))
    # the daily Top 3 (our own curated format): once the day's quota is full
    top3 = make_top3(day, done, plan, stage)
    if top3:
        plan.append(top3)
        files[top3[2]] = top3[0].pop("file")
    base = inbox_deploy(files, b"[]", MEDIA_TITLE)
    prune_drafts()

    # 2) Buffer posts, one time slot per short, every channel (X/Threads capped per day)
    caps = load_json(HERE / "sources.json", {}).get("caps") or {}
    used, ok, failed = {}, 0, 0
    for m, due, rel in plan:
        url = f"{base}/{rel}"
        log(f"{due:%H:%M} {m['platform']}/{m['channel']} '{m.get('title', '')[:50]}' -> {url}")
        if not is_public(url):
            log("  not reachable after deploy, skipped")
            failed += 1
            continue
        posts = {}
        for i, ch in enumerate(chans):
            service = ch["service"].lower()
            if used.get(service, 0) >= caps.get(service, 99):
                continue
            ch_due = net_due(due, day, service)            # Aleš 17. 11.: each network a few minutes apart
            m["now"] = bool(m.get("hot"))                  # hot path: everywhere NOW (Buffer shareNow)
            try:
                posts[ch["id"]] = create_post(post_input(ch, m, url, ch_due, not a.ostre), ch.get("key", "BUFFER_API_KEY"))
                used[service] = used.get(service, 0) + 1
                ok += 1
                log(f"  {service}: ok")
            except Exception as e:
                failed += 1
                log(f"  {service}: FAILED {e}")
        size = files.get(rel)
        mb = round(Path(size).stat().st_size / 1e6, 1) if size and Path(size).exists() else None
        done[m["id"]] = {"url": url, "path": rel, "due": due.isoformat(), "draft": not a.ostre, "posts": posts, "mb": mb,
                         "channel": m.get("channel"), "platform": m.get("platform"), "score": round(m.get("score") or 0, 1),
                         "hot": bool(m.get("hot")), "rating": m.get("rating"), "views": m.get("views"),   # Banana League (liga.py)
                         "golden": bool(m.get("golden"))}
        if slot_of.get(m["id"]):
            done[m["id"]]["slot"] = slot_of[m["id"]]
        save_json(PUBLISHED, done)
    exp_post(a, day, now)                            # experiment: own slot, outside the quota, after the classics
    # make failures visible: a red run = an e-mail from GitHub
    if failed:
        print(f"::warning::Buffer: {failed} postu se nepovedlo, {ok} ok")
    if plan and not ok:
        print("::error::Buffer: nevysel ani jeden post (klic, Netlify nebo Buffer API)")
        sys.exit(1)

# Experiments (experimenty.py, Aleš 17. 11.): max exp_max_per_day a day per brand (2 when a concept beats
# the classics), their own slots between the classic ones, never in the quota, X/Threads caps kept.
EXP_SLOTS = ["19:15", "22:15", "01:15"]       # UK evening, US east prime, US west after work

def exp_slots(day):
    """(base, due) of the experiment slots: due moves -10..+10 min (per day + brand), never :00 / :30."""
    rnd = random.Random(f"{day}:{ZNACKA}:exp")
    out = []
    for s in EXP_SLOTS:
        t = datetime.strptime(s, "%H:%M").time()
        base = datetime.combine(day + timedelta(days=1 if t.hour < 12 else 0), t, TZ)
        d = rnd.choice([x for x in range(-10, 11) if (base + timedelta(minutes=x)).minute not in (0, 30)])
        out.append((base, base + timedelta(minutes=d)))
    return out

def exp_post(a, day, now):
    """Post today's rendered experiment (out/_exp/<brand>/) in a free experiment slot. Never raises."""
    try:
        import experimenty as E
        ready = E.waiting(ZNACKA, day)
        if a.zkouska:
            for line in E.preview(ZNACKA, day):
                log(line)
        if not ready:
            return
        if budget_level():
            print(f"::warning::Netlify: ~{month_gb():.0f} GB this month, experiment {ready[0]['concept']} paused")
            return
        done = load_json(PUBLISHED, {})
        m = ready[0]
        n_today = sum(1 for k, rec in done.items() if k.startswith("exp_") and in_day(rec, day))
        if n_today >= E.per_day(ZNACKA, m["concept"]) or not E.enabled(ZNACKA, m["concept"]):
            return                                     # limit reached, or switched off since it was rendered
        game = load_json(HERE / "sources.json", {}).get("game", "Grand Theft Auto VI|GTA ?VI|GTA ?6")
        if not re.search(game, m.get("category") or "", re.I):
            log(f"exp: not published (category '{m.get('category')}' is not {game})")
            return
        taken = {rec.get("slot") for k, rec in done.items() if k.startswith("exp_")}
        free = [(b, t) for b, t in exp_slots(day) if t > now + timedelta(minutes=20) and b.isoformat() not in taken]
        if not free:
            log("exp: no experiment slot left today")
            return
        base, due = free[0]
        chans = load_json(CHANNELS, [])
        log(f"exp {m['concept']} ({', '.join(x['channel'] for x in m['items'])}) -> {due:%H:%M}"
            f" x {len(chans)} channels{' (ZKOUSKA)' if a.zkouska else ''}")
        if a.zkouska:
            fake = {"id": "CHANNEL_ID", "service": "youtube"}
            print("   ", gql_value(post_input(fake, m, "https://media.example/exp.mp4", due, not a.ostre))[:400])
            return
        load_env()
        if not (brand_keys() and chans and os.environ.get("NETLIFY_AUTH_TOKEN")):
            return
        rel = f"{day:%Y%m%d}/{safe_name(m['id'])}.mp4"
        url = f"{inbox_deploy({rel: Path(m['file'])}, b'[]', MEDIA_TITLE)}/{rel}"
        if not is_public(url):
            print("::warning::exp: not reachable after deploy")
            return
        caps = load_json(HERE / "sources.json", {}).get("caps") or {}
        svc = {c["id"]: c["service"].lower() for c in chans}
        used = {}
        for rec in done.values():                      # X / Threads: the day's cap counts every post
            if in_day(rec, day):
                for cid in rec.get("posts") or {}:
                    used[svc.get(cid)] = used.get(svc.get(cid), 0) + 1
        m["_seq"] = len(done)
        posts = {}
        for ch in chans:
            service = ch["service"].lower()
            if used.get(service, 0) >= caps.get(service, 99):
                log(f"  exp {service}: daily cap reached, skipped")
                continue
            try:
                posts[ch["id"]] = create_post(post_input(ch, m, url, net_due(due, day, service), not a.ostre),
                                              ch.get("key", "BUFFER_API_KEY"))
                log(f"  exp {service}: ok")
            except Exception as e:
                log(f"  exp {service}: FAILED {e}")
        if not posts:                                  # nothing went out: stays pending, the next run retries
            print("::warning::exp: no Buffer post succeeded")
            return
        done = load_json(PUBLISHED, {})
        done[m["id"]] = {"url": url, "path": rel, "due": due.isoformat(), "slot": base.isoformat(), "draft": not a.ostre,
                         "posts": posts, "mb": round(Path(m["file"]).stat().st_size / 1e6, 1), "channel": "exp",
                         "platform": "exp", "concept": m["concept"], "items": [x["id"] for x in m["items"]]}
        save_json(PUBLISHED, done)
    except Exception as e:                            # an experiment never costs a classic post
        print(f"::warning::exp: {str(e)[-200:]}")

TOP3_FONT = HERE / "fonts" / "cap" / "montserrat-900.ttf"

def top3_only(a, day, now):
    """A run with no new shorts: build, deploy and post the day's Top 3 if it is due."""
    load_env()
    if not (brand_keys() and os.environ.get("NETLIFY_AUTH_TOKEN")):
        return
    done = load_json(PUBLISHED, {})
    stage = OUT / "_media"
    stage.mkdir(parents=True, exist_ok=True)
    top3 = make_top3(day, done, [], stage)
    if not top3:
        return
    m, due, rel = top3
    base = inbox_deploy({rel: m.pop("file")}, b"[]", MEDIA_TITLE)    # draft deploy, 0 credits
    url = f"{base}/{rel}"
    if not is_public(url):
        print("::warning::Top 3: not reachable after deploy")
        return
    posts, caps, used = {}, load_json(HERE / "sources.json", {}).get("caps") or {}, {}
    for i, ch in enumerate(load_json(CHANNELS, [])):
        service = ch["service"].lower()
        try:
            posts[ch["id"]] = create_post(post_input(ch, m, url, net_due(due, day, service), not a.ostre),
                                          ch.get("key", "BUFFER_API_KEY"))
            log(f"  top3 {service}: ok")
        except Exception as e:
            log(f"  top3 {service}: FAILED {e}")
    done[m["id"]] = {"url": url, "path": rel, "due": due.isoformat(), "draft": not a.ostre, "posts": posts,
                     "channel": "top3", "platform": "top3"}
    save_json(PUBLISHED, done)

def make_top3(day, done, plan, stage):
    """
    Daily 'Top 3 GTA 6 moments' (Fable 15. 11.: a curated series is the 'something new' the
    platforms ask for): once the day's quota is full, the 3 best shorts of the day, #3 -> #1,
    each with a big number for the first 1.2 s, posted 75 min after the day's last short.
    Returns (meta, due, site path) with meta['file'] = the local mp4, or None.
    """
    key = f"top3_{day}"
    if not BRAND.get("top3", True) or key in done or remaining(day) - len(plan) > 0 or budget_level():
        return None
    items = []                                       # (score, meta-ish, local file or url, due)
    for k, rec in done.items():
        if (not k.startswith(("top3_", "exp_")) and in_day(rec, day) and rec.get("url") and rec.get("channel")
                and rec.get("platform") != "youtube"):
            items.append((rec.get("score") or 0, {"channel": rec.get("channel") or "?", "platform": rec.get("platform")},
                          rec["url"], datetime.fromisoformat(rec["due"])))
    for m, due, rel in plan:
        if m.get("platform") != "youtube":           # YouTube 'views' are the whole video's: not comparable
            items.append((m.get("score") or 0, m, m["short"] if not m.get("remote") else None, due))
    items = [x for x in items if x[2]]
    if len(items) < 3:
        return None
    best = sorted(items, key=lambda x: -x[0])[:3]
    srcs = []
    for i, (_, m, src, _) in enumerate(best):
        if str(src).startswith("http"):
            local = stage / f"top3_{i}.mp4"
            try:
                local.write_bytes(http(src, timeout=300)[1])
            except Exception as e:
                log(f"top3: {src} unavailable ({e})")
                return None
            src = local
        srcs.append(Path(src))
    total = 0.0
    for f in srcs:
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                                "-of", "json", str(f)], capture_output=True, text=True, timeout=60)
        info = json.loads(probe.stdout or "{}")
        if "audio" not in [x.get("codec_type") for x in info.get("streams", [])]:
            print(f"::warning::Top 3: {f.name} has no audio, skipped today")
            return None
        total += float(info.get("format", {}).get("duration") or 0)
    if total > 130:                                  # X takes at most 140 s
        print(f"::warning::Top 3: {total:.0f} s is too long for X, skipped today")
        return None
    order = list(reversed(range(3)))                 # countdown: #3, #2, #1
    font = str(TOP3_FONT).replace("\\", "/").replace(":", "\\:")
    parts = []
    for j, i in enumerate(order):
        parts.append(f"[{i}:v]drawtext=fontfile='{font}':text='#{i + 1}':fontsize=260:fontcolor=0xFFE135:"
                     f"borderw=10:bordercolor=black:x=(w-text_w)/2:y=760:enable='lt(t,1.2)'[v{j}]")
    flt = ";".join(parts) + ";" + "".join(f"[v{j}][{i}:a]" for j, i in enumerate(order)) + "concat=n=3:v=1:a=1[v][a]"
    out = stage / f"{key}.mp4"
    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error"]
    for f in srcs:
        cmd += ["-i", str(f)]
    cmd += ["-filter_complex", flt, "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "23", "-maxrate", "3.5M", "-bufsize", "7M", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if r.returncode or not out.exists():
        print(f"::warning::Top 3: ffmpeg failed {r.stderr[-300:]}")
        return None
    due = max(x[3] for x in items) + timedelta(minutes=75)
    meta = {"id": key, "platform": "top3", "channel": "top3", "category": "Grand Theft Auto VI",
            "items": [best[i][1] for i in range(3)], "peak": 1.0, "score": 0, "file": out, "title": "Top 3"}
    log(f"top3: {', '.join(x[1]['channel'] for x in best)} -> {due:%d.%m %H:%M}")
    return meta, due, f"{day:%Y%m%d}/top3.mp4"

def cmd_schranka(a):
    """
    Worker PC: its rendered YouTube shorts that are not published yet -> yt-inbox draft deploy.
    Redeploys only when there is a short the live inbox doesn't have (published ones simply stay
    until the next deploy; `den` skips them). --uklid deletes clip folders older than --dni.
    """
    load_env()
    need("NETLIFY_AUTH_TOKEN", "NETLIFY_SITE_ID")
    game = load_json(HERE / "sources.json", {}).get("game", "Grand Theft Auto VI|GTA ?VI|GTA ?6")
    cfg = brands_cfg()
    names = ["bananas"] + [k for k, v in cfg.items() if isinstance(v, dict) and k != "bananas"]
    done = {z: load_json(HERE / ((cfg.get(z) or {}).get("published") or "publikovano.json"), {}) for z in names}
    cutoff = time.time() - a.dni * 86400
    items, files = [], {}
    for meta_file in OUT.glob("*/*/meta.json"):
        if meta_file.parts[-3].startswith("_"):
            continue
        if meta_file.stat().st_mtime < cutoff:
            if a.uklid:
                shutil.rmtree(meta_file.parent, ignore_errors=True)
            continue
        m = load_json(meta_file, {})
        keep = m.get("id", "").startswith(a.prefix) if a.prefix else m.get("platform") == "youtube"
        if not keep or not re.search(game, m.get("category") or "", re.I):
            continue
        # every brand version of the clip that its brand has not published yet (`file` = bananas,
        # `file_fanpage` = fanpage); the clip drops out once all its brands have published it
        item = {k: v for k, v in m.items() if k not in ("short", "shorts", "base", "source")}
        for z in names:
            short = brand_short(m, meta_file, z)
            if not short or not Path(short).exists() or m.get("id") in done[z]:
                continue
            rel = f"yt/{safe_name(m['id'])}.mp4" if z == "bananas" else f"yt/{safe_name(m['id'])}_{z}.mp4"
            files[rel] = Path(short)
            item["file" if z == "bananas" else f"file_{z}"] = rel
        if any(k == "file" or k.startswith("file_") for k in item):
            items.append(item)
    now = time.time()                                # only the best few: `den` takes a handful a day anyway
    items.sort(key=lambda m: -(m.get("views") or 0) / (max((now - (m.get("created") or now)) / 3600, 0) + 1) ** 0.5)
    items = items[: a.max]
    files = {m[k]: files[m[k]] for m in items for k in m if k == "file" or k.startswith("file_")}
    sent = HERE / f"schranka_{a.nazev}.json"          # ids of the live inbox (local, not in git)
    live = set(load_json(sent, []))
    new = [m["id"] for m in items if m["id"] not in live]
    if not new:
        log(f"inbox: nothing new ({len(items)} waiting, all already on Netlify)")
        return
    url = inbox_deploy(files, json.dumps(items, ensure_ascii=False).encode(), a.nazev)
    save_json(sent, [m["id"] for m in items])
    log(f"inbox: {len(items)} shorts ({len(new)} new) -> {url}")

def main():
    p = argparse.ArgumentParser(description="gta6bananaclips: shorts -> Buffer")
    sub = p.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("kanaly", help="list Buffer channels and save them to buffer.json")
    k.set_defaults(fn=cmd_kanaly)
    d = sub.add_parser("den", help="best N shorts -> Buffer, one per time slot")
    d.add_argument("--n", type=int, default=10)
    d.add_argument("--den", help="YYYY-MM-DD or 'dnes' (default: tomorrow)")
    d.add_argument("--ostre", action="store_true", help="schedule for real (default: Buffer drafts)")
    d.add_argument("--zkouska", action="store_true", help="dry run: print the plan, touch nothing")
    d.set_defaults(fn=cmd_den)
    s = sub.add_parser("schranka", help="worker PC: YouTube shorts -> free Netlify draft deploy for GitHub")
    s.add_argument("--dni", type=float, default=3, help="only shorts rendered in the last N days")
    s.add_argument("--uklid", action="store_true", help="delete clip folders older than that")
    s.add_argument("--max", type=int, default=10, help="at most this many shorts in the inbox (best first)")
    s.add_argument("--nazev", default=INBOX_TITLE, help="draft deploy title (yt-inbox, live-inbox)")
    s.add_argument("--prefix", default="", help="only shorts whose id starts with this (live_); default: YouTube")
    s.set_defaults(fn=cmd_schranka)
    z = sub.add_parser("zbyva", help="print how many shorts today's quota still allows (for the workflow)")
    z.set_defaults(fn=lambda a: print(0 if ZNACKA == "fanpage" and budget_level() >= 2
                                      else remaining_max(datetime.now(TZ).date())))   # render enough for the strong extras
    for x in (k, d, s, z):
        x.add_argument("--znacka", default="bananas", help="account from brands.json: bananas (default) | fanpage")
    a = p.parse_args()
    set_brand(a.znacka)
    a.fn(a)

if __name__ == "__main__":
    main()
