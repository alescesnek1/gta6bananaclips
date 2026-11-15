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
"""

import argparse
import hashlib
import io
import shutil
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

def safe_name(clip_id):
    return re.sub(r"[^A-Za-z0-9_-]", "_", clip_id)

def netlify(path, data=None, method=None, ctype="application/json", timeout=120):
    h = {"Authorization": f"Bearer {os.environ['NETLIFY_AUTH_TOKEN']}", "Content-Type": ctype}
    return json.loads(http(f"{NETLIFY}{path}", data, h, method, timeout)[1] or b"null")

def inbox_deploy(files, inbox):
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
    d = netlify(f"/sites/{site}/deploys", json.dumps({"files": digests, "draft": True, "title": INBOX_TITLE}).encode(), "POST")
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
    """Shorts from the newest yt-inbox draft deploy (YouTube worker PC); [] when there is none."""
    if not (os.environ.get("NETLIFY_AUTH_TOKEN") and os.environ.get("NETLIFY_SITE_ID")):
        return []
    try:
        deploys = netlify(f"/sites/{os.environ['NETLIFY_SITE_ID']}/deploys?per_page=50")
        d = next((d for d in deploys if d.get("title") == INBOX_TITLE and d.get("state") == "ready"), None)
        if not d:
            return []
        base = d["links"]["permalink"]
        items = json.loads(http(f"{base}/inbox.json", timeout=60)[1])
    except Exception as e:                                    # no inbox must never stop Twitch/Kick posts
        log(f"inbox: unreadable ({e})")
        return []
    for m in items:
        m["short"], m["remote"] = f"{base}/{m['file']}", True
    log(f"inbox: {len(items)} YouTube shorts from the worker PC ({d.get('created_at', '')[:16]})")
    return items

# -----------------------------------------------------------------------------
# what to post

def candidates():
    """
    Rendered shorts not yet published, best first: virality = views per hour of age (softened).
    Gate 2 of 2: only shorts whose saved stream category / video title matches the game
    in sources.json (GTA 6) can go out - whatever slipped past the render step.
    """
    game = load_json(HERE / "sources.json", {}).get("game", "Grand Theft Auto VI|GTA ?VI|GTA ?6")
    done = load_json(PUBLISHED, {})
    now = time.time()
    out, local = [], []
    for meta_file in OUT.glob("*/*/meta.json"):
        if meta_file.parts[-3].startswith("_"):
            continue                                 # _ukazka, _media
        local.append(load_json(meta_file, {}))
    seen = {m.get("id") for m in local}
    load_env()
    for m in local + [m for m in inbox_items() if m.get("id") not in seen]:
        if not m or m.get("id") in done or not (m.get("remote") or Path(m.get("short", "")).exists()):
            continue
        if not m.get("cam"):                         # Aleš: every short has the streamer's face on top
            continue
        if not re.search(game, m.get("category") or "", re.I):
            log(f"  not published (category '{m.get('category') or '?'}' is not {game}): {m.get('id')}")
            continue
        age_h = (now - (m.get("created") or now)) / 3600
        m["score"] = (m.get("views") or 0) / (max(age_h, 0) + 1) ** 0.5   # same "virality" as klipuj.batch
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

def caption(m, service):
    q = (m.get("quote") or "").strip()
    q = f"“{q}”" if q else ""
    src = f"🎮 {home(m)}"
    if service in ("twitter", "threads"):            # short, one tag, text differs every post
        return "\n\n".join(x for x in (q, f"{src} #GTA6") if x)
    rot = random.Random(m["id"]).sample(HASHTAGS, 2)  # same clip -> same tags, clips differ
    return "\n\n".join(x for x in (q, src, " ".join(["#gta6", "#gta6bananaclips"] + rot)) if x)

def youtube_title(m):
    """Viewer clip titles are often junk ('yoooo') or risky ('LEAK'): then use the quote."""
    t = (m.get("title") or "").strip()
    if len(t) < 12 or JUNK_RE.search(t) or sum(c.isalpha() for c in t) < len(t) / 2:
        t = (m.get("quote") or "").strip() or "GTA 6 moment"
    return f"{t} | {m['channel']} GTA 6 #shorts"[:100]

def daily_quota(day):
    """Ramp-up for new accounts (sources.json 'ramp': {date: posts per day}); 10 if not set."""
    ramp = load_json(HERE / "sources.json", {}).get("ramp") or {}
    n = None
    for d, k in sorted(ramp.items()):
        if date.fromisoformat(d) <= day:
            n = k
    return n if n is not None else (min(ramp.values()) if ramp else 10)

def remaining(day):
    """Today's quota minus what earlier runs already scheduled into this publishing day."""
    taken = {rec.get("due") for rec in load_json(PUBLISHED, {}).values()}
    used = sum(t.isoformat() in taken for t in slot_times(day, len(SLOTS)))
    return max(0, daily_quota(day) - used)

