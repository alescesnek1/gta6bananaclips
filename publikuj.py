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
  .venv/Scripts/python publikuj.py den --den dnes --n 10   # just in time: slots of the next 3 h + hot clips now

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
# KLIPY_OUT: the 24/7 PC renders into an ASCII path (19. 11.: its home folder with a diacritic broke ffmpeg)
OUT = Path(os.environ.get("KLIPY_OUT") or HERE / "out")
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
#   08:00   07:00     02:00    23:00    Australia/NZ evening (AEDT 18:00) - launch 18. 11.: AU/NZ play first
#   10:00   09:00     04:00    01:00    Australia prime time (AEDT 20:00)
#   11:00   10:00     05:00    02:00    Australia late, EU morning
# (CET; during summer time everything shifts together, the pattern stays.)
SLOTS = ["14:00", "17:00", "18:30", "20:00", "21:30", "23:00", "00:30", "02:00", "03:30", "05:00",
         "08:00", "10:00", "11:00"]
# Just-in-time (18. 11., before the launch): a run commits only the slots due within the next
# plan_horizon_h hours (sources.json, default 3), with the best clips it has at that moment; the
# rest of the day's quota stays open for better clips found later. Catch-up: when the free slots
# after the horizon (minus plan_spare_slots) could no longer reach the day's minimum, the run fills
# more now, average clips included. Hot clips (score >= hot_score, < hot_max_age_h old) and live
# chat clips go out at once (shareNow) whenever the day has quota left up to max_per_day.

NOW = None                              # tests: a fixed "now" (datetime in TZ); None = the clock
FAST = False                            # den --rychle: the live fast path (own record file, live clips only)

def now_tz():
    return NOW or datetime.now(TZ)

def pub_day(t=None):
    """The publishing day a moment belongs to: the window 12:00 that day .. 11:59 the next (Prague).
    01:30 still belongs to yesterday's day (its 02:00 slot, its quota), not to the coming one."""
    t = t or now_tz()
    return t.date() if t.hour >= 12 else t.date() - timedelta(days=1)

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

# Two writers, two files per brand (no git conflict on one JSON): normal runs write the brand's
# `published` file, the live fast path (den --rychle, its own concurrency group) writes the same name
# with _live (publikovano_live.json, publikovano_fanpage_live.json). Everything that counts or checks
# what went out (duplicates, quotas, slots, X/Threads caps, Top 3, Netlify budget) reads both.
def live_file(p):
    p = Path(p)
    return p.with_name(f"{p.stem}_live{p.suffix}")

def rec_files(znacka=None):
    """The brand's record files: [main, live]."""
    if znacka is None or znacka == ZNACKA:
        main = PUBLISHED
    else:
        main = HERE / ((brands_cfg().get(znacka) or {}).get("published") or "publikovano.json")
    return [main, live_file(main)]

def records(znacka=None):
    """Everything the brand published (both record files; the main file wins a shared id)."""
    main, live = rec_files(znacka)
    return {**load_json(live, {}), **load_json(main, {})}

def target_file():
    """The record file THIS run writes."""
    return live_file(PUBLISHED) if FAST else PUBLISHED

def all_record_files():
    """Record files of every brand (both writers)."""
    mains = {HERE / b.get("published", "publikovano.json") for b in brands_cfg().values() if isinstance(b, dict)}
    mains |= {PUBLISHED, HERE / "publikovano.json"}
    return sorted({f for m in mains for f in (m, live_file(m))})

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

class RateLimited(RuntimeError):
    """Buffer says wait longer than RATE_MAX_WAIT: the clip is skipped this run (the next run retries)."""

RATE_MAX_WAIT = 120

def gql(query, key="BUFFER_API_KEY"):
    body = json.dumps({"query": query}).encode()
    headers = {"Authorization": f"Bearer {os.environ[key]}", "Content-Type": "application/json"}
    for attempt in range(3):
        try:
            data = json.loads(http(API, body, headers, "POST", 60)[1])
            break
        except urllib.error.HTTPError as e:
            if e.code == 429:                      # rate limit: wait as told, but never longer than 2 min
                ra = e.headers.get("Retry-After") or ""
                wait = int(ra) if ra.isdigit() else 60
                if wait > RATE_MAX_WAIT or attempt >= 2:
                    raise RateLimited(f"Buffer rate limit (Retry-After {ra or '?'} s) - skipped this run")
                log(f"  Buffer rate limit, waiting {wait}s")
                time.sleep(wait)
                if lock_lost():                    # the wait may have outlived our publishing lock
                    raise RuntimeError("publishing lock lost while waiting for Buffer - not posted")
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

SHORT_LIMIT_S = 30.5          # klipuj STORY_CAP 30 s (classic loop short 12-20 s, cap 25)
OUTRO_LIMIT_S = 15.5          # fanpage ending B: the full model outro after the clip

def short_too_long(path, m):
    """Gate before deploy: a short longer than the owner's cap never goes out (whatever rendered it).
    Returns the reason or "". No ffprobe = no check (logged)."""
    if not shutil.which("ffprobe"):
        log("  length gate: ffprobe missing, not checked")
        return ""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                            str(path)], capture_output=True, text=True, timeout=60)
        d = float(r.stdout.strip())
    except Exception:
        return ""
    limit = SHORT_LIMIT_S + (OUTRO_LIMIT_S if m.get("ending") == "outro" else 0)
    return f"short {d:.1f} s over the {limit:.1f} s cap" if d > limit else ""

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
        for page in range(1, 6):                       # newest first: the old drafts are further down
            ds = netlify(f"/sites/{site}/deploys?per_page=100&page={page}") or []
            for d in ds:
                if d.get("title") not in (MEDIA_TITLE, INBOX_TITLE, "live-inbox") or d.get("context") == "production":
                    continue
                age = datetime.now(timezone.utc) - datetime.fromisoformat(d["created_at"].replace("Z", "+00:00"))
                if age > timedelta(days=days):
                    netlify(f"/deploys/{d['id']}", method="DELETE")
            if len(ds) < 100:
                break
    except Exception as e:
        log(f"  prune drafts: {e}")

def safe_name(clip_id):
    return re.sub(r"[^A-Za-z0-9_-]", "_", clip_id)

def netlify(path, data=None, method=None, ctype="application/json", timeout=120):
    """Netlify API call. 429 (deploys: 3 per minute) -> wait as told (max 120 s) and try again, 4 tries."""
    h = {"Authorization": f"Bearer {os.environ['NETLIFY_AUTH_TOKEN']}", "Content-Type": ctype}
    for attempt in range(4):
        try:
            return json.loads(http(f"{NETLIFY}{path}", data, h, method, timeout)[1] or b"null")
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 3:
                raise
            ra = e.headers.get("Retry-After") or ""
            wait = min(int(ra) if ra.isdigit() else 25 * (attempt + 1), 120)
            log(f"  Netlify rate limit, waiting {wait}s")
            time.sleep(wait)

# Netlify allows ~100 deploys a day per site (and 3 a minute). Every writer deploys (den per brand,
# live hand-over, experiments, Top 3, the PC's yt-inbox): non-essential deploys stop at DEPLOY_SOFT
# in the last 24 h, the publishing deploy of `den` (essential) at DEPLOY_HARD.
DEPLOY_SOFT, DEPLOY_HARD = 85, 97
_deploys = {}

def deploys_24h():
    """Deploys of the site created in the last 24 h (newest 100 are enough to see the limit)."""
    if "n" not in _deploys:
        try:
            ds = netlify(f"/sites/{os.environ['NETLIFY_SITE_ID']}/deploys?per_page=100") or []
            cut = datetime.now(timezone.utc) - timedelta(hours=24)
            _deploys["n"] = sum(1 for d in ds if d.get("created_at") and
                                datetime.fromisoformat(d["created_at"].replace("Z", "+00:00")) >= cut)
        except Exception as e:                    # unknown: do not block, the deploy itself will tell
            log(f"  Netlify deploy count unknown ({str(e)[-80:]})")
            _deploys["n"] = 0
    return _deploys["n"]

def inbox_deploy(files, inbox, title=None, essential=False):
    """
    files: {site path: local path}, inbox: bytes of inbox.json. A draft deploy (deploy preview,
    no credits) by file digest: Netlify asks only for files it doesn't have yet.
    """
    n = deploys_24h()
    if n >= (DEPLOY_HARD if essential else DEPLOY_SOFT):
        raise RuntimeError(f"Netlify: {n} deploys in 24 h, {'essential' if essential else 'non-essential'} deploy skipped")
    _deploys["n"] = n + 1
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
    titles = ("live-inbox",) if FAST else (INBOX_TITLE, "live-inbox")       # fast path: live clips only
    deploys = []
    try:                                                      # newest first; media drafts may push an inbox down
        for page in range(1, 4):
            ds = netlify(f"/sites/{os.environ['NETLIFY_SITE_ID']}/deploys?per_page=100&page={page}") or []
            deploys += ds
            if len(ds) < 100 or all(any(d.get("title") == t and d.get("state") == "ready" for d in deploys)
                                    for t in titles):
                break
    except Exception as e:                                    # no inbox must never stop Twitch/Kick posts
        log(f"inbox: unreadable ({e})")
        if not deploys:
            return []
    for title in titles:
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
        if TAKE_LONG:
            try:                                      # long-form must never break the shorts
                take_long(items, base)
            except Exception as e:
                log(f"inbox {title}: long-form rows skipped ({e})")
        out += items
    return out

# YouTube moments for long-form (worker PC): the inbox item carries a 16:9 720p cut of the source
# (`long_file`) and its long_pool.json row (`pool_row`). GitHub can't download from YouTube, so the
# row's source AND url become that file's Netlify draft URL (kompilace.ready downloads `url` with
# yt-dlp; no `section`, the file is already the cut). The draft is pruned after 4 days with the
# inbox (prune_drafts) - a row older than that is skipped by kompilace (download fails).
POOL_FILE = Path(os.environ.get("KLIPY_POOL") or HERE / "long_pool.json")
EXTRA_MB = HERE / "netlify_extra.json"   # {id: {due, mb}}: Netlify transfer that is not a short (month_gb)
LONG_FETCHES = 2                         # kompilace may fetch a source once per brand
TAKE_LONG = True                         # den --zkouska: off (a dry run touches nothing)

