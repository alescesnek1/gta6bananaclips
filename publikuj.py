"""
publikuj.py - gta6bananaclips: rendered shorts -> public URL (Cloudflare R2) -> Buffer
-> Instagram, TikTok, X, Facebook, YouTube.

Buffer's API has no upload: it takes a PUBLIC video URL and fetches the file at
publish time, so every short is first put into a public R2 bucket and must stay
there until it is published.

Daily flow:
  .venv/Scripts/python klipuj.py batch          # render fresh moments
  .venv/Scripts/python publikuj.py den --n 10   # best 10 -> Buffer, spread over tomorrow

By default posts land in Buffer as DRAFTS (nothing goes public). --ostre schedules
them for real. --zkouska prints the plan and the GraphQL without touching anything.

One-time setup:
  1. .env (see .env.example): BUFFER_API_KEY, R2_* keys, MEDIA_BASE_URL
  2. .venv/Scripts/python publikuj.py kanaly    # saves your Buffer channels to buffer.json
"""

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
PUBLISHED = HERE / "publikovano.json"   # clip id -> url, time, Buffer post ids
CHANNELS = HERE / "buffer.json"         # Buffer channels we post to (from `kanaly`)
API = "https://api.buffer.com"
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
    for line in (HERE / ".env").read_text(encoding="utf-8").splitlines() if (HERE / ".env").exists() else []:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))

def need(*keys):
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        sys.exit(f"missing in .env: {', '.join(missing)} (see .env.example)")

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
    req = urllib.request.Request(API, data=json.dumps({"query": query}).encode(), method="POST", headers={
        "Authorization": f"Bearer {os.environ[key]}", "Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read())
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
# public hosting: Cloudflare R2 (S3 API), served from MEDIA_BASE_URL

def r2():
    import boto3
    return boto3.client("s3", endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
                        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto")

def cleanup_r2(done, keep_days=2):
    """Delete videos published more than keep_days ago: keeps R2 inside the free 10 GB."""
    now = datetime.now(timezone.utc)
    s3, n = None, 0
    for cid, rec in done.items():
        if rec.get("deleted") or rec.get("draft") or "key" not in rec:
            continue                                  # drafts: Buffer still needs the file
        if now - datetime.fromisoformat(rec["due"]) > timedelta(days=keep_days):
            s3 = s3 or r2()
            s3.delete_object(Bucket=os.environ["R2_BUCKET"], Key=rec["key"])
            rec["deleted"] = now.isoformat(timespec="seconds")
            n += 1
    if n:
        save_json(PUBLISHED, done)
        log(f"R2: deleted {n} published videos")

def upload(path, key):
    r2().upload_file(str(path), os.environ["R2_BUCKET"], key, ExtraArgs={"ContentType": "video/mp4"})
    url = f"{os.environ['MEDIA_BASE_URL'].rstrip('/')}/{key}"
    # Buffer will fetch it later: make sure the world can read it now
    with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=30) as r:
        if r.status != 200:
            raise RuntimeError(f"{url} not public (HTTP {r.status})")
    return url

# -----------------------------------------------------------------------------
# what to post

def candidates():
    """Rendered shorts not yet published, best first: views (log) plus a bonus for freshness."""
    done = load_json(PUBLISHED, {})
    now = time.time()
    out = []
    for meta_file in OUT.glob("*/*/meta.json"):
        if meta_file.parts[-3].startswith("_"):
            continue                                 # _ukazka, _tmp
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
    today = datetime.now(TZ).date()
    day = today if a.den == "dnes" else date.fromisoformat(a.den) if a.den else today + timedelta(days=1)
    # slots still ahead of us (+20 min for Buffer to fetch the video)
    times = [t for t in slot_times(day, len(SLOTS)) if t > datetime.now(TZ) + timedelta(minutes=20)][: a.n]
    picks = candidates()[: len(times)]
    times = times[: len(picks)]
    chans = load_json(CHANNELS, [])
    mode = "ZKOUŠKA" if a.zkouska else ("NAOSTRO" if a.ostre else "koncepty")
    log(f"{day}: {len(picks)} shorts x {len(chans) or '?'} channels ({mode})")
    if not a.zkouska:
        load_env()
        need("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "MEDIA_BASE_URL")
        if not chans:
            sys.exit("no channels: run `publikuj.py kanaly` first")
    done = load_json(PUBLISHED, {})
    if not a.zkouska:
        cleanup_r2(done)
    for m, due in zip(picks, times):
        log(f"{due:%H:%M} {m['platform']}/{m['channel']} '{m.get('title', '')[:50]}' ({m.get('views')} views)")
        if a.zkouska:
            fake = {"id": "CHANNEL_ID", "service": "youtube"}
            print("   ", gql_value(post_input(fake, m, "https://media.example/clip.mp4", due, not a.ostre))[:400])
            continue
        key = f"{day:%Y%m%d}/{m['id']}.mp4"
        try:
            url = upload(m["short"], key)
        except Exception as e:
            log(f"  upload failed: {e}")
            continue
        posts = {}
        for ch in chans:
            try:
                posts[ch["id"]] = create_post(post_input(ch, m, url, due, not a.ostre), ch.get("key", "BUFFER_API_KEY"))
                log(f"  {ch['service']}: ok")
            except Exception as e:
                log(f"  {ch['service']}: FAILED {e}")
        done[m["id"]] = {"url": url, "key": key, "due": due.isoformat(), "draft": not a.ostre, "posts": posts}
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