def post_input(ch, m, video_url, due, draft):
    service = ch["service"].lower()
    inp = {
        "text": caption(m, service),
        "channelId": ch["id"],
        "schedulingType": Enum("automatic"),
        "mode": Enum("addToQueue" if draft else "customScheduled"),
        "assets": [{"video": {"url": video_url}}],
    }
    if not draft:
        inp["dueAt"] = due.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    else:
        inp["saveToDraft"] = True
    if service in ("instagram", "tiktok"):
        inp["assets"][0]["video"]["metadata"] = {"thumbnailOffset": 1000}
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
    """The publishing day runs from 14:00 to 05:00 the next morning (Prague)."""
    out = []
    for s in SLOTS[:n]:
        t = datetime.strptime(s, "%H:%M").time()
        out.append(datetime.combine(day + timedelta(days=1 if t.hour < 12 else 0), t, TZ))
    return out

# -----------------------------------------------------------------------------
# commands

def cmd_kanaly(a):
    """Channels of every Buffer account we have a key for (free plan = 3 channels per account)."""
    load_env()
    keys = buffer_keys()
    if not keys:
        sys.exit("missing in .env: BUFFER_API_KEY_1 (and _2 for the second account), see .env.example")
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
    # slots still ahead of us (+20 min for the deploy and for Buffer to fetch the video)
    taken = {rec.get("due") for rec in load_json(PUBLISHED, {}).values()}   # an earlier run today
    quota = min(a.n, remaining(day))         # several runs a day share one daily quota
    times = [t for t in slot_times(day, len(SLOTS))
             if t > now + timedelta(minutes=20) and t.isoformat() not in taken][:quota]
    day_slots = {t.isoformat() for t in slot_times(day, len(SLOTS))}
    yt_left = load_json(HERE / "sources.json", {}).get("youtube_per_day", 1) - sum(
        k.startswith("youtube_") and rec.get("due") in day_slots for k, rec in load_json(PUBLISHED, {}).items())
    picks = []
    for m in candidates():
        if len(picks) >= len(times):
            break
        if m.get("platform") == "youtube":
            if yt_left <= 0:
                continue
            yt_left -= 1
        picks.append(m)
    times = times[: len(picks)]
    chans = load_json(CHANNELS, [])
    mode = "ZKOUŠKA" if a.zkouska else ("NAOSTRO" if a.ostre else "koncepty")
    log(f"{day}: {len(picks)} shorts (quota {quota}) x {len(chans) or '?'} channels ({mode})")
    if os.environ.get("GITHUB_ACTIONS") and not a.ostre and not a.zkouska:
        print("::warning::Buffer: jen KONCEPTY, nic nevyjde. Na GitHubu nastav promennou OSTRE=1.")
    if a.zkouska:
        for m, due in zip(picks, times):
            log(f"{due:%H:%M} {m['platform']}/{m['channel']} '{m.get('title', '')[:50]}' ({m.get('views')} views)")
            fake = {"id": "CHANNEL_ID", "service": "youtube"}
            print("   ", gql_value(post_input(fake, m, "https://media.example/clip.mp4", due, not a.ostre))[:400])
        return
    if not picks:
        return
    load_env()
    need("NETLIFY_AUTH_TOKEN", "NETLIFY_SITE_ID", "MEDIA_BASE_URL")
    if not buffer_keys() or not chans:
        sys.exit("no Buffer keys or channels: fill .env and run `publikuj.py kanaly` first")

    # 1) one deploy: today's picks + videos from earlier runs whose slot hasn't come yet
    done = load_json(PUBLISHED, {})
    stage = OUT / "_media"
    stage.mkdir(parents=True, exist_ok=True)
    files = {}
    for rec in done.values():
        # keep anything due later, or published in the last 6 h (Buffer may retry a fetch)
        if rec.get("path") and datetime.fromisoformat(rec["due"]) > now - timedelta(hours=6):
            local = stage / rec["path"].replace("/", "_")
            try:
                local.write_bytes(http(rec["url"], timeout=300)[1])   # carry it over
                files[rec["path"]] = local
            except Exception as e:
                log(f"  could not carry over {rec['url']}: {e}")
    plan = []
    for m, due in zip(picks, times):
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
    netlify_deploy(files)

    # 2) Buffer posts, one time slot per short, every channel (X/Threads capped per day)
    caps = load_json(HERE / "sources.json", {}).get("caps") or {}
    used, ok, failed = {}, 0, 0
    for m, due, rel in plan:
        url = media_url(rel)
        log(f"{due:%H:%M} {m['platform']}/{m['channel']} '{m.get('title', '')[:50]}' -> {url}")
        if not is_public(url):
            log("  not reachable after deploy, skipped")
            failed += 1
            continue
        posts = {}
        for ch in chans:
            service = ch["service"].lower()
            if used.get(service, 0) >= caps.get(service, 99):
                continue
            try:
                posts[ch["id"]] = create_post(post_input(ch, m, url, due, not a.ostre), ch.get("key", "BUFFER_API_KEY"))
                used[service] = used.get(service, 0) + 1
                ok += 1
                log(f"  {service}: ok")
            except Exception as e:
                failed += 1
                log(f"  {service}: FAILED {e}")
        done[m["id"]] = {"url": url, "path": rel, "due": due.isoformat(), "draft": not a.ostre, "posts": posts}
        save_json(PUBLISHED, done)
    # make failures visible: a red run = an e-mail from GitHub
    if failed:
        print(f"::warning::Buffer: {failed} postu se nepovedlo, {ok} ok")
    if plan and not ok:
        print("::error::Buffer: nevysel ani jeden post (klic, Netlify nebo Buffer API)")
        sys.exit(1)