def take_long(items, base):
    pool = load_json(POOL_FILE, {})
    extra = load_json(EXTRA_MB, {})
    added = 0
    for m in items:
        row, rel = m.get("pool_row"), m.get("long_file")
        if not row or not rel or not row.get("id"):
            continue
        old = pool.get(row["id"]) or {}
        if str(old.get("source", "")).startswith("https://"):
            continue                                  # taken in by an earlier run
        url = f"{base}/{rel}"
        pool[row["id"]] = {**old, **{k: v for k, v in row.items() if v is not None and k != "section"},
                           "source": url, "url": url, "yt_url": row.get("url"), "added": int(time.time())}
        pool[row["id"]].pop("section", None)
        extra[f"long_{row['id']}"] = {"due": datetime.now(TZ).isoformat(timespec="seconds"),
                                      "mb": round((m.get("long_mb") or row.get("src_mb") or 10) * LONG_FETCHES, 1)}
        added += 1
    if added:
        tmp = POOL_FILE.with_name(f"{POOL_FILE.name}.{os.getpid()}.tmp")   # same compact format as klipuj.pool_add
        tmp.write_text(json.dumps(dict(sorted(pool.items())), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(POOL_FILE)
        cutoff = time.time() - 40 * 86400             # the ledger only needs this month
        extra = {k: v for k, v in extra.items()
                 if datetime.fromisoformat(v["due"]).timestamp() >= cutoff}
        save_json(EXTRA_MB, extra)
        log(f"inbox: {added} YouTube moments -> long_pool.json")

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

PLAN_DAY = None                          # the publishing day `den` plans (trailer cut-off uses it, not the clock)
MAX_FAILS = 2                            # a clip whose publishing failed this many times is skipped for good

def fails_file():
    """{clip id: failed publishing attempts} - committed with the records (zamek pust --uloz)."""
    return HERE / "publikace_chyby.json"

def add_fail(clip_id):
    f = fails_file()
    d = load_json(f, {})
    d[clip_id] = int(d.get(clip_id) or 0) + 1
    save_json(f, d)

def candidates(znacka=None):
    """
    Rendered shorts of this brand not yet published by it, best first: virality = views per hour
    of age (softened). Gate 2 of 2: only shorts whose saved stream category / video title matches
    the game in sources.json (GTA 6) can go out - whatever slipped past the render step.
    """
    znacka = znacka or ZNACKA
    game = load_json(HERE / "sources.json", {}).get("game", "Grand Theft Auto VI|GTA ?VI|GTA ?6")
    # Never twice: the id is checked against EVERY record file (both brands, normal + live fast path).
    # Aleš 17. 11.: one clip = one account. A clip the other brand already posted never goes out here
    # (render gives each clip one brand; this also covers the inbox and a clip rendered for both).
    done = {}
    for f in all_record_files() + rec_files(znacka):
        done.update(load_json(f, {}))
    for k, rec in list(done.items()):        # a clip used in an experiment never goes out again (either account)
        if k.startswith("exp_"):
            done.update({i: {} for i in rec.get("items") or [] if isinstance(i, str)})
    fails = load_json(fails_file(), {})
    done.update({k: {} for k, n in fails.items() if isinstance(n, int) and n >= MAX_FAILS})
    now = now_tz().timestamp()
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
        if FAST and not is_live(m):
            continue                                 # the fast path only carries live chat clips
        if is_live(m) and not load_json(HERE / "sources.json", {}).get("live_publish", True):
            continue                                 # live clips paused (18. 11.: launch-day cutscenes / game dialogue)
        if is_live(m) and (m.get("spike_z") or 0) < live_z_min(m, load_json(HERE / "sources.json", {})):
            continue                                 # chat explosion below today's threshold (tuned in sources.json)
        if (m.get("platform") == "youtube" and (PLAN_DAY or pub_day()) >= date.fromisoformat(
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
        src_c = load_json(HERE / "sources.json", {})
        if crossover(m, src_c):                                   # >= 2 star streamers named: x 1.5
            m["score"] *= 1.5
            m["crossover"] = True
        # Aleš 18. 11.: the quota goes to the famous streamers first, never burnt on no-names:
        # Kai Cenat / IShowSpeed / xQc x 2, other star streamers x 1.5
        ch_c = str(m.get("channel") or "").lower()
        if ch_c in {x.lower() for x in src_c.get("star_tier1", [])}:
            m["score"] *= 2.0
        elif ch_c in {x.lower() for x in src_c.get("star_streamers", [])}:
            m["score"] *= 1.5
        why = action_gate(m, src_c) or repeat_moment(m, done)
        if why:
            log(f"  not published ({why}): {m.get('id')}")
            continue
        out.append(m)
    # viewer-made Twitch/Kick clips first: a YouTube window's "views" are the whole video's,
    # not comparable, so YouTube only fills what is left (and at most youtube_per_day)
    return sorted(out, key=lambda m: (m.get("platform") == "youtube", -m["score"]))

# Display names for titles and the crossover check (login -> how people write it). Unknown logins: the
# login itself, capitalised. Logins that are ordinary words count only as the capitalised name.
NAMES = {"kaicenat": "Kai Cenat", "ishowspeed": "IShowSpeed", "xqc": "xQc", "jynxzi": "Jynxzi", "caseoh_": "CaseOh",
         "adinross": "Adin Ross", "zackrawrr": "Asmongold", "hasanabi": "HasanAbi", "summit1g": "summit1g",
         "sketch": "Sketch", "lacy": "Lacy", "stableronaldo": "Stable Ronaldo", "fanum": "Fanum",
         "dukedennis": "Duke Dennis", "agent00": "Agent00", "tarik": "Tarik", "shroud": "shroud",
         "timthetatman": "TimTheTatman", "nickmercs": "NICKMERCS", "lirik": "LIRIK", "moistcr1tikal": "Moistcr1tikal",
         "sypherpk": "SypherPK", "n3on": "N3on", "rakai": "Rakai", "rayasianboy": "RayAsianBoy", "kelo": "Kelo",
         "sodapoppin": "Sodapoppin", "jasontheween": "JasonTheWeen", "plaqueboymax": "PlaqueBoyMax",
         "darkviperau": "DarkViperAU", "typicalgamer": "TypicalGamer", "ninja": "Ninja", "ludwig": "Ludwig", "valkyrae": "Valkyrae"}
WORD_LOGINS = {"sketch", "lacy", "tarik", "kelo", "ninja", "fanum", "shroud"}

def display_name(m):
    ch = str(m.get("channel") or "").lstrip("@")
    name = m.get("name") if isinstance(m.get("name"), str) and m.get("name").strip() else None
    return NAMES.get(ch.lower()) or name or (ch[:1].upper() + ch[1:] if ch else "This streamer")

def crossover(m, src):
    """The clip's title / quote / transcript names >= 2 star streamers (a crossover: both fan bases care)."""
    text = " ".join(str(m.get(k) or "") for k in ("title", "quote"))
    if m.get("short") and not m.get("remote"):
        tr = Path(m["short"]).parent / "transcript.txt"
        if tr.exists():
            try:
                text += " " + tr.read_text(encoding="utf-8", errors="ignore")[:20000]
            except OSError:
                pass
    hits = set()
    for login in {x.lower() for x in src.get("star_streamers", [])}:
        name = NAMES.get(login, login)
        if login in WORD_LOGINS:                     # 'sketch', 'lacy': only the capitalised name counts
            found = re.search(r"(?<![\w])" + re.escape(name) + r"(?![\w])", text)
        else:
            pats = [re.escape(name), re.escape(login.rstrip("_"))]
            found = re.search(r"(?<![\w])(" + "|".join(pats) + r")(?![\w])", text, re.I)
        if found:
            hits.add(login)
    return len(hits) >= 2

def is_live(m):
    """A clip cut from a live chat explosion (klipuj.py live)."""
    return bool(m.get("live")) or str(m.get("id", "")).startswith("live_")

def is_hot(m, src_cfg=None):
    """Goes out NOW (shareNow): a live chat clip, a world-class streamer's clip (star_streamers, Aleš 18. 11.:
    Kai Cenat / IShowSpeed carry the account), or a viewer clip spreading fast (score >= hot_score),
    both younger than hot_max_age_h (an old one waits for a normal slot). Never a YouTube window."""
    src_cfg = src_cfg if src_cfg is not None else load_json(HERE / "sources.json", {})
    if m.get("platform") == "youtube" or not m.get("created"):
        return False
    if m.get("age_h", 99) > src_cfg.get("hot_max_age_h", 3):
        return False
    return is_live(m) or is_star(m, src_cfg) or (m.get("score") or 0) >= src_cfg.get("hot_score", 2000)

def is_star(m, src_cfg):
    stars = {x.lower() for x in src_cfg.get("star_streamers", [])}
    return str(m.get("channel") or "").lower() in stars and (m.get("score") or 0) >= src_cfg.get("star_min_score", 300)

def star_tier(m, src_cfg):
    """0 = top star (star_tier1: Kai Cenat, IShowSpeed, xQc - Aleš 18. 11.: they go before everyone),
    1 = other star streamer, 2 = not a star."""
    if not is_star(m, src_cfg):
        return 2
    top = {x.lower() for x in src_cfg.get("star_tier1", [])}
    return 0 if str(m.get("channel") or "").lower() in top else 1

def hot_pick(cands, recs, now, limit, src_cfg, day=None, blocked=None):
    """
    Hot clips this run may post NOW, paced so one run (or a burst of runs) never burns the day:
    at most hot_per_run (2) per run, a viewer / star clip only hot_gap_min (30) after the brand's last
    hot post, a live clip only live_gap_min (20) after its last live post - unless it is exceptional
    (score >= 3 x hot_score, or a live chat explosion with spike_z >= hot_bypass_z). Exceptional clips
    first (they may break the day's pace), then star streamers' clips, then live, then by score.
    day = the publishing day: every post must also fit the day-long pace + evening reserve (pace_fits,
    with the burst) unless it is exceptional(); clips held back only by the pace go to `blocked`.
    """
    hs = src_cfg.get("hot_score", 2000)
    hot = [m for m in cands if is_hot(m, src_cfg)]
    hot.sort(key=lambda m: (not exceptional(m, src_cfg), star_tier(m, src_cfg), not is_live(m),
                            -(m.get("score") or 0), -(m.get("spike_z") or 0)))
    dues = quota_dues(day, recs) if day else None
    times, live_times = [], []
    for rec in recs.values():
        if not rec.get("hot"):
            continue
        try:
            t = datetime.fromisoformat(rec.get("due") or "")
        except ValueError:
            continue
        times.append(t)
        if rec.get("live"):
            live_times.append(t)
    gap, lgap = timedelta(minutes=src_cfg.get("hot_gap_min", 30)), timedelta(minutes=src_cfg.get("live_gap_min", 20))
    out = []
    for m in hot:
        if len(out) >= min(limit, src_cfg.get("hot_per_run", 2)):
            break
        live = is_live(m)
        strong = ((m.get("score") or 0) >= 3 * hs or
                  (live and (m.get("spike_z") or 0) >= src_cfg.get("hot_bypass_z", 25)))
        if not strong:
            if live and any(abs(now - t) < lgap for t in live_times):
                continue
            if not live and any(abs(now - t) < gap for t in times):
                continue
        if dues is not None and not exceptional(m, src_cfg) and not pace_fits(dues, now, day, src_cfg, hot=True):
            if blocked is not None:
                blocked.append(m)
            continue
        out.append(m)
        if dues is not None:
            dues.append(now)
        times.append(now)
        if live:
            live_times.append(now)
    return out

# Anti-spam: every post gets its own text (a quote from what the streamer said), no
# @mentions (they would tag strangers / break X's automation rules), few rotating hashtags.
JUNK_RE = re.compile(r"leak|n[i1]gg|f[a@]gg|retard|\bkys\b", re.I)
HASHTAGS = ["#gta", "#gaming", "#twitch", "#gtavi", "#gta6clips", "#vicecity", "#gamingclips", "#streamer"]

def home(m):
    """Credit link without an @ (youtube.com/@x would read as a mention / tag a stranger)."""
    ch = str(m.get("channel") or "").lstrip("@")
    return {"kick": f"kick.com/{ch}", "youtube": f"{ch} on YouTube"}.get(m.get("platform"), f"twitch.tv/{ch}")

def quote_of(m):
    """The streamer's own words for the caption, or "" when they look like junk / risky (JUNK_RE)."""
    q = (m.get("quote") or "").strip().replace("@", "")
    return "" if not q or JUNK_RE.search(q) else q

URL_RE = re.compile(r"(?:https?://)?(?:[a-z0-9-]+\.)+(?:com|tv|net|org|gg|io|co|be)(?:/\S*)?", re.I)
X_MAX = 270                                       # X counts 280 weighted characters; a margin

def x_len(text):
    """X's weighted length: a link counts 23, characters outside Latin / general punctuation (emoji, CJK) 2."""
    n, pos = 0, 0
    for u in URL_RE.finditer(text):
        n += 23
        for ch in text[pos:u.start()]:
            n += _xw(ch)
        pos = u.end()
    return n + sum(_xw(ch) for ch in text[pos:])

def _xw(ch):
    c = ord(ch)
    return 1 if (c <= 4351 or 8192 <= c <= 8205 or 8208 <= c <= 8223 or 8242 <= c <= 8247) else 2

def x_fit(text, limit=X_MAX):
    """Drop whole lines before the last one (credit + #GTA6) until X accepts it, then cut."""
    lines = text.split("\n")
    while x_len("\n".join(lines)) > limit and len(lines) > 2:
        del lines[-2]
    text = "\n".join(lines)
    while x_len(text) > limit:
        text = text[:-1]
    return re.sub(r"\n{3,}", "\n\n", text).strip()

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
FAN_BAIT = [   # 18. 11.: no Heat lines - the Heat badge is off (Aleš), the caption must not refer to it
    "Rate that 1-5 🔥", "How wild was that on a scale of 1 to 5? 🔥", "Too much or just enough? 👇",
    "Did {s} handle that well? Be honest 👇", "One word for that reaction 👇", "Would you watch this twice? 👀",
    "Smooth or messy? 👇", "Did that make you rewind? 👇", "Who saw that coming? 👇",
    "Best moment of the day so far? 👇",
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
    if ZNACKA != "bananas" or not BRAND.get("league") or m.get("platform") in ("top3", "top4"):
        return ""
    try:
        if "rows" not in _league:
            import liga
            _league["rows"] = {str(r["channel"]).lower(): r["rank"] for r in liga.table(7)}
        rank = _league["rows"].get(str(m.get("channel") or "").lower())
    except Exception as e:                        # the league is a bonus: never cost a post
        log(f"  league: {e}")
        return ""
    return f"#{rank} in the Banana League this week" if rank and rank <= 10 else ""

def chat_line(m):
    """Live chat clips: 'Chat went 14× during this' - the measured ratio only, nothing estimated."""
    x = m.get("chat_x")
    return f"Chat went {int(x)}× during this" if isinstance(x, (int, float)) and x >= 3 else ""

# Titles (18. 11., what the big clip channels do): "{Streamer} {what happened} In GTA 6! 😭" - the name and
# the event are what people search. The action comes only from the moment's own tags (transcript + the
# viewer's clip title, klipuj.moment_tags) when the tag is strong (tag_scores >= TITLE_MIN); otherwise a
# neutral line that claims no event. Bananas and fanpage get different phrases / emoji for the same clip.
# SlopMonster lint 5/5.
TITLE_ACTIONS = {
    "crash": ["CRASHES HARD In GTA 6", "Can't Stop CRASHING In GTA 6", "WRECKS Everything In GTA 6"],
    "chase": ["Gets Into A WILD Chase In GTA 6", "Tries To GET AWAY In GTA 6", "Goes ON THE RUN In GTA 6"],
    "funny": ["Can't Stop LAUGHING In GTA 6", "Is CRYING Laughing In GTA 6", "LOSES IT Laughing In GTA 6"],
    "glitch": ["Finds A CRAZY Glitch In GTA 6", "Runs Into A WEIRD Glitch In GTA 6", "BREAKS GTA 6 With A Glitch"],
    "rage": ["RAGES OUT In GTA 6", "Gets SO MAD In GTA 6", "Hits BREAKING POINT In GTA 6"],
    "first-reaction": ["Can't Believe How GTA 6 Looks", "Sees Vice City In GTA 6", "Reacts To GTA 6 Graphics",
                       "Is Stunned By GTA 6", "Gets A First Look At GTA 6", "Can't Get Over GTA 6's Details"],
}
TITLE_NEUTRAL = ["Can't Believe This In GTA 6", "Wasn't Ready For This In GTA 6",
                 "Did Not See This Coming In GTA 6", "Reacts To This GTA 6 Moment",
                 "Gets Caught Off Guard In GTA 6", "Has No Words For This In GTA 6"]
TITLE_MIN = 0.5

def title_action(m):
    """The strongest real tag of the moment as a phrase pool, or the neutral pool."""
    sc = m.get("tag_scores") or {}
    for tag in m.get("tags") or []:
        if tag in TITLE_ACTIONS and (sc.get(tag) or 0) >= TITLE_MIN:
            return TITLE_ACTIONS[tag]
    return TITLE_NEUTRAL

# The viewer's own Twitch clip title says what happened in words people search ("xQc commits Grand Theft
# Auto for the first time in GTA 6") - competitor analysis 18. 11.: the big clip channels title the
# concrete event; our 3-phrase pool gave "... Sees Vice City In GTA 6!" three times on launch day. Used
# only when it reads as a plain English sentence (no junk like "wtf" / "xaxaxa", no slurs, no spoilers).
VIEWER_BAD = re.compile(r"\*|leak|spoil|ending|final mission|dies\b|death|betray|nigg|fag|retard|\bkys\b|"
                        r"cunt|whore|\bsex|nude|porn|\bslur|\bdrop\b|giveaway|sub\b|clip it|"   # plain swears: starred
                        # 19. 11.: "Kai Cenat: Pegging in gta 6 gone wild!" went out - sexual words never in a title
                        r"pegg|blowjob|handjob|\banal\b|threesome|onlyfans|hentai|\bmilf|\bthot|twerk|booty|"
                        r"strip ?club|stripper|hooker|\bdick|\bcock|pussy|boob|\btits?\b|naked|horny|\bcum\b|rimjob",
                        re.I)
VIEWER_GLUE = re.compile(r"\b(the|a|an|to|in|for|of|on|at|with|gets|is|was|first|after|when|his|her|their)\b", re.I)

def viewer_title(m):
    """The Twitch viewer's clip title as a title, or "" (live chat clips / YouTube have none worth using)."""
    if m.get("platform") != "twitch" or is_live(m):
        return ""
    t = re.sub(r"\s+", " ", str(m.get("title") or "")).strip(" .!?-")
    if not t or not re.fullmatch(r"[A-Za-z0-9 ,.'!?&:-]+", t) or VIEWER_BAD.search(t):
        return ""
    if not 4 <= len(t.split()) <= 12 or not VIEWER_GLUE.search(t):
        return ""
    if t.isupper():
        t = re.sub(r"\bGta\b", "GTA", re.sub(r"\bVi\b", "VI", t.title()))
    name, login = display_name(m), str(m.get("channel") or "").lstrip("@")
    lead = re.match(re.escape(login) + r"\b|" + re.escape(name) + r"\b", t, re.I) if login else None
    t = name + t[lead.end():] if lead else t[:1].upper() + t[1:]   # "xqc commits" -> "xQc commits"
    if not re.search(re.escape(name.split()[0]) + (f"|{re.escape(login)}" if login else ""), t, re.I):
        t = f"{name}: {t}"
    if not re.search(r"GTA ?(6|VI)|Grand Theft Auto", t, re.I):
        t += " In GTA 6" if t.istitle() else " in GTA 6"
    return t if len(t) <= 85 else ""

_recent_titles = None
_TITLE_CACHE = {}

def recent_titles(hours=48):
    """Title phrases of both brands' posts of the last `hours` (records' "title"): never the same twice."""
    global _recent_titles
    if _recent_titles is None:
        cut = now_tz() - timedelta(hours=hours)
        _recent_titles = set()
        for f in all_record_files():
            for rec in load_json(f, {}).values():
                try:
                    if isinstance(rec, dict) and rec.get("title") and datetime.fromisoformat(rec.get("due") or "") >= cut:
                        _recent_titles.add(re.sub(r"[^a-z0-9 ]", "", rec["title"].lower()).strip())
                except ValueError:
                    pass
    return _recent_titles

def title_core(m):
    """The title without the emoji: the viewer's title, else the first pool phrase not used in 48 h."""
    vt = viewer_title(m)
    used = recent_titles()
    key = lambda t: re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()
    if vt and key(vt) not in used:
        return vt
    k = int(hashlib.md5(str(m.get("id")).encode()).hexdigest(), 16) + (1 if ZNACKA == "fanpage" else 0)
    name = display_name(m)
    rot_ = lambda xs: xs[k % len(xs):] + xs[:k % len(xs)]
    act = title_action(m)                                # the moment's real action first, neutral only after
    phrases = rot_(act) + ([] if act is TITLE_NEUTRAL else rot_(TITLE_NEUTRAL))
    for ph in phrases:                                   # a phrase nobody used (any streamer) first,
        if not any(u.endswith(key(ph)) for u in used):
            return f"{name} {ph}"
    for ph in phrases:                                   # then one not used for this streamer
        if key(f"{name} {ph}") not in used:
            return f"{name} {ph}"
    return f"{name} {phrases[0]}"

def title_line(m):
    """'Kai Cenat Crashes Hard In GTA 6! 😭' (no hashtags; brands differ in phrase and emoji). Fixed per clip
    within a run (captions of every network and the record share it)."""
    ck = json.dumps([ZNACKA, m.get("id"), m.get("title"), m.get("tags"), m.get("tag_scores")], sort_keys=True,
                    default=str)
    cache = _TITLE_CACHE                                 # per run: one clip = one title on every network
    if ck not in cache:
        k = int(hashlib.md5(str(m.get("id")).encode()).hexdigest(), 16) + (1 if ZNACKA == "fanpage" else 0)
        core = title_core(m)
        cache[ck] = f"{core.rstrip('.!?')}! {['😭', '💀'][k % 2]}".replace("@", "")
        recent_titles().add(re.sub(r"[^a-z0-9 ]", "", core.lower()).strip())
    return cache[ck]

def tag_login(m):
    t = re.sub(r"[^a-z0-9_]", "", str(m.get("channel") or "").lower())
    return f"#{t}" if t else ""

def caption_fanpage(m, service):
    rnd = random.Random(f"{m['id']}:{service}:fanpage")
    s = display_name(m) if m.get("channel") else "they"
    title = title_line(m)                                 # what happened first (searchable), then the quote
    hook = rot(FAN_BAIT, m, service).format(s=s)          # comment bait, a share trigger after
    q = quote_of(m)
    q = f"\"{q}\"" if q else ""
    cta = rot(FAN_SHARE, m, service, 3).format(s=s)
    chat = chat_line(m)
    src = f"Streamer: {home(m)}"
    if service in ("twitter", "threads"):
        text = "\n\n".join(x for x in (title, q, hook, chat, cta, f"{src} #GTA6") if x)
        return text if x_len(text) <= X_MAX else "\n\n".join(x for x in (title, hook, f"{src} #GTA6") if x)
    tags = rnd.sample(FAN_HASHTAGS, 1)
    return "\n\n".join(x for x in (title, q, hook, chat, cta, src,
                                   " ".join(y for y in ["#gta6", tag_login(m), "#gta6clips"] + tags if y)) if x)

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
    """Post text: never an @ anywhere (mentions tag strangers / break X's automation rules); X / Threads
    within X's weighted limit."""
    from cenzura import cenzura, tag_ok              # 18. 11.: swears / sexual words starred ("str** club")
    text = " ".join(w for w in _caption(m, service).replace("@", "").split(" ")
                    if not w.startswith("#") or tag_ok(w))           # a starred hashtag finds nothing: dropped
    text = cenzura(text)
    if service == "tiktok":                          # 18. 11.: 0 views on every TikTok - no outbound links there
        text = re.sub(r"(?:https?://)?(?:www\.)?(twitch\.tv|kick\.com|youtube\.com)/([\w.-]+)",
                      lambda mm: f"{mm.group(2)} on {'Twitch' if 'twitch' in mm.group(1) else 'Kick' if 'kick' in mm.group(1) else 'YouTube'}",
                      text)
    return x_fit(text) if service in ("twitter", "threads") else text

def _caption(m, service):
    if m.get("platform") == "exp":
        return caption_exp(m, service)
    if m.get("platform") == "top4":                  # owner 19. 11.: the daily Top 4 funniest, credit all four
        lines = [f"#{i} {' '.join(w.capitalize() for w in x.get('label', '').split())} - {home(x)}" for i, x in enumerate(m["items"], 1)]
        brand_tag = "#gta6bananaclips" if ZNACKA == "bananas" else "#gta6fanpageclip"
        if service in ("twitter", "threads"):
            return "Top 4 funniest GTA 6 moments today\n\n" + "\n".join(lines) + "\n\n#GTA6"
        return ("Top 4 funniest GTA 6 moments today. Which one got you? 😭\n\n" + "\n".join(lines)
                + f"\n\n#gta6 {brand_tag} #gaming #gta6clips #funnymoments")
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
    s = display_name(m) if m.get("channel") else "they"
    hook = rot(BAIT, m, service).format(s=s)
    q = quote_of(m)
    q = f"“{q}”" if q else ""
    score = (f"🍌 Banana score: {m['rating']}/10" + (" GOLDEN" if m.get("golden") else "")) if m.get("rating") else ""
    if re.search(r"score|bananas|1-10", hook, re.I):  # a guessing question: never spoil the answer in the text
        score = ""
    data = " · ".join(y for y in (score, league_line(m), chat_line(m)) if y)
    cta = rot(SHARE, m, service, 3).format(s=s)
    src = f"🎮 {home(m)}"
    title = title_line(m)                            # 18. 11.: "{Streamer} {what happened} In GTA 6! 😭" first
    if service in ("twitter", "threads"):            # short: X counts 280 characters
        text = "\n\n".join(x for x in (title, q, hook, chat_line(m), cta, f"{src} #GTA6") if x)
        return text if x_len(text) <= X_MAX else "\n\n".join(x for x in (title, hook, f"{src} #GTA6") if x)
    tags = rnd.sample(HASHTAGS, 1)
    return "\n\n".join(x for x in (title, q, hook, data, cta, src,
                                   " ".join(y for y in ["#gta6", tag_login(m), "#gta6bananaclips"] + tags if y)) if x)

def youtube_title(m):
    """
    'GTA 6' and the streamer first (that is what people search), then the clip title -
    viewer titles are often junk ('yoooo') or risky ('LEAK'): then the quote.
    """
    if m.get("platform") == "top3":
        return "Top 3 GTA 6 Moments Today 🍌 #shorts"
    if m.get("platform") == "top4":
        who = ", ".join(dict.fromkeys(display_name(x) for x in reversed(m.get("items") or []) if x.get("channel")))
        t = f"Top 4 Funniest GTA 6 Moments Today | {who}"
        from cenzura import cenzura
        return cenzura(t)[: 100 - len(" #shorts")] + " #shorts"
    if m.get("platform") == "exp":
        items = m.get("items") or [{"channel": "streamers"}]
        t = EXP_TITLES.get(m.get("concept"), "GTA 6 moments").format(s=items[0]["channel"], k=len(items))
        return t[:90] + " #shorts"
    t = title_line(m)                                # 18. 11.: streamer + what happened, like the big clip channels
    t = re.sub(r"\s+", " ", re.sub(r"[^\x00- ]", "", t)).strip()   # no emoji: YouTube dropped it, "6!  #shorts"
    tail = f" | {display_name(m)} GTA VI"            # keywords like the big clip channels ("... | xQc GTA VI")
    if len(t) + len(tail) + len(" #shorts") <= 100:
        t += tail
    from cenzura import cenzura
    t = cenzura(t)
    return t[: 100 - len(" #shorts")] + " #shorts"

def in_day(rec, day):
    """Is the post due inside this publishing day (12:00 that day to 12:00 the next, Prague)?
    A post deleted from Buffer by hand (record "deleted": true) frees its quota and slot again."""
    if rec.get("deleted"):
        return False
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
    n = n if n is not None else (min(ramp.values()) if ramp else 10)
    return max(1, n - top4_reserve())                # owner 19. 11.: fewer normal shorts for the 2 daily Top 4

def month_gb():
    """
    Netlify bandwidth this month, estimated: every published video is fetched by Buffer for each
    of the 6 networks (+1 download from an inbox). Netlify: 20 credits per GB -> 1000 credits = 50 GB.
    """
    start = now_tz().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    mb = 0.0
    files = all_record_files()                       # both accounts (and both writers) share one Netlify site
    for rec in (r for f in files for r in load_json(f, {}).values()):
        try:
            if datetime.fromisoformat(rec.get("due", "")) >= start:
                mb += (rec.get("mb") or 20) * 7
        except ValueError:
            pass
    for rec in load_json(EXTRA_MB, {}).values():     # long-form sources from the inbox (mb already x fetches)
        try:
            if datetime.fromisoformat(rec.get("due", "")) >= start:
                mb += rec.get("mb") or 0
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

def used_today(day, recs=None):
    """Posts of the quota today, both writers (the Top 3 and experiments are extras, never counted)."""
    recs = records() if recs is None else recs
    return sum(in_day(rec, day) for k, rec in recs.items()
               if not k.startswith(("top3_", "top4_", "exp_")) and not rec.get("nocount"))   # nocount: Aleš excluded it

def extra_score(day, src_cfg):
    """Score a clip needs to go out OVER the obligatory quota: the brand's extra_ramp (launch week
    19.-25. 11.: good_score, every good clip up to max_per_day), else sources.json extra_score."""
    n = None
    for d, k in sorted((BRAND.get("extra_ramp") or {}).items()):
        if date.fromisoformat(d) <= day:
            n = k
    return n if n is not None else src_cfg.get("extra_score", 5000)

def remaining(day, recs=None):
    """Today's obligatory quota (ramp) minus what earlier runs already scheduled (hot posts count, Top 3 not)."""
    return max(0, daily_quota(day) - used_today(day, recs))

def max_per_day(day):
    top = BRAND.get("max_per_day") or load_json(HERE / "sources.json", {}).get("max_per_day", 10)
    return max(daily_quota(day), top - top4_reserve())

def remaining_max(day, recs=None):
    """Up to max_per_day (10) when the extra clips are strong enough (Aleš: 8 is a must, 10 if worth it)."""
    return max(0, max_per_day(day) - used_today(day, recs))

# Day-long pacing (Aleš 18. 11.: "don't burn the day's posts in the first minutes when a stream explodes -
# it may explode even more later"). Every post of the brand (hot, star, live, normal) fits a cumulative
# curve over the publishing window: posts due by t <= ceil(max_per_day x audience weight elapsed by t)
# (+ pace_burst for hot / star / live clips - the burst is their headroom above the normal pace), and
# until 22:00 Prague 3 posts of max_per_day stay free (US prime), until 03:00 1 (reserve_until).
# Weights per Prague hour favour US/UK prime (18-04) and the AU morning (08-11), low 05-08.
#   hour:  0  1  2  3    4  5  6    7    8    9    10   11 12 13 14   15   16   17 18   19   20 21 22 23
PACE_WEIGHTS = [3, 3, 3, 2.5, 2, 1, 0.5, 0.5, 1.5, 1.5, 1.5, 1, 1, 1, 1.5, 1.5, 1.5, 2, 2.5, 2.5, 3, 3, 3, 3]
RESERVE_UNTIL = {"22": 3, "03": 1}
EXCEPTIONAL = {"score_x": 4, "star_x": 2, "live_z": 25}

def window_start(day):
    return datetime.combine(day, datetime.min.time(), TZ) + timedelta(hours=12)

def pace_share(day, t, weights):
    """Share of the window's audience weight (12:00 day .. 12:00 next, Prague hours) elapsed by t."""
    hours = (t.astimezone(TZ).replace(tzinfo=None) - window_start(day).replace(tzinfo=None)).total_seconds() / 3600
    acc = 0.0
    for i in range(24):
        acc += weights[(12 + i) % 24] * min(1.0, max(0.0, hours - i))
    return acc / (sum(weights) or 1)

def pace_limit(day, t, src, hot=False):
    """How many of the day's posts may be due by t: the audience curve (+ burst for hot clips),
    the evening reserve, never over max_per_day. pace_weights [] in sources.json = curve off."""
    mx = max_per_day(day)
    w = src.get("pace_weights", PACE_WEIGHTS)
    lim = mx
    if w and len(w) == 24 and sum(w) > 0:
        lim = math.ceil(mx * pace_share(day, t, w) - 1e-9) + (src.get("pace_burst", 2) if hot else 0)
    for hh, keep in (src.get("reserve_until", RESERVE_UNTIL) or {}).items():
        h = int(hh)
        end = datetime.combine(day + timedelta(days=0 if h >= 12 else 1), datetime.min.time(), TZ) + timedelta(hours=h)
        if t < end:
            lim = min(lim, mx - keep)
    return max(0, min(mx, lim))

def quota_dues(day, recs):
    """Due times of the day's quota posts (Top 3 and experiments are extras, not counted)."""
    out = []
    for k, rec in recs.items():
        if not k.startswith(("top3_", "top4_", "exp_")) and not rec.get("nocount") and in_day(rec, day):
            out.append(datetime.fromisoformat(rec["due"]))
    return out

def pace_fits(dues, u, day, src, hot=False):
    """Does one more post due at u keep every point from u on under the curve?"""
    pts = sorted(dues + [u])
    for i, v in enumerate(pts, 1):
        if v >= u and i > pace_limit(day, v, src, hot):
            return False
    return True

def upgrade_note(blocked, recs, now, src):
    """A hot clip held back by the pace / the day's budget while a much weaker normal post (upgrade_x,
    3 x) still waits in Buffer (scheduled, due > now): only logged. Deleting a Buffer post needs Aleš's
    approval - nothing is deleted automatically; both go out only when the budget allows."""
    x = src.get("upgrade_x", 3)
    waiting = []
    for k, rec in recs.items():
        try:
            due = datetime.fromisoformat(rec.get("due") or "")
        except ValueError:
            continue
        if not k.startswith(("top3_", "top4_", "exp_")) and not rec.get("hot") and due > now and rec.get("score"):
            waiting.append((rec["score"], due, k))
    notes = []
    for m in sorted(blocked, key=lambda m: -(m.get("score") or 0))[:3]:
        weak = [w for w in waiting if w[0] * x <= (m.get("score") or 0)]
        if weak:
            s, due, k = min(weak)
            notes.append(f"UPGRADE? hot {m['id']} (score {m.get('score', 0):.0f}) held back by the pace, "
                         f"{x}x stronger than the scheduled {k} (score {s:.0f}, {due:%H:%M}) - Buffer post kept, "
                         f"deleting it needs Ales's approval")
    for n in notes:
        log(n)
        print(f"::notice::{n}")
    return notes

# Aleš 18. 11. odpoledne: "nezábavný obsah bez akce nemá šanci být virální" - a clip goes out only with
# real action in it: a strong action tag (klipuj.moment_tags: crash / chase / glitch / funny / rage from
# the transcript, the viewer's clip title, sound and picture motion) that beats plain "looking at GTA 6"
# (first-reaction). Exceptions only by numbers (exceptional(): 4x hot_score, star 2x, live z >= live_z;
# or a cluster of max(10, viewers/5000) viewer clips) and the render's own viral non-gameplay cut (person).
def live_z_min(m, src):
    """The chat-explosion bar of this clip's streamer (live_spike_z_by, else live_spike_z)."""
    return (src.get("live_spike_z_by") or {}).get(str(m.get("channel") or "").lower(), src.get("live_spike_z", 0))

def outburst(m, src):
    """Owner 18. 11.: IShowSpeed's outbursts (screaming, barking, jumping) are always extra viral: a streamer in
    outburst_streamers with a loud payoff (moment_tags _loud: 95th pct of loudness / median) or a reaction tag."""
    if str(m.get("channel") or "").lower() not in {x.lower() for x in src.get("outburst_streamers", [])}:
        return False
    sc = m.get("tag_scores") or {}
    return (sc.get("_loud") or 0) >= src.get("outburst_loud", 5.0)    # a real outburst is loud, not just a word

ACTION_TAGS = ("crash", "chase", "glitch", "funny", "rage")
ACTION_MIN = 0.5

def action_gate(m, src):
    """'' = may go out, else the reason it may not."""
    if m.get("platform") in ("top3", "top4", "exp") or m.get("layout") == "person":
        return ""
    if exceptional(m, src) or (m.get("cluster") or 1) >= max(10, (m.get("viewers") or 0) / 5000) or outburst(m, src):
        return ""
    top = {x.lower() for x in src.get("star_tier1", [])}
    if not is_live(m) and (m.get("score") or 0) < src.get("min_viral", 600)             and str(m.get("channel") or "").lower() not in top:
        return f"not viral enough (score {m.get('score') or 0:.0f} < {src.get('min_viral', 600)})"
    sc = m.get("tag_scores") or {}
    act = max((sc.get(t) or 0) for t in ACTION_TAGS)
    amin = src.get("action_min", ACTION_MIN)
    if act < amin:
        return f"no action: best action tag {act:.2f} < {amin}"
    if (sc.get("first-reaction") or 0) > act + 0.15:
        return f"mostly a first look at GTA 6 (first-reaction {sc.get('first-reaction'):.2f} > action {act:.2f})"
    return ""

def repeat_moment(m, recs, days=3):
    """19. 11. (YouTube Studio: "do not upload the same clips again"): a moment either account already posted in
    the last `days` - another viewer's clip of it (same VOD < 30 s apart, or same channel created < 30 s apart)
    or another window of the same YouTube video (< 90 s apart). '' = new."""
    def yt(i):
        mm = re.match(r"youtube_(.+)_(\d+)$", str(i))
        return (mm.group(1), int(mm.group(2))) if mm else None
    cut = time.time() - days * 86400
    me_yt = yt(m.get("id"))
    for k, r in recs.items():
        if not isinstance(r, dict) or k == m.get("id"):
            continue
        try:
            if datetime.fromisoformat(r.get("due") or "").timestamp() < cut:
                continue
        except ValueError:
            continue
        if m.get("video_id") and r.get("video_id") == m.get("video_id") and m.get("vod_offset") is not None                 and r.get("vod_offset") is not None and abs(r["vod_offset"] - m["vod_offset"]) < 30:
            return f"same moment as {k} (another viewer's clip)"
        if m.get("created") and r.get("created") and r.get("channel") == m.get("channel")                 and abs(r["created"] - m["created"]) < 30:
            return f"same moment as {k}"
        other = yt(k)
        if me_yt and other and me_yt[0] == other[0] and abs(me_yt[1] - other[1]) < 90:
            return f"same YouTube moment as {k}"
    return ""

def service_recent(recs, chans, service, due, gap_min):
    """Did this brand post to `service` less than gap_min minutes before `due` (any record, any path)?"""
    ids = {c["id"] for c in chans if c.get("service", "").lower() == service}
    for r in recs.values():
        if not isinstance(r, dict) or r.get("deleted") or not ids & set(r.get("posts") or {}):
            continue
        try:
            t = datetime.fromisoformat(r.get("due") or "")
        except ValueError:
            continue
        if abs((due - t).total_seconds()) < gap_min * 60:
            return True
    return False

def strong_clip(m, src):
    """TikTok / YouTube (0 views on every post, 18. 11.): only a top streamer's clip or a clip over hot_score."""
    top = {x.lower() for x in src.get("star_tier1", [])}
    return (str(m.get("channel") or "").lower() in top or (m.get("score") or 0) >= src.get("hot_score", 2000) or exceptional(m, src)
            or m.get("platform") in ("top3", "top4"))

def exceptional(m, src):
    """A clip big enough to break the pace and the evening reserve (never max_per_day): score >= 4 x
    hot_score, a star streamer's clip >= 2 x hot_score, a live chat explosion with spike_z >= 25."""
    hs, ex = src.get("hot_score", 2000), {**EXCEPTIONAL, **(src.get("exceptional") or {})}
    s = m.get("score") or 0
    return (s >= ex["score_x"] * hs or (is_star(m, src) and s >= ex["star_x"] * hs)
            or (is_live(m) and (m.get("spike_z") or 0) >= ex["live_z"]))

def day_plan(day, now, recs=None, n_hot=0):
    """
    How much THIS run commits (shared by `den` and `zbyva`, so render and publishing agree):
      cap    posts the day still allows (max_per_day; over the Netlify soft budget only the minimum) - hot
             posts may use all of it; slots only up to max(minimum, max_per_day - hot_room)
      free   base slots still ahead (due > now + 20 min for the deploy and Buffer's fetch), not taken
      near   free slots due within plan_horizon_h - the only ones a normal run fills
      owed   catch-up: posts that must go out now because the free slots after the horizon
             (minus plan_spare_slots, a run may be skipped) can no longer reach the minimum
      slots  how many slots this run fills when the clips are good enough (owed ones in any case)
    n_hot = hot posts this run makes first (no slot; they count toward the minimum and the cap).
    """
    src = load_json(HERE / "sources.json", {})
    recs = records() if recs is None else recs
    used = used_today(day, recs) + n_hot
    must = daily_quota(day)
    level = budget_level()
    top = must if level else max_per_day(day)
    cap = max(0, top - used)
    # slots never take the last hot_room posts of max_per_day while the US prime time is ahead: they stay
    # for hot / live clips found later (21:00 US launch moment); never below the minimum. From
    # hot_room_until (20:00 Prague) of the publishing day the room goes to normal slots too (only clips
    # >= extra_score fill it, `den`), so a strong 9th / 10th clip is not stuck waiting for a hot one
    room_end = datetime.combine(day, datetime.min.time(), TZ) + timedelta(hours=src.get("hot_room_until_h", 20))
    room = src.get("hot_room", 2) if now < room_end else 0
    slot_cap = max(0, max(must, top - room) - used)
    taken = {rec.get("slot") or rec.get("due") for rec in recs.values() if not rec.get("deleted")}
    free = [t for t in slot_times(day, len(SLOTS))
            if jittered(t, day) > now + timedelta(minutes=20) and t.isoformat() not in taken]
    horizon = now + timedelta(hours=src.get("plan_horizon_h", 3))
    near = [t for t in free if jittered(t, day) <= horizon]
    later = len(free) - len(near)
    # 19. 11. (0 posts 12:00-15:15 while 70 clips waited): the morning slots (08-11 h) are spare room for
    # extras, not capacity for the minimum - counted as 'later' they kept the catch-up at 0 all afternoon
    # and only clips >= good_score were rendered. The minimum is owed against the quota's own slots.
    quota_slots = {t.isoformat() for t in slot_times(day, max(1, must))}
    later_q = len([t for t in free if t not in near and t.isoformat() in quota_slots])
    owed = max(0, max(0, must - used) - max(0, later_q - src.get("plan_spare_slots", 1)))
    owed = min(owed, len(near) + 1)                  # a late day catches up over the next runs, never 4 at once
    owed = min(owed, len(free), slot_cap)
    # pacing: a near slot only when its post stays under the normal curve (no burst) and the evening
    # reserve; this run's hot posts (n_hot, due ~now) count first. Catch-up for the minimum beats the
    # pace (the minimum must be reached by the end of the window), never max_per_day.
    dues = quota_dues(day, recs) + [now] * n_hot
    paced = []
    for t in near:
        if len(paced) >= slot_cap:
            break
        u = jittered(t, day)
        if pace_fits(dues, u, day, src):
            paced.append(t)
            dues.append(u)
    use = paced + [t for t in free if t not in paced][: max(0, owed - len(paced))]
    use = sorted(use)[:slot_cap]
    return {"used": used, "must": must, "cap": cap, "level": level, "free": free, "near": near,
            "owed": owed, "slots": len(use), "use": use, "paced": len(paced), "later": later}

def post_input(ch, m, video_url, due, draft):
    service = ch["service"].lower()
    inp = {
        "text": caption(m, service),
        "channelId": ch["id"],
        "schedulingType": Enum("automatic"),
        "mode": Enum("addToQueue" if draft else "shareNow" if m.get("now") else "customScheduled"),
        "assets": [{"video": {"url": video_url}}],
    }
    if draft:
        inp["saveToDraft"] = True
    elif not m.get("now"):                            # hot path (shareNow) needs neither dueAt nor a draft flag:
        inp["dueAt"] = due.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")   # saveToDraft made it a draft
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

def day_service_counts(done, chans, day):
    """{service: posts already made this publishing day} over every record (classics, hot, Top 3,
    experiments) - the X / Threads caps are per day, and a day has many runs."""
    svc = {c["id"]: c["service"].lower() for c in chans}
    used = {}
    for rec in done.values():
        if in_day(rec, day):
            for cid in rec.get("posts") or {}:
                s = svc.get(cid)
                used[s] = used.get(s, 0) + 1
    return used

def create_post(inp, key):
    if lock_lost():                  # every publishing path (classics, hot, Top 3, experiments) stops here
        raise RuntimeError("publishing lock lost (zamek_ztracen) - not posted")
    q = ("mutation { createPost(input: " + gql_value(inp) + ") { __typename "
         "... on PostActionSuccess { post { id } } ... on MutationError { message } } }")
    res = gql(q, key)["createPost"] or {}
    if res.get("message"):
        raise RuntimeError(f"{res.get('__typename') or 'MutationError'}: {res['message']}")
    if not (res.get("post") or {}).get("id"):
        raise RuntimeError(f"Buffer createPost: unexpected answer {res.get('__typename') or res}")
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

def off_grid(t):
    """Never on the full / half hour."""
    return t + timedelta(minutes=1) if t.minute in (0, 30) else t

def fresh_due(t, rnd=None):
    """A scheduled time that is (nearly) past by now - a slow deploy, a Buffer wait - becomes
    now + 3..5 min (next full minute, never :00 / :30) instead of posting into the past."""
    now = now_tz()
    if t > now + timedelta(minutes=2):
        return t
    t = (now + timedelta(minutes=(rnd or random).randint(3, 5) + 1)).replace(second=0, microsecond=0)
    return off_grid(t)

def hot_stagger(clip_id, service, now):
    """Hot post: the networks 0-4 min apart (not all six at the same second). (shareNow?, due):
    offset 0 = shareNow, otherwise customScheduled at the next full minute + offset, never :00 / :30."""
    order = list(NETS)
    random.Random(f"{clip_id}:hot").shuffle(order)            # a different network leads every time
    off = [0, 1, 2, 2, 3, 4][order.index(service)] if service in order else 0
    if off == 0:
        return True, now
    return False, off_grid((now + timedelta(minutes=off + 1)).replace(second=0, microsecond=0))

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
    global TAKE_LONG, PLAN_DAY, FAST
    if load_json(HERE / "sources.json", {}).get("publish_pause") and not a.zkouska:
        log("publishing paused (sources.json publish_pause) - nothing posted this run")
        return
    FAST = bool(getattr(a, "rychle", False))
    TAKE_LONG = not a.zkouska and not FAST       # a dry run writes nothing; the fast path only its record file
    now = now_tz()
    # --den dnes = the publishing day we are in NOW (12:00-11:59): after midnight that is still
    # yesterday's day (its late slots, its quota) - the hot path checks the current window
    day = pub_day(now) if a.den == "dnes" else date.fromisoformat(a.den) if a.den else now.date() + timedelta(days=1)
    PLAN_DAY = day
    level = budget_level()
    if level >= 2 and ZNACKA == "fanpage":   # Netlify hard limit: the second account waits for next month
        print(f"::warning::Netlify: ~{month_gb():.0f} GB this month, {BRAND.get('name', ZNACKA)} paused")
        return
    if level:                                # Netlify < 1000 credits/month: extras pause, the minimum always goes out
        print(f"::warning::Netlify: ~{month_gb():.0f} GB this month, extra videos and Top 3 paused")
    recs = records()                         # both writers (normal runs + live fast path)
    src_cfg = load_json(HERE / "sources.json", {})
    good, extra = src_cfg.get("good_score", 1000), extra_score(day, src_cfg)
    cands = candidates()

    # 1) hot path: live chat clips and clips spreading fast go out NOW on every network, whenever the
    # day has quota left up to max_per_day (they may run ahead of the planned pace)
    p0 = day_plan(day, now, recs)
    hot_due = (now + timedelta(minutes=2)).replace(second=0, microsecond=0)   # the deploy below takes ~1 min
    hot, blocked = [], []
    if in_day({"due": hot_due.isoformat()}, day):
        hot = hot_pick(cands, recs, hot_due, min(a.n, p0["cap"]), src_cfg, day=day, blocked=blocked)
        if p0["cap"] <= 0:
            blocked = [m for m in cands if is_hot(m, src_cfg)]
        upgrade_note(blocked, recs, now, src_cfg)
    for m in hot:
        m["hot"] = True
        log(f"HOT{' (live)' if is_live(m) else ''}: {m['channel']} '{m.get('title', '')[:40]}' -> {hot_due:%H:%M}")

    # 2) just in time: only the slots due within the horizon (+ catch-up for the minimum), the best
    # clips available right now; the fast path never fills slots
    p = day_plan(day, now, recs, n_hot=len(hot))
    picks = []
    if not FAST:
        n = min(p["slots"], a.n - len(hot))
        pre = src_cfg.get("youtube_prelaunch") or {}
        yt_cap = (pre.get("per_day", 3) if pre and day < date.fromisoformat(pre.get("until", "2000-01-01"))
                  else src_cfg.get("youtube_per_day", 1))
        yt_left = yt_cap - sum(k.startswith("youtube_") and in_day(rec, day) for k, rec in recs.items())
        hot_ids = {m["id"] for m in hot}
        for m in cands:                          # best first
            if len(picks) >= n:
                break
            if m["id"] in hot_ids or (m.get("platform") == "youtube" and yt_left <= 0):
                continue
            # Aleš: do not burn the minimum on average clips - an average one only as catch-up (owed);
            # over the minimum only extra_score (launch week extra_ramp: good_score) up to max_per_day
            k = p["used"] + len(picks)
            if len(picks) >= p["owed"] and m["score"] < (good if k < p["must"] else extra):
                continue
            if m.get("platform") == "youtube":
                yt_left -= 1
            picks.append(m)
    slots = [None] * len(hot) + p["use"][: len(picks)]         # paced near slots (+ catch-up), earliest first
    picks = hot + picks
    times = [hot_due if s is None else jittered(s, day) for s in slots]
    chans = load_json(CHANNELS, [])
    seq0 = len(recs)                         # caption rotation: the brand's post counter (no line twice in a row)
    for i, m in enumerate(picks):
        m["_seq"] = seq0 + i
    mode = "ZKOUŠKA" if a.zkouska else ("NAOSTRO" if a.ostre else "koncepty")
    log(f"{day} [{ZNACKA}{' fast' if FAST else ''}]: {len(picks)} shorts ({len(hot)} hot) - day {p0['used']}/{p0['must']}"
        f" (max {p0['used'] + p0['cap']}), {len(p['near'])} free slots in the next {src_cfg.get('plan_horizon_h', 3)} h,"
        f" catch-up {p['owed']}, {p['later']} later, pace now {len(quota_dues(day, recs))}/{pace_limit(day, now, src_cfg)}"
        f" (+burst {pace_limit(day, now, src_cfg, hot=True)}); over the minimum only score >= {extra}"
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
        if not FAST:
            exp_post(a, day, now)                    # experiment of the day: plan only
        return
    if not picks:
        if not FAST and load_json(CHANNELS, []):
            top3_only(a, day, now, final=p["later"] == 0)   # the day's last stretch: the Top 3 may be due
            exp_post(a, day, now)                    # experiment: own slot, outside the quota
        if load_json(CHANNELS, []):
            top4_pool_collect(a, day, now)           # unposted good clips feed the Top 4 too
            top4_post(a, day, now)                   # the daily Top 4 funniest (owner 19. 11.), at its own times
        return
    load_env()
    need("NETLIFY_AUTH_TOKEN", "NETLIFY_SITE_ID", "MEDIA_BASE_URL")
    if not brand_keys() or not chans:
        sys.exit(f"no Buffer keys or channels: fill .env and run `publikuj.py kanaly --znacka {ZNACKA}` first")

    # 1) one deploy of this run's videos as a Netlify DRAFT deploy: 0 credits (a production
    # deploy is 15), and its permalink stays forever, so earlier videos need no carry-over
    stage = OUT / "_media"
    stage.mkdir(parents=True, exist_ok=True)
    files = {}
    plan = []
    slot_of = {}
    for m, due, slot in zip(picks, times, slots):
        slot_of[m["id"]] = slot.isoformat() if slot else None
        rel = f"{day:%Y%m%d}/{safe_name(m['id'])}.mp4"
        src = Path(m["short"]) if not m.get("remote") else stage / f"inbox_{safe_name(m['id'])}.mp4"
        if m.get("remote"):                          # YouTube / live short from an inbox
            try:
                src.write_bytes(http(m["short"], timeout=300)[1])
            except Exception as e:
                log(f"  inbox short unavailable, skipped: {m['short']} ({e})")
                add_fail(m["id"])
                continue
        too_long = short_too_long(src, m)
        if too_long:                                 # 18. 11.: 34-42 s shorts from the old plain cut went out
            log(f"  {too_long}, skipped")
            add_fail(m["id"])
            continue
        files[rel] = src
        if m.get("remote") and m.get(f"payoff_{ZNACKA}"):      # YouTube / live inbox: already uploaded by the PC
            m["_payoff_url"] = f"{m['base']}/{m[f'payoff_{ZNACKA}']}"
        po = (m.get("payoffs") or {}).get(ZNACKA)           # the clean payoff for the daily Top 4 (19. 11.)
        if po and not m.get("remote") and Path(po).exists():
            m["_payoff_rel"] = f"{day:%Y%m%d}/{safe_name(m['id'])}_payoff.mp4"
            files[m["_payoff_rel"]] = Path(po)
        plan.append((m, due, rel))
    # the daily Top 3 (our own curated format): in the day's last stretch, once the minimum is met
    try:
        top3 = None if FAST else make_top3(day, recs, plan, stage, final=p["later"] == 0)
    except Exception as e:                       # the Top 3 never costs the classics
        log(f"top3: {str(e)[-200:]}")
        top3 = None
    if top3:
        plan.append(top3)
        files[top3[2]] = top3[0].pop("file")
    if not files:
        print("::warning::nothing to deploy (every short unavailable)")
        return
    try:
        base = inbox_deploy(files, b"[]", MEDIA_TITLE, essential=True)
    except Exception as e:                       # Netlify limit / outage: nothing can go out this run
        print(f"::error::Netlify deploy failed, nothing posted this run: {str(e)[-200:]}")
        sys.exit(1)
    prune_drafts()

    # 2) Buffer posts, one time slot per short, every channel (X/Threads capped per day: the cap
    # counts every post of this publishing day, both writers, not only this run's)
    caps = src_cfg.get("caps") or {}
    used, ok, failed = day_service_counts(recs, chans, day), 0, 0
    target = target_file()
    for m, due, rel in plan:
        url = f"{base}/{rel}"
        if lock_lost():                                    # another run may hold the lock now: stop
            print("::error::zamek publikace ztracen - zbytek postu tohoto behu se neposle")
            failed += 1
            break
        log(f"{due:%H:%M} {m['platform']}/{m['channel']} '{m.get('title', '')[:50]}' -> {url}")
        posts, limited = {}, False
        try:                                               # one clip's trouble never stops the others
            if not is_public(url):
                log("  not reachable after deploy, skipped")
                failed += 1
                add_fail(m["id"])
                continue
            for i, ch in enumerate(chans):
                service = ch["service"].lower()
                if used.get(service, 0) >= caps.get(service, 99):
                    continue
                if service in (src_cfg.get("strong_only") or ["youtube"]) and not strong_clip(m, src_cfg):
                    continue                               # 18. 11.: 0 views on YouTube - only the strongest there
                gap = (src_cfg.get("net_gap_min") or {}).get(service)
                if gap and service_recent(recs, chans, service, due, gap):
                    continue                               # 19. 11.: TikTok every clip, but spaced (no spam look)
                win = (src_cfg.get("net_hours") or {}).get(service)
                if win and not (win[0] <= due.hour < win[1]):
                    continue                               # 19. 11. (YouTube Studio advice): 1 short a day when viewers are on
                if m.get("hot"):                           # hot: NOW, the networks 0-4 min apart
                    m["now"], ch_due = hot_stagger(m["id"], service, now_tz())
                else:                                      # Aleš 17. 11.: each network a few minutes apart
                    m["now"] = False
                    ch_due = fresh_due(net_due(due, day, service))
                try:
                    posts[ch["id"]] = create_post(post_input(ch, m, url, ch_due, not a.ostre), ch.get("key", "BUFFER_API_KEY"))
                    used[service] = used.get(service, 0) + 1
                    ok += 1
                    log(f"  {service}: ok")
                except RateLimited as e:                   # Buffer wants > 2 min: skip the rest of this clip now
                    failed += 1
                    limited = True
                    log(f"  {service}: {e}")
                    break
                except Exception as e:
                    failed += 1
                    log(f"  {service}: FAILED {e}")
        except Exception as e:
            failed += 1
            log(f"  FAILED {str(e)[-200:]}")
        if not posts and not limited:                      # a rate limit is Buffer's problem, not the clip's
            add_fail(m["id"])
        size = files.get(rel)
        mb = round(Path(size).stat().st_size / 1e6, 1) if size and Path(size).exists() else None
        rec = {"url": url, "path": rel, "due": due.isoformat(), "draft": not a.ostre, "posts": posts, "mb": mb,
               "channel": m.get("channel"), "platform": m.get("platform"), "score": round(m.get("score") or 0, 1),
               "hot": bool(m.get("hot")), "rating": m.get("rating"), "views": m.get("views"),   # Banana League (liga.py)
               "golden": bool(m.get("golden")), "title": title_line(m),
               "video_id": m.get("video_id"), "vod_offset": m.get("vod_offset"), "created": m.get("created"),
               "payoff": f"{base}/{m['_payoff_rel']}" if m.get("_payoff_rel") else m.get("_payoff_url"),
               "funny": (m.get("tag_scores") or {}).get("funny"), "tags": m.get("tags"),
               "quote": (m.get("quote") or "")[:160]}
        if is_live(m):                                     # live: the stream's audience is not clip views
            rec.update(live=True, viewers=m.get("viewers"), spike_z=m.get("spike_z"))
        if slot_of.get(m["id"]):
            rec["slot"] = slot_of[m["id"]]
        if not posts:                                      # nothing went out: no record, the next run retries
            continue
        own = load_json(target, {})
        own[m["id"]] = rec
        save_json(target, own)
        recs[m["id"]] = rec
        push_records(target)                               # on origin before the next clip (under the lock)
    if not FAST:
        exp_post(a, day, now)                        # experiment: own slot, outside the quota, after the classics
    top4_pool_collect(a, day, now)                   # unposted good clips feed the Top 4 too
    top4_post(a, day, now)                           # the daily Top 4 funniest (owner 19. 11.), at its own times
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
        done = records()                               # both writers (X/Threads cap, slots)
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
        used = day_service_counts(done, chans, day)    # X / Threads: the day's cap counts every post
        m["_seq"] = len(done)
        posts = {}
        for ch in chans:
            service = ch["service"].lower()
            if used.get(service, 0) >= caps.get(service, 99):
                log(f"  exp {service}: daily cap reached, skipped")
                continue
            try:
                posts[ch["id"]] = create_post(post_input(ch, m, url, fresh_due(net_due(due, day, service)), not a.ostre),
                                              ch.get("key", "BUFFER_API_KEY"))
                log(f"  exp {service}: ok")
            except Exception as e:
                log(f"  exp {service}: FAILED {e}")
        if not posts:                                  # nothing went out: stays pending, the next run retries
            print("::warning::exp: no Buffer post succeeded")
            return
        done = load_json(PUBLISHED, {})               # experiments: normal runs only -> the main file
        done[m["id"]] = {"url": url, "path": rel, "due": due.isoformat(), "slot": base.isoformat(), "draft": not a.ostre,
                         "posts": posts, "mb": round(Path(m["file"]).stat().st_size / 1e6, 1), "channel": "exp",
                         "platform": "exp", "concept": m["concept"], "items": [x["id"] for x in m["items"]]}
        save_json(PUBLISHED, done)
        push_records(PUBLISHED)
    except Exception as e:                            # an experiment never costs a classic post
        print(f"::warning::exp: {str(e)[-200:]}")

# -----------------------------------------------------------------------------
# Top 4 funniest (owner 19. 11.): twice a day per channel, from the channel's OWN posted shorts (the two
# accounts never share a clip). #4 -> #1, least funny first; each entry is the short's clean payoff
# (klipuj.payoff_clip: a moment before the punchline, the punchline, a bit after), hard cut to the next.
# A ranking list fills in on the game band: "4. <a few funny words>" appears the moment its clip starts and
# stays, then 3, 2, 1. No banana score card, no outro: only the brand's Follow card at the end.

TOP4_N = 4
TOP4_COL = {1: "00D7FF", 2: "E0C8B4", 3: "3F8FD9", 4: "FFFFFF"}   # owner 19. 11.: medal colours, ASS &HBBGGRR
                                                                  # (gold #FFD700, silver #B4C8E0, bronze #D98F3F, white)
TOP4_FALLBACK = {"funny": ["CAN'T STOP LAUGHING", "HE LOST IT", "CHAT IS CRYING"],
                 "chase": ["COPS ON HIM", "COPS CAMPING", "FULL SEND ESCAPE"],
                 "crash": ["TOTAL WRECK", "WORST DRIVER EVER", "THAT CRASH THOUGH"],
                 "rage": ["RAGE MODE", "PURE RAGE", "CONTROLLER IN DANGER"],
                 "glitch": ["GTA BROKE", "PHYSICS LEFT", "WHAT WAS THAT"],
                 "": ["NO WAY", "DIABOLICAL", "PURE CHAOS", "ABSOLUTE CINEMA", "NOT AGAIN"]}
TOP4_SYSTEM = ("You are the funniest editor of a GTA 6 streamer clip page. You write the label of one entry in a "
               "'funniest moments' ranking on a vertical video: 2 to 4 words in capitals that name what happens in "
               "THIS clip with a joke or a twist, the way a friend would sum it up. Good: 'PARKING LIKE A PRO' "
               "(crashed while parking), 'COPS SAID NOT TODAY', 'THAT CAR HAD FAMILY', 'GRAVITY SAID NO'. Bad: generic "
               "words that fit any clip ('FUNNY MOMENT', 'NO WAY', 'CHAOS'), anything you cannot see or hear in the "
               "clip.")
TOP4_STOP = {"THE", "AND", "THAT", "THIS", "WITH", "FROM", "INTO", "WHAT", "HAVE", "JUST", "LIKE", "WHEN", "THEY",
             "YOUR", "GTA", "MOMENT", "FUNNY", "CLIP", "STREAMER", "GAME"}

def top4_reserve():
    """Normal shorts give way to the Top 4 (owner 19. 11.): one fewer per Top 4 slot of the day."""
    return len(BRAND.get("rank_times") or []) if BRAND.get("rank4") else 0

def top4_slots(day):
    """The brand's Top 4 times of the publishing day (brands.json rank_times, Prague), never on :00 / :30."""
    out = []
    for hhmm in BRAND.get("rank_times") or []:
        t = datetime.strptime(hhmm, "%H:%M").time()
        out.append(off_grid(datetime.combine(day + timedelta(days=1 if t.hour < 12 else 0), t, TZ)))
    return out

def top4_grid(mp4):
    """3 frames of the payoff (start, middle, end) side by side for the vision model -> jpg path or None."""
    out = Path(mp4).with_suffix(".grid.jpg")
    r = subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(mp4), "-vf",
                        "fps=3/5,scale=360:-1,tile=3x1", "-frames:v", "1", "-q:v", "3", str(out)],
                       capture_output=True, text=True, timeout=120)
    return out if not r.returncode and out.exists() else None

def top4_grounded(lab, facts):
    """Does the label belong to the clip? A content word of it (or its first 4 letters) is in the facts."""
    words = [w for w in re.findall(r"[A-Z]+", lab) if len(w) >= 3 and w not in TOP4_STOP]
    f = facts.upper()
    return any(w in f or (len(w) >= 5 and w[:4] in f) for w in words)

def top4_label(rec, mp4=None, taken=()):
    """2-4 funny words for the list (owner 19. 11.: 'vtipnych par slov', 'musi davat smysl, byt vtipny a souviset
    s klipem'): the facts of THIS clip (what the vision model sees in its frames, the streamer's own words, the
    clip title) -> the bigger text model writes the label -> kept only when it is short, clean, not repeated in
    this video and grounded in those facts (a content word of it is in them); else a template from its tags."""
    from cenzura import cenzura
    facts = []
    try:
        import nahled_ai
        nahled_ai.load_env()
        grid = top4_grid(mp4) if mp4 else None
        scene = nahled_ai.clip_scene(grid) if grid else None
        if grid:
            grid.unlink(missing_ok=True)
        if scene:
            facts.append(f"What you see: {scene}")
        if quote_of(rec):
            facts.append(f"What the streamer says: \"{quote_of(rec)[:160]}\"")
        if rec.get("title"):
            facts.append(f"Clip title: {rec['title']}")
        if rec.get("tags"):
            facts.append(f"Moment type: {', '.join(rec['tags'])}")
        if not (scene or quote_of(rec)):
            log("top4: nothing known about the clip, template label")
        else:
            ask = ("\n".join(facts) + "\n\nWrite the label: 2 to 4 words, capitals, funny, about exactly this clip. "
                   "No emoji, no quotes, no swear words, no streamer name."
                   + (f" Must differ from: {', '.join(taken)}." if taken else "") + " Reply with the label only.")
            for temp in (0.7, 0.4):
                ans = nahled_ai.ask_text(TOP4_SYSTEM, ask, max_tokens=16, temperature=temp, model=nahled_ai.TEXT_BIG)
                lab = re.sub(r"\s+", " ", ans.strip().splitlines()[0] if ans and ans.strip() else "").strip(" \"'.!").upper()
                ok = (re.fullmatch(r"[A-Z0-9' ?!-]{3,24}", lab) and 2 <= len(lab.split()) <= 4 and cenzura(lab) == lab
                      and display_name(rec).upper() not in lab and lab not in taken)
                if ok and top4_grounded(lab, " ".join(facts)):
                    return lab
                log(f"top4: label '{lab}' rejected ({'not about the clip' if ok else 'form'})")
    except Exception as e:
        log(f"top4: label (AI) failed ({str(e)[-80:]})")
    pool = [x for t in (rec.get("tags") or []) if t in TOP4_FALLBACK for x in TOP4_FALLBACK[t]] + TOP4_FALLBACK[""]
    return next((x for x in pool if x not in taken), "NO WAY")      # never the same label twice in one video

def top4_owner(key, brands):
    """A clip published on BOTH accounts goes into the Top 4 of one of them only (owner 19. 11.: the channels
    never take each other's clips): a fixed draw from the clip id."""
    return sorted(brands)[int(hashlib.md5(key.encode()).hexdigest(), 16) % len(brands)]

def top4_pick(recs, now, others=None):
    """The brand's best clips (top4_strength) of the last TOP4_WINDOW_H h with a clean payoff, none in an earlier
    Top 4 of either account and none that belongs to the other account's Top 4 (top4_owner), best first: [(key, rec)] (top4_post takes
    the first 4 that pass the nudity check). others = {brand: its records} of the other accounts."""
    others = others or {}
    used = {i for rr in [recs, *others.values()] for k, r in rr.items() if k.startswith("top4_")
            for i in (r.get("items") or [])}
    shared = {k: [ZNACKA] + [z for z, rr in others.items() if k in rr] for k in recs}
    used |= {k for k, zs in shared.items() if len(zs) > 1 and top4_owner(k, zs) != ZNACKA}
    src = load_json(HERE / "sources.json", {})
    cand = []
    for k, r in recs.items():
        if k.startswith(("top3_", "top4_", "exp_")) or k in used or r.get("deleted") or not r.get("payoff"):
            continue
        try:
            t = datetime.fromisoformat(r.get("due") or "")
        except ValueError:
            continue
        if not now - timedelta(hours=TOP4_WINDOW_H) <= t <= now:   # already out: the Top 4 never spoils a short
            continue
        strength = top4_strength(r, src)
        if strength is not None:
            cand.append((strength, k, r))
    cand.sort(key=lambda x: -x[0])
    return [(k, r) for _, k, r in cand]

TOP4_WINDOW_H = 36                                    # recycling is fine (owner 19. 11.), but only the best

def top4_strength(r, src):
    """Owner 19. 11.: 'klidne at je recykluje ale jen ty nejlepsi': only a star's clip (star_tier1, e.g.
    IShowSpeed), a viral one (score >= good_score) or a clearly funny one (funny >= 0.6) gets in; ranked by
    funny 50 %, virality 30 % (score / hot_score, capped) and a star bonus 20 %. None = not good enough."""
    star = str(r.get("channel") or "").lower() in {x.lower() for x in src.get("star_tier1", [])}
    funny, score = r.get("funny") or 0, r.get("score") or 0
    if not (star or score >= src.get("good_score", 1000) or funny >= 0.6):
        return None
    return 0.5 * funny + 0.3 * min(1.0, score / max(1, src.get("hot_score", 2000))) + (0.2 if star else 0)

TOP4_NUDE_Q = "A frame from a video game. Is there nudity (bare breasts, nipples, buttocks, genitals)? Answer yes or no."

def top4_nudity(mp4):
    """True = nudity stays on screen in the payoff (both of 2 game-band frames, = klipuj.nudity_explicit; owner
    19. 11.: GTA 6 has nudity, not too strict - a glimpse or a silhouette passes) -> never in a Top 4. None (no
    vision) counts as not checked = not allowed either."""
    import nahled_ai
    try:
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                    str(mp4)], capture_output=True, text=True, timeout=60).stdout.strip())
    except ValueError:
        return None
    yes = []
    for i, t in enumerate((dur / 3, dur * 2 / 3)):
        f = Path(mp4).with_suffix(f".nude{i}.jpg")
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(mp4), "-frames:v", "1",
                        "-vf", "crop=1080:1280:0:640,scale=640:-2", str(f)], capture_output=True, timeout=60)
        if not f.exists():
            return None
        ans = nahled_ai.vision(f, TOP4_NUDE_Q, max_tokens=4)
        f.unlink(missing_ok=True)
        if ans is None:
            return None
        yes.append(str(ans).strip().lower().startswith("yes"))
    return all(yes)

