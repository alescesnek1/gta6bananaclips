"""
sit_data.py - what Buffer can't tell us, measured from a home connection (YouTube blocks GitHub's servers):

  python tools/sit_data.py            # -> zhlednuti.json (our YouTube / TikTok views) + konkurence.json

zhlednuti.json  {"at": iso, "youtube": {brand: [{id, title, views, at}]}, "tiktok": {brand: [...]}}
                Buffer's post metrics report 0 for YouTube and TikTok (18. 11.), so the monitor and the
                cloud routine read the real public counts from here.
konkurence.json {"at": iso, "twitch_streams": [...], "twitch_clips": [...], "youtube_today": [...]}
                the GTA VI category right now (Helix: live streams, top viewer clips of the last 12 h)
                and today's most viewed GTA 6 uploads on YouTube (search pages, upload date = today).

Read-only everywhere (public pages, Twitch app token). Needs yt-dlp on PATH and TWITCH_CLIENT_ID /
TWITCH_CLIENT_SECRET in .env. Never raises: a source that fails is just missing from the file.
"""
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
HANDLES = {"bananas": "gta6bananaclips", "fanpage": "gta6fanpageclip"}
YT_QUERIES = ["gta 6", "gta 6 funny", "gta 6 kai cenat", "gta 6 ishowspeed", "gta 6 xqc", "gta 6 clips"]


def env():
    out = dict(os.environ)
    p = HERE / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out.setdefault(k.strip(), v.strip().strip('"'))
    return out


def ytdlp(args, timeout=180):
    try:
        r = subprocess.run(["yt-dlp", "--no-warnings", *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return [line for line in r.stdout.splitlines() if line.strip()]
    except Exception as e:
        print(f"yt-dlp: {e}", file=sys.stderr)
        return []


def num(x):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return None


def our_youtube(handle, n=10):
    """The newest n shorts with their public view counts (the flat list has no counts for new ones)."""
    ids = ytdlp(["--flat-playlist", "--playlist-end", str(n), "--print", "%(id)s",
                 f"https://www.youtube.com/@{handle}/shorts"])
    out = []
    for vid in ids:
        line = ytdlp(["--skip-download", "--print", "%(view_count)s\t%(timestamp)s\t%(title)s",
                      f"https://www.youtube.com/shorts/{vid}"], timeout=90)
        if line:
            v, t, title = (line[0].split("\t") + ["", "", ""])[:3]
            out.append({"id": vid, "title": title, "views": num(v), "at": num(t)})
    return out


def our_tiktok(handle, n=15):
    out = []
    for line in ytdlp(["--flat-playlist", "--playlist-end", str(n), "--print",
                       "%(id)s\t%(view_count)s\t%(timestamp)s\t%(title).80s", f"https://www.tiktok.com/@{handle}"]):
        vid, v, t, title = (line.split("\t") + ["", "", "", ""])[:4]
        out.append({"id": vid, "title": title, "views": num(v), "at": num(t)})
    return out


def twitch(e):
    cid, sec = e.get("TWITCH_CLIENT_ID"), e.get("TWITCH_CLIENT_SECRET")
    if not (cid and sec):
        return {}
    tok = json.load(urllib.request.urlopen(urllib.request.Request(
        "https://id.twitch.tv/oauth2/token", data=urllib.parse.urlencode(
            {"client_id": cid, "client_secret": sec, "grant_type": "client_credentials"}).encode()), timeout=30))
    hdr = {"Client-ID": cid, "Authorization": "Bearer " + tok["access_token"]}

    def get(path, **q):
        url = "https://api.twitch.tv/helix/" + path + "?" + urllib.parse.urlencode(q, doseq=True)
        return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=30))["data"]

    game = get("games", name="Grand Theft Auto VI")[0]["id"]
    start = (datetime.now(timezone.utc) - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
    streams = [{"login": s["user_login"], "viewers": s["viewer_count"], "lang": s["language"], "title": s["title"][:90]}
               for s in get("streams", game_id=game, first=25)]
    clips = [{"login": c["broadcaster_name"], "views": c["view_count"], "dur": c["duration"], "title": c["title"][:90],
              "at": c["created_at"], "url": c["url"]} for c in get("clips", game_id=game, started_at=start, first=40)]
    return {"twitch_streams": streams, "twitch_clips": sorted(clips, key=lambda c: -c["views"])[:25]}


def youtube_today():
    seen, out = set(), []
    for q in YT_QUERIES:              # sp=EgQIAhAB: upload date today, type video
        url = f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(q)}&sp=EgQIAhAB"
        for line in ytdlp(["--flat-playlist", "--playlist-end", "20", "--print",
                           "%(id)s\t%(view_count)s\t%(duration)s\t%(channel)s\t%(title).90s", url]):
            vid, v, d, ch, title = (line.split("\t") + [""] * 5)[:5]
            if vid in seen:
                continue
            seen.add(vid)
            out.append({"id": vid, "views": num(v), "dur": num(d), "channel": ch, "title": title})
    out = [x for x in out if x["views"] is not None]
    return sorted(out, key=lambda x: -x["views"])[:30]


def main():
    e = env()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    views = {"at": now, "youtube": {}, "tiktok": {}}
    for brand, h in HANDLES.items():
        views["youtube"][brand] = our_youtube(h)
        views["tiktok"][brand] = our_tiktok(h)
    (HERE / "zhlednuti.json").write_text(json.dumps(views, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    comp = {"at": now}
    try:
        comp.update(twitch(e))
    except Exception as ex:
        print(f"twitch: {ex}", file=sys.stderr)
    comp["youtube_today"] = youtube_today()
    (HERE / "konkurence.json").write_text(json.dumps(comp, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    yt = sum(x["views"] or 0 for b in views["youtube"].values() for x in b)
    tt = sum(x["views"] or 0 for b in views["tiktok"].values() for x in b)
    print(f"zhlednuti: youtube {yt}, tiktok {tt}; konkurence: {len(comp.get('twitch_clips', []))} twitch klipu, "
          f"{len(comp['youtube_today'])} youtube videi")


if __name__ == "__main__":
    main()