def cmd_schranka(a):
    """
    Worker PC: its rendered YouTube shorts that are not published yet -> yt-inbox draft deploy.
    Redeploys only when there is a short the live inbox doesn't have (published ones simply stay
    until the next deploy; `den` skips them). --uklid deletes clip folders older than --dni.
    """
    load_env()
    need("NETLIFY_AUTH_TOKEN", "NETLIFY_SITE_ID")
    game = load_json(HERE / "sources.json", {}).get("game", "Grand Theft Auto VI|GTA ?VI|GTA ?6")
    done = load_json(PUBLISHED, {})
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
        short = Path(m.get("short") or "")
        if (m.get("platform") != "youtube" or m.get("id") in done or not short.exists() or not m.get("cam")
                or not re.search(game, m.get("category") or "", re.I)):
            continue
        rel = f"yt/{safe_name(m['id'])}.mp4"
        files[rel] = short
        items.append({**{k: v for k, v in m.items() if k not in ("short", "base", "source")}, "file": rel})
    now = time.time()                                # only the best few: `den` takes a handful a day anyway
    items.sort(key=lambda m: -(m.get("views") or 0) / (max((now - (m.get("created") or now)) / 3600, 0) + 1) ** 0.5)
    items = items[: a.max]
    files = {m["file"]: files[m["file"]] for m in items}
    sent = HERE / "schranka.json"                    # ids of the live inbox (local, not in git)
    live = set(load_json(sent, []))
    new = [m["id"] for m in items if m["id"] not in live]
    if not new:
        log(f"inbox: nothing new ({len(items)} waiting, all already on Netlify)")
        return
    url = inbox_deploy(files, json.dumps(items, ensure_ascii=False).encode())
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
    s.set_defaults(fn=cmd_schranka)
    z = sub.add_parser("zbyva", help="print how many shorts today's quota still allows (for the workflow)")
    z.set_defaults(fn=lambda a: print(remaining(datetime.now(TZ).date())))
    a = p.parse_args()
    a.fn(a)

if __name__ == "__main__":
    main()
