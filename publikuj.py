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

One-time setup:
  1. .env (see .env.example): BUFFER_API_KEY_1/_2, NETLIFY_AUTH_TOKEN, NETLIFY_SITE_ID, MEDIA_BASE_URL
  2. .venv/Scripts/python publikuj.py kanaly    # saves your Buffer channels to buffer.json
"""

import argparse
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
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
SLOTS = ["09:00", "10:30", "12:00", "13:30", "15:00", "16:30", "18:00", "19:30", "21:00", "22:30"]

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
                wait = int(e.headers.get("Retry-After") or 60)
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

# -----------------------------------------------------------------------------
# what to post

def candidates():
    """Rendered shorts not yet published, best first: views (log) minus a penalty for age."""
    done = load_json(PUBLISHED, {})
    now = time.time()
    out = []
    for meta_file in OUT.glob("*/*/meta.json"):
        if meta_file.parts[-3].startswith("_"):
            continue                                 # _ukazka, _media
        m = load_json(meta_file, {})
        if not m or m.get("id") in done or not Path(m.get("short", "")).exists():
            continue
        age_h = (now - (m.get("created") or now)) / 3600
        m["score"] = math.log1p(m.get("views") or 0) - age_h / 24
        out.append(m)
    return sorted(out, key=lambda m: -m["score"])

def youtube_title(m):
    t = (m.get("title") or "").strip() or "GTA 6 moment"
    return f"{t} | {m['channel']} #shorts"[:100]

def post_input(ch, m, video_url, due, draft):
    service = ch["service"].lower()
    inp = {
        "text": m["caption"],
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
    return [datetime.combine(day, datetime.strptime(s, "%H:%M").time(), TZ) for s in SLOTS[:n]]

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
    times = [t for t in slot_times(day, len(SLOTS)) if t > now + timedelta(minutes=20)][: a.n]
    picks = candidates()[: len(times)]
    times = times[: len(picks)]
    chans = load_json(CHANNELS, [])
    mode = "ZKOUŠKA" if a.zkouska else ("NAOSTRO" if a.ostre else "koncepty")
    log(f"{day}: {len(picks)} shorts x {len(chans) or '?'} channels ({mode})")
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
        if rec.get("path") and datetime.fromisoformat(rec["due"]) > now:
            local = stage / rec["path"].replace("/", "_")
            try:
                local.write_bytes(http(rec["url"], timeout=300)[1])   # carry it over
                files[rec["path"]] = local
            except urllib.error.URLError as e:
                log(f"  could not carry over {rec['url']}: {e}")
    plan = []
    for m, due in zip(picks, times):
        rel = f"{day:%Y%m%d}/{re.sub(r'[^A-Za-z0-9_-]', '_', m['id'])}.mp4"
        files[rel] = Path(m["short"])
        plan.append((m, due, rel))
    netlify_deploy(files)

    # 2) Buffer posts, one time slot per short, every channel
    for m, due, rel in plan:
        url = media_url(rel)
        log(f"{due:%H:%M} {m['platform']}/{m['channel']} '{m.get('title', '')[:50]}' -> {url}")
        if not is_public(url):
            log("  not reachable after deploy, skipped")
            continue
        posts = {}
        for ch in chans:
            try:
                posts[ch["id"]] = create_post(post_input(ch, m, url, due, not a.ostre), ch.get("key", "BUFFER_API_KEY"))
                log(f"  {ch['service']}: ok")
            except Exception as e:
                log(f"  {ch['service']}: FAILED {e}")
        done[m["id"]] = {"url": url, "path": rel, "due": due.isoformat(), "draft": not a.ostre, "posts": posts}
        save_json(PUBLISHED, done)

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
    a = p.parse_args()
    a.fn(a)

if __name__ == "__main__":
    main()