def top4_ass_time(s):
    return f"{int(s // 3600)}:{int(s % 3600 // 60):02d}:{s % 60:05.2f}"

def make_top4(items, out, znacka=None):
    """items = [(rank, label, local payoff mp4)] in play order (#4 first) -> out mp4, or raises."""
    durs = []
    for _, _, f in items:
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                                "-of", "json", str(f)], capture_output=True, text=True, timeout=60)
        info = json.loads(probe.stdout or "{}")
        if "audio" not in [x.get("codec_type") for x in info.get("streams", [])]:
            raise RuntimeError(f"{Path(f).name} has no audio")
        durs.append(float(info.get("format", {}).get("duration") or 0))
    starts = [sum(durs[:i]) for i in range(len(durs))]
    total = sum(durs)
    if not 8 <= total <= 60:
        raise RuntimeError(f"{total:.1f} s is outside 8-60 s")
    y0, lh = 110, 104                                 # owner 19. 11.: up in the facecam band (where the head is),
                                                      # above the seam captions; 1 on top, 4 at the bottom
    ass = out.with_suffix(".ass")
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
            "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding\n"
            f"Style: R,Anton,84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,0,0,0,0,100,100,1,0,1,6,2,4,0,0,0,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    ev = []
    for (rank, label, _), t0 in zip(items, starts):  # owner 19. 11.: the numbers 1-4 are there from the start,
        y = y0 + (rank - 1) * lh                         # only the label appears when its clip starts
        ev.append(f"Dialogue: 0,{top4_ass_time(0)},{top4_ass_time(total + 1)},R,,0,0,0,,"
                  f"{{\\pos(44,{y})\\c&H{TOP4_COL[rank]}&}}{rank}.")
        ev.append(f"Dialogue: 0,{top4_ass_time(t0)},{top4_ass_time(total + 1)},R,,0,0,0,,"
                  f"{{\\pos(112,{y})\\fad(90,0)\\fscx60\\fscy60\\t(0,140,\\fscx100\\fscy100)}}"
                  f"{{\\c&H{TOP4_COL[rank]}&}}{label}")
    ass.write_text(head + "\n".join(ev) + "\n", encoding="utf-8")
    follow = HERE / "assets" / "follow" / f"follow_{znacka or ZNACKA}.webm"
    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error"]
    for _, _, f in items:
        cmd += ["-i", str(f)]
    n = len(items)
    flt = "".join(f"[{i}:v]fps=30,scale=1080:1920,setsar=1,format=yuv420p[v{i}];" for i in range(n))
    flt += "".join(f"[v{i}][{i}:a]" for i in range(n)) + f"concat=n={n}:v=1:a=1[cv][a];"
    esc = lambda p: str(p).replace("\\", "/").replace(":", "\\:")
    flt += f"[cv]subtitles='{esc(ass)}':fontsdir='{esc(HERE / 'fonts' / 'cap')}'"
    if follow.exists():                               # the Follow card over the last ~2.2 s (alpha webm)
        cmd += ["-c:v", "libvpx-vp9", "-itsoffset", f"{max(0.0, total - 2.25):.3f}", "-i", str(follow)]
        flt += f"[sv];[sv][{n}:v]overlay=0:0:eof_action=pass[v]"
    else:
        flt += "[v]"
    cmd += ["-filter_complex", flt, "-map", "[v]", "-map", "[a]", "-t", f"{total:.3f}", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "21", "-maxrate", "4M", "-bufsize", "8M", "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    ass.unlink(missing_ok=True)
    if r.returncode or not out.exists():
        raise RuntimeError(f"ffmpeg failed {r.stderr[-300:]}")
    return out, starts[-1] + 1.0                     # cover: #1 has just started, the whole list is on screen

TOP4_POOL = HERE / "top4_pool.json"           # "<brand>:<clip id>" -> a rendered clip that did NOT go out as a short

def top4_pool_collect(a, day, now):
    """Owner 19. 11.: the Top 4 is not only a rerun of the channel's own shorts - every good rendered clip of
    this run that was not posted (over the quota, not picked) keeps its clean payoff for the Top 4: uploaded
    and listed in top4_pool.json (the runner's out/ is gone after the run). Only clips that pass the action
    gate (never a boring one), only the brand's own payoff (render gave each clip its brand). Never raises."""
    if FAST or not (BRAND.get("rank4") and BRAND.get("rank_times")):
        return
    try:
        pool = load_json(TOP4_POOL, {})
        src = load_json(HERE / "sources.json", {})
        files, add = {}, {}
        for m in candidates()[:30]:
            key = f"{ZNACKA}:{m.get('id')}"
            if key in pool or action_gate(m, src):
                continue
            if m.get("remote"):                       # inbox (YouTube PC / live): the payoff is online already
                if not m.get(f"payoff_{ZNACKA}"):
                    continue
                rel = f"{m['base']}/{m[f'payoff_{ZNACKA}']}"
            else:
                po = (m.get("payoffs") or {}).get(ZNACKA)
                if not po or not Path(po).exists():
                    continue
                rel = f"{day:%Y%m%d}/top4pool_{safe_name(m['id'])}_{ZNACKA}.mp4"
                files[rel] = Path(po)
            add[key] = {"id": m["id"], "due": now.isoformat(), "payoff": rel, "channel": m.get("channel"),
                        "platform": m.get("platform"), "score": round(m.get("score") or 0, 1),
                        "funny": (m.get("tag_scores") or {}).get("funny"), "tags": m.get("tags"),
                        "quote": (m.get("quote") or "")[:160], "title": title_line(m), "pool": True}
        if not add:
            return
        log(f"top4 pool: +{len(add)} unposted clip(s) of {ZNACKA}")
        if a.zkouska:
            return
        if files:
            load_env()
            base = inbox_deploy(files, b"[]", MEDIA_TITLE)
            for v in add.values():
                if not v["payoff"].startswith("http"):
                    v["payoff"] = f"{base}/{v['payoff']}"
        pool = load_json(TOP4_POOL, {})
        old = (now - timedelta(days=3)).isoformat()
        pool = {k: v for k, v in pool.items() if (v.get("due") or "") >= old}
        pool.update(add)
        save_json(TOP4_POOL, pool)
        push_records(TOP4_POOL)
    except Exception as e:
        print(f"::warning::Top 4 pool: {str(e)[-200:]}")

def top4_sources(znacka, recs):
    """What a brand's Top 4 may draw from: its own published shorts (+ its Top 4 records) and its pool clips."""
    pool = {v["id"]: v for k, v in load_json(TOP4_POOL, {}).items() if k.startswith(f"{znacka}:") and v.get("id")}
    return {**pool, **recs}

def top4_post(a, day, now):
    """Post the due Top 4 of the publishing day (at most one per run). Never raises: a Top 4 never costs the
    classics."""
    try:
        if not (BRAND.get("rank4") and BRAND.get("rank_times")) or budget_level() >= 2:
            return
        recs = records()
        due_slot = None
        for i, slot in enumerate(top4_slots(day)):
            key = f"top4_{day}_{i}"
            if key not in recs and slot - timedelta(minutes=45) <= now <= slot + timedelta(hours=3):
                due_slot = (key, slot)
                break
        if not due_slot:
            return
        key, slot = due_slot
        stage = OUT / "_media"
        stage.mkdir(parents=True, exist_ok=True)
        chosen, files = [], {}                       # funniest first; nudity (or no check possible) = skipped
        others = {z: top4_sources(z, records(z)) for z, v in brands_cfg().items() if isinstance(v, dict) and z != ZNACKA}
        for k, r in top4_pick(top4_sources(ZNACKA, recs), now, others):
            if len(chosen) == TOP4_N:
                break
            local = stage / f"{key}_{len(chosen)}.mp4"
            try:
                local.write_bytes(http(r["payoff"], timeout=300)[1])
            except Exception as e:
                log(f"top4: {k} payoff unavailable ({str(e)[-80:]})")
                continue
            nude = top4_nudity(local)
            if nude is not False:
                log(f"top4: {k} skipped ({'nudity' if nude else 'nudity not checked'})")
                continue
            chosen.append((k, r))
            files[k] = local
        if len(chosen) < TOP4_N:
            log(f"top4 {key}: only {len(chosen)} good clean unused clips with a payoff, not yet")
            return
        chosen.reverse()                             # play order: #4 (least funny of the four) first, #1 last
        items = []
        for i, (k, r) in enumerate(chosen):
            items.append((TOP4_N - i, top4_label(r, files[k], [x[1] for x in items]), files[k]))
        out, cover = make_top4(items, stage / f"{key}.mp4")
        due = off_grid(max(slot, (now_tz() + timedelta(minutes=20)).replace(second=0, microsecond=0)))
        m = {"id": key, "platform": "top4", "channel": "top4", "category": "Grand Theft Auto VI", "peak": cover,
             "items": [{"channel": r.get("channel"), "platform": r.get("platform"), "label": lab}
                       for (k, r), (_, lab, _) in zip(reversed(chosen), reversed(items))]}
        names = " | ".join(f"{rk}. {lab} ({r.get('channel')})" for (rk, lab, _), (k, r) in zip(items, chosen))
        log(f"top4 {key}: {names} -> {due:%d.%m %H:%M}")
        if a.zkouska:
            log(f"top4: test only, video {out}")
            return out
        load_env()
        rel = f"{day:%Y%m%d}/{key}.mp4"
        base = inbox_deploy({rel: out}, b"[]", MEDIA_TITLE)
        url = f"{base}/{rel}"
        if not is_public(url):
            print("::warning::Top 4: not reachable after deploy")
            return
        chans = load_json(CHANNELS, [])
        caps = load_json(HERE / "sources.json", {}).get("caps") or {}
        used = day_service_counts(recs, chans, day)
        posts = {}
        for ch in chans:
            service = ch["service"].lower()
            if used.get(service, 0) >= caps.get(service, 99):
                log(f"  top4 {service}: daily cap reached, skipped")
                continue
            try:
                posts[ch["id"]] = create_post(post_input(ch, m, url, fresh_due(net_due(due, day, service)), not a.ostre),
                                              ch.get("key", "BUFFER_API_KEY"))
                log(f"  top4 {service}: ok")
            except Exception as e:
                log(f"  top4 {service}: FAILED {e}")
        if not posts:
            print("::warning::Top 4: no Buffer post succeeded")
            return
        own = load_json(PUBLISHED, {})
        own[key] = {"url": url, "path": rel, "due": due.isoformat(), "draft": not a.ostre, "posts": posts,
                    "channel": "top4", "platform": "top4", "items": [k for k, _ in chosen],
                    "labels": [x[1] for x in items], "mb": round(out.stat().st_size / 1e6, 1)}
        save_json(PUBLISHED, own)
        push_records(PUBLISHED)
        return out
    except Exception as e:
        print(f"::warning::Top 4: {str(e)[-200:]}")

TOP3_FONT = HERE / "fonts" / "cap" / "montserrat-900.ttf"

def top3_only(a, day, now, final=True):
    """A run with no new shorts: build, deploy and post the day's Top 3 if it is due."""
    load_env()
    if not (brand_keys() and os.environ.get("NETLIFY_AUTH_TOKEN")):
        return
    done = records()
    stage = OUT / "_media"
    stage.mkdir(parents=True, exist_ok=True)
    top3 = make_top3(day, done, [], stage, final=final)
    if not top3:
        return
    m, due, rel = top3
    try:
        base = inbox_deploy({rel: m.pop("file")}, b"[]", MEDIA_TITLE)    # draft deploy, 0 credits
        url = f"{base}/{rel}"
        public = is_public(url)
    except Exception as e:
        print(f"::warning::Top 3: deploy failed {str(e)[-200:]}")
        return
    if not public:
        print("::warning::Top 3: not reachable after deploy")
        return
    chans = load_json(CHANNELS, [])
    posts, caps, used = {}, load_json(HERE / "sources.json", {}).get("caps") or {}, day_service_counts(done, chans, day)
    for i, ch in enumerate(chans):
        service = ch["service"].lower()
        if used.get(service, 0) >= caps.get(service, 99):   # X / Threads daily cap applies to the Top 3 too
            log(f"  top3 {service}: daily cap reached, skipped")
            continue
        try:
            posts[ch["id"]] = create_post(post_input(ch, m, url, fresh_due(net_due(due, day, service)), not a.ostre),
                                          ch.get("key", "BUFFER_API_KEY"))
            log(f"  top3 {service}: ok")
        except Exception as e:
            log(f"  top3 {service}: FAILED {e}")
    if not posts:                                    # failed everywhere: the next run may try again
        print("::warning::Top 3: no Buffer post succeeded")
        return
    own = load_json(PUBLISHED, {})                   # Top 3: normal runs only -> the main file
    own[m["id"]] = {"url": url, "path": rel, "due": due.isoformat(), "draft": not a.ostre, "posts": posts,
                    "channel": "top3", "platform": "top3"}
    save_json(PUBLISHED, own)
    push_records(PUBLISHED)

def make_top3(day, done, plan, stage, final=True):
    """
    Daily 'Top 3 GTA 6 moments' (Fable 15. 11.: a curated series is the 'something new' the
    platforms ask for): once the day's minimum is met AND the day is in its last stretch (`final`:
    no free slot left after the planning horizon), the 3 best shorts of the day, #3 -> #1,
    each with a big number for the first 1.2 s, posted 75 min after the day's last short.
    Returns (meta, due, site path) with meta['file'] = the local mp4, or None.
    """
    key = f"top3_{day}"
    if (not final or not BRAND.get("top3", True) or key in done or remaining(day, done) - len(plan) > 0
            or budget_level()):
        return None
    items = []                                       # (score, meta-ish, local file or url, due)
    for k, rec in done.items():
        if (not k.startswith(("top3_", "top4_", "exp_")) and in_day(rec, day) and rec.get("url") and rec.get("channel")
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
    # built later now (last stretch of the day): never in the past - render + deploy + Buffer's fetch,
    # and never on the full / half hour
    due = max(due, (now_tz() + timedelta(minutes=25)).replace(second=0, microsecond=0))
    # never after the publishing window (12:00 next day): at the latest its end - 5 min (+ the networks'
    # 0-9 min spread stays inside the next day's first slots' gap)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), TZ) + timedelta(hours=12)
    due = min(due, end - timedelta(minutes=5 + 10))
    if due < now_tz() + timedelta(minutes=10):
        log("top3: the publishing window is over, skipped")
        return None
    due = off_grid(due)
    meta = {"id": key, "platform": "top3", "channel": "top3", "category": "Grand Theft Auto VI",
            "items": [best[i][1] for i in range(3)], "peak": 1.0, "score": 0, "file": out, "title": "Top 3"}
    log(f"top3: {', '.join(x[1]['channel'] for x in best)} -> {due:%d.%m %H:%M}")
    return meta, due, f"{day:%Y%m%d}/top3.mp4"

def refresh_records():
    """Overwrite the local record files with origin/main's (a long-running job's checkout is stale).
    Read-only for git: fetch + show, nothing is committed. Keeps the local file when git fails."""
    if git("fetch", "-q", "origin", "main")[0]:
        log("inbox: git fetch failed, using the local records")
        return
    for f in all_record_files() + [fails_file()]:
        rc, out, _ = git("show", f"origin/main:{f.name}")
        if rc == 0 and out:
            try:
                save_json(f, json.loads(out))
            except json.JSONDecodeError:
                pass

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
    live_box = a.prefix.startswith("live")
    if live_box:                                     # the live job's checkout is hours old: fresh records
        refresh_records()
    done = {z: records(z) for z in names}            # both record files of each brand
    for z in names:                                  # experiment clips never go out again
        for k, rec in list(done[z].items()):
            if k.startswith("exp_"):
                done[z].update({i: {} for i in rec.get("items") or [] if isinstance(i, str)})
    src_cfg = load_json(HERE / "sources.json", {})
    open_brands = set(names)
    if live_box:                                     # a live short only when its brand can still post today
        keep_z = ZNACKA
        try:
            for z in names:
                set_brand(z)
                if ZNACKA == "fanpage" and budget_level() >= 2 or day_plan(pub_day(), now_tz(), records(z))["cap"] <= 0:
                    open_brands.discard(z)
        except Exception as e:
            log(f"inbox: quota check failed ({str(e)[-100:]}), all brands open")
            open_brands = set(names)
        finally:
            set_brand(keep_z)
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
        if live_box and time.time() - (m.get("created") or 0) > src_cfg.get("hot_max_age_h", 3) * 3600:
            continue                                 # too old for the hot path: it would never go out
        for z in names:
            short = brand_short(m, meta_file, z)
            if not short or not Path(short).exists() or m.get("id") in done[z] or z not in open_brands:
                continue
            rel = f"yt/{safe_name(m['id'])}.mp4" if z == "bananas" else f"yt/{safe_name(m['id'])}_{z}.mp4"
            files[rel] = Path(short)
            item["file" if z == "bananas" else f"file_{z}"] = rel
            po = (m.get("payoffs") or {}).get(z)          # the clean payoff for the daily Top 4 (19. 11.:
            if po and Path(po).exists():                  # IShowSpeed & co. come from here, never in a Top 4 before)
                prel = f"yt/{safe_name(m['id'])}_payoff{'' if z == 'bananas' else '_' + z}.mp4"
                files[prel] = Path(po)
                item[f"payoff_{z}"] = prel
        if any(k == "file" or k.startswith("file_") for k in item):
            # long-form: the 16:9 720p cut of the moment + its pool row (GitHub can't fetch YouTube)
            try:
                long_src = meta_file.parent / (m.get("long_src") or "")
                if m.get("long_src") and m.get("pool_row") and long_src.is_file():
                    rel = f"yt/{safe_name(m['id'])}_16x9.mp4"
                    files[rel] = long_src
                    item["long_file"] = rel
                else:
                    item.pop("pool_row", None)
            except Exception as e:                    # never at the cost of the shorts inbox
                log(f"inbox: long-form source of {m.get('id')} skipped ({e})")
                item.pop("long_file", None)
            items.append(item)
    now = time.time()                                # only the best few: `den` takes a handful a day anyway
    if live_box:                                     # live: the newest first (they go out by the hot path)
        items.sort(key=lambda m: -(m.get("created") or 0))
    else:
        items.sort(key=lambda m: -(m.get("views") or 0) / (max((now - (m.get("created") or now)) / 3600, 0) + 1) ** 0.5)
    items = items[: a.max or (40 if live_box else 10)]
    files = {m[k]: files[m[k]] for m in items for k in m if k in ("file", "long_file") or k.startswith("file_")}
    sent = HERE / f"schranka_{a.nazev}.json"          # ids of the live inbox (local, not in git)
    live = set(load_json(sent, []))
    new = [m["id"] for m in items if m["id"] not in live]
    if not new:
        log(f"inbox: nothing new ({len(items)} waiting, all already on Netlify)")
        return
    try:
        url = inbox_deploy(files, json.dumps(items, ensure_ascii=False).encode(), a.nazev)
    except Exception as e:                           # Netlify limit / outage: the next hand-over retries
        print(f"::warning::inbox {a.nazev}: deploy failed ({str(e)[-200:]})")
        sys.exit(1)
    save_json(sent, [m["id"] for m in items])
    log(f"inbox: {len(items)} shorts ({len(new)} new) -> {url}")

# -----------------------------------------------------------------------------
# Publishing lock (18. 11.): the live fast path has its own concurrency group, so it no longer waits
# behind a render - but then two runs could publish at the same moment, each blind to the other's
# fresh posts (double post, max_per_day or the X/Threads cap exceeded). The publishing part of every
# run therefore holds a lock that lives in git: publikace_zamek.json on main, taken and released by a
# commit pushed straight on top of origin/main (git plumbing: the work tree, the index and local
# commits stay untouched). A push is a compare-and-swap - rejected if origin moved since the fetch -
# so two runs can never both take it. A dead run's lock expires after --ttl minutes.

LOCK_FILE = "publikace_zamek.json"
LOCK_LOST = "zamek_ztracen"              # local flag (not in git): `obnov` lost the lock -> `den` stops posting
LOCK_UNTIL = "zamek_do"                  # local (not in git): our lock surely holds until this epoch (expiry - margin)
LOCK_MARGIN = 180                        # s before the expiry in git: stop posting, whatever the renewal is doing

def lock_lost():
    """True once this job must not post any more: the renewal lost the lock, or the last confirmed expiry
    (minus a margin) has passed - checked against the clock, so a hanging git cannot outlive the lock."""
    if (HERE / LOCK_LOST).exists():
        return True
    try:
        return time.time() > float((HERE / LOCK_UNTIL).read_text())
    except (FileNotFoundError, ValueError):
        return False                         # no lock in use (local runs, tests)

def lock_deadline(until):
    (HERE / LOCK_UNTIL).write_text(str(until - LOCK_MARGIN))

def push_records(path):
    """
    Under the publishing lock: push the record file to origin right after a post (merged per key, the
    lock stays held), so a run that ever takes the lock over sees every post of this run. If the push
    fails, this job posts nothing more (LOCK_LOST) - an unshared record must not grow.
    """
    if not (HERE / LOCK_UNTIL).exists():
        return True                              # no lock in use (local runs, tests)
    try:
        ok = lock_release("den", [Path(path).name], tries=8, release=False)
    except Exception as e:
        log(f"records push: {str(e)[-120:]}")
        ok = False
    if not ok:
        (HERE / LOCK_LOST).write_text(str(int(time.time())))
        print("::error::zaznam publikace se nepodarilo pushnout - dalsi posty tohoto behu se neposlou")
    return ok
GIT_ID = {"GIT_AUTHOR_NAME": "gta6bananaclips-bot", "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
          "GIT_COMMITTER_NAME": "gta6bananaclips-bot", "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com"}

def git(*args, data=None, env=None, cwd=None):
    r = subprocess.run(["git", *args], cwd=cwd or HERE, input=data, capture_output=True, timeout=120,
                       env={**os.environ, **GIT_ID, **(env or {})})
    return r.returncode, r.stdout.decode("utf-8", "replace").strip(), r.stderr.decode("utf-8", "replace").strip()

def git_cas(build, msg, cwd=None):
    """
    One compare-and-swap on origin/main. build(get) gets get(path) -> origin's text of a file (or None)
    and returns {path: new text} to commit, or None to back off (lock held). True = pushed (or nothing
    to change), None = backed off, False = origin moved / git error (try again).
    """
    if git("fetch", "-q", "origin", "main", cwd=cwd)[0]:
        return False
    rc, base, _ = git("rev-parse", "origin/main", cwd=cwd)
    if rc:
        return False

    def get(path):
        rc, out, _ = git("show", f"origin/main:{path}", cwd=cwd)
        return out if rc == 0 else None
    files = build(get)
    if files is None:
        return None
    if not files:
        return True
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        env = {"GIT_INDEX_FILE": str(Path(t) / "index")}
        if git("read-tree", base, env=env, cwd=cwd)[0]:
            return False
        for path, text in files.items():
            rc, blob, _ = git("hash-object", "-w", "--stdin", data=text.encode("utf-8"), cwd=cwd)
            if rc or git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env, cwd=cwd)[0]:
                return False
        rc, tree, _ = git("write-tree", env=env, cwd=cwd)
        if rc:
            return False
    rc, commit, _ = git("commit-tree", tree, "-p", base, "-m", msg, cwd=cwd)
    if rc:
        return False
    return git("push", "-q", "origin", f"{commit}:refs/heads/main", cwd=cwd)[0] == 0

def lock_take(who, ttl_min=20, wait_min=25, cwd=None, poll=10):
    """Take the publishing lock (wait while another live run holds it). False = gave up."""
    deadline = time.time() + wait_min * 60
    holder = {}
    while True:
        def build(get):
            try:
                cur = json.loads(get(LOCK_FILE) or "{}")
            except json.JSONDecodeError:
                cur = {}
            if cur.get("kdo") and cur["kdo"] != who and cur.get("until", 0) > time.time():
                holder.update(cur)
                return None                      # held by a live run
            if cur.get("kdo") and cur["kdo"] != who:
                log(f"lock: {cur['kdo']} expired, taking over")
            holder["until"] = time.time() + ttl_min * 60
            return {LOCK_FILE: json.dumps({"kdo": who, "until": holder["until"],
                                           "od": datetime.now(timezone.utc).isoformat(timespec="seconds")}) + "\n"}
        r = git_cas(build, f"zamek: {who}", cwd)
        if r is True:
            if cwd is None:
                lock_deadline(holder["until"])
            log(f"lock: taken by {who}")
            return True
        if time.time() >= deadline:
            print(f"::warning::zamek publikace: {holder.get('kdo') or 'git'} ho drzi, po {wait_min:.0f} min vzdavam")
            return False
        time.sleep(poll if r is None else 2)

def lock_release(who, save=(), cwd=None, tries=10, release=True):
    """
    Release the lock and, in the same commit, push this run's record files - merged into origin's
    version (ours wins per key), and the merged file is written back to the work tree (so the later
    `save state` rebase sees identical changes, no conflict). release=False: only push the records.
    """
    root = Path(cwd) if cwd else HERE
    merged = {}

    def build(get):
        out = {}
        for f in save:
            local = root / f
            if not local.exists() and get(f) is None:
                continue
            try:
                origin = json.loads(get(f) or "{}")
            except json.JSONDecodeError:
                origin = {}
            if Path(f).name == "stories_state.json":       # nested {brand: {reposts, days, pool}}: a shallow
                from tools.merge_state import merge        # merge would drop origin's posts of the same brand
                merged[f] = merge(origin, load_json(local, {}))
            else:
                merged[f] = {**origin, **load_json(local, {})}
            out[f] = json.dumps(merged[f], indent=2, ensure_ascii=False)
        try:
            cur = json.loads(get(LOCK_FILE) or "{}")
        except json.JSONDecodeError:
            cur = {}
        if release and cur.get("kdo") == who:
            out[LOCK_FILE] = json.dumps({"kdo": None, "until": 0}) + "\n"
        return out
    for _ in range(tries):
        merged.clear()
        if git_cas(build, f"stav publikace{' + zamek pust' if release else ''}: {who}", cwd) is True:
            for f, data in merged.items():
                save_json(root / f, data)
            if release and cwd is None:
                (HERE / LOCK_UNTIL).unlink(missing_ok=True)
            log(f"lock: {'released' if release else 'records pushed'} by {who}"
                + (f", saved {', '.join(merged)}" if merged else ""))
            return True
        time.sleep(random.uniform(1.5, 6))           # random: two writers pushing at once do not collide again
    print(f"::error::zamek publikace: nepodarilo se uvolnit / ulozit zaznam ({who})")
    return False

def lock_renew(who, ttl_min=20, cwd=None):
    """Push a new expiry while we still hold the lock. True = renewed, None = not ours any more."""
    def build(get):
        try:
            cur = json.loads(get(LOCK_FILE) or "{}")
        except json.JSONDecodeError:
            cur = {}
        if cur.get("kdo") != who:
            return None
        new["until"] = time.time() + ttl_min * 60
        return {LOCK_FILE: json.dumps({**cur, "until": new["until"]}) + "\n"}
    new = {}
    for _ in range(5):
        r = git_cas(build, f"zamek obnov: {who}", cwd)
        if r is True and cwd is None:
            lock_deadline(new["until"])
        if r is not False:
            return r
        time.sleep(3)
    return False

def cmd_zamek(a):
    if a.akce == "vezmi":
        sys.exit(0 if lock_take(a.kdo, a.ttl, a.cekej) else 1)
    if a.akce == "obnov":
        # runs in the background of the job from `vezmi` to `pust` (the workflow kills it before `pust`):
        # a long publishing part (Top 3 render, Buffer rate limits) never outlives its lock; when the job
        # dies, this dies with it and the lock expires after --ttl
        (HERE / LOCK_LOST).unlink(missing_ok=True)
        good = time.time()                       # last time the lock was surely ours
        while True:
            time.sleep(max(30, a.ttl * 60 / 4))
            try:
                r = lock_renew(a.kdo, a.ttl)
            except Exception as e:               # git timeout etc. = a failed renewal, never a silent exit
                log(f"lock: renewal error {str(e)[-120:]}")
                r = False
            if r:
                good = time.time()
                log(f"lock: renewed by {a.kdo}")
                continue
            # not ours any more, or no renewal for 3/4 of the TTL (git down): `den` stops posting
            if r is None or time.time() - good > a.ttl * 60 * 0.75:
                (HERE / LOCK_LOST).write_text(str(int(time.time())))
                print(f"::error::zamek publikace: {a.kdo} ho uz nedrzi / nelze obnovit - publikace se zastavi",
                      flush=True)
                sys.exit(1)
            log("lock: renewal failed, trying again")
    sys.exit(0 if lock_release(a.kdo, a.uloz) else 1)

def cmd_zbyva(a):
    """
    Render side of the just-in-time plan (same day_plan as `den`), so GitHub renders only what this
    run can post: `need` slot posts (`owed` of them catch-up = any clip, then `good_n` that need
    good_score, the rest extra_score) + up to `hot` extra clips that are hot (score >= hot_score, fresh).
    A rendered clip that is not posted is lost (the runner's out/ is gone next run).
    """
    now = now_tz()
    day = pub_day(now)
    if ZNACKA == "fanpage" and budget_level() >= 2:
        print("0 0 0 0 0 0 0" if a.plan else 0)
        return
    src = load_json(HERE / "sources.json", {})
    good, extra = src.get("good_score", 1000), extra_score(day, src)
    p = day_plan(day, now)
    good_n = max(0, min(p["slots"] - p["owed"], p["must"] - p["used"] - p["owed"]))
    hot = min(max(0, p["cap"] - p["slots"]), src.get("hot_per_run", 2))
    hs = int(src.get("hot_score", 2000))
    # hot pacing (hot_pick): right after a hot post only an exceptional clip (3 x hot_score) may go out,
    # so only such a clip is worth rendering (a rendered, unposted clip is lost with the runner)
    gap = timedelta(minutes=src.get("hot_gap_min", 30))
    recent = []
    for rec in records().values():
        try:
            if rec.get("hot") and not rec.get("live") and abs(now - datetime.fromisoformat(rec.get("due") or "")) < gap:
                recent.append(rec)
        except ValueError:
            pass
    if recent:
        hot, hs = min(hot, 1), 3 * hs
    # day-long pace (pace_fits with the burst): when it has no room for a hot post now, only an
    # exceptional clip (4 x hot_score, it may break the pace) is worth rendering
    dues = quota_dues(day, records())
    room = 0
    while room < hot and pace_fits(dues + [now] * room, now, day, src, hot=True):
        room += 1
    if room < hot:
        ex = {**EXCEPTIONAL, **(src.get("exceptional") or {})}
        hot = room if room else min(hot, 1)
        if not room:
            hs = max(hs, int(ex["score_x"] * src.get("hot_score", 2000)))
    if a.plan:
        print(p["slots"], p["owed"], good_n, int(good), int(extra), hot, hs)
    else:
        print(p["slots"])

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
    s.add_argument("--max", type=int, default=None, help="at most this many shorts in the inbox (best first;"
                   " default 10, live-inbox 40)")
    s.add_argument("--nazev", default=INBOX_TITLE, help="draft deploy title (yt-inbox, live-inbox)")
    s.add_argument("--prefix", default="", help="only shorts whose id starts with this (live_); default: YouTube")
    s.set_defaults(fn=cmd_schranka)
    d.add_argument("--rychle", action="store_true",
                   help="live fast path: only live clips, only the hot path, record -> publikovano*_live.json")
    z = sub.add_parser("zbyva", help="how many shorts this run should render (just-in-time plan, for the workflow)")
    z.add_argument("--plan", action="store_true", help="print: need owed good_n good extra hot hot_score")
    z.set_defaults(fn=cmd_zbyva)
    lk = sub.add_parser("zamek", help="publishing lock in git (one publisher at a time: normal run / live fast path)")
    lk.add_argument("akce", choices=["vezmi", "pust", "obnov"],
                    help="vezmi = take, pust = release (+ save records), obnov = keep renewing while the job lives")
    lk.add_argument("--kdo", required=True, help="lock owner, e.g. the GitHub run id")
    lk.add_argument("--ttl", type=float, default=20, help="minutes the lock is valid (a dead run frees it)")
    lk.add_argument("--cekej", type=float, default=25, help="vezmi: wait at most this many minutes")
    lk.add_argument("--uloz", nargs="*", default=[], help="pust: also commit these record files (merged into origin's)")
    lk.set_defaults(fn=cmd_zamek)
    for x in (k, d, s, z, lk):
        x.add_argument("--znacka", default="bananas", help="account from brands.json: bananas (default) | fanpage")
    a = p.parse_args()
    set_brand(a.znacka)
    a.fn(a)

if __name__ == "__main__":
    main()
