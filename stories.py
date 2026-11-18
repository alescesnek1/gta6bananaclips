"""
stories.py - Instagram (+ Facebook) stories for both accounts, fully on its own (GitHub Actions).

What Buffer can do (introspected 18. 11. 2026):
  instagram  metadata.instagram.type = story (image or video asset, IG Professional account) - OK
             (an IG story draft was accepted and deleted again in the test)
  facebook   metadata.facebook.type = story (PostTypeFacebook: post / reel / story) on our Pages
  tiktok, youtube, threads: no story type in Buffer's API -> nothing there
  Stickers (poll, question, link, music): NOT possible. InstagramStickerFieldsInput is only a text
  to copy by hand for reminder (notification) publishing - not autonomous. So a question is part of
  the picture and people answer by replying to the story (a DM).

Every run of denne (after publishing) calls `python stories.py den [--ostre]`. Per brand:
  REPOST       every reel that went out (publikovano*.json) -> the same Netlify mp4 as a story
               20-60 min after the reel, at most REPOST_MAX a publishing day. Scheduled only once the
               reel is (nearly) live, so a reel deleted before its slot is never reposted.
  INTERESTING  2-4 a day at varied times (never :00 / :30) in the AU/NZ, EU and US prime windows:
               question / fact / rate / guess frames built with Pillow over a real frame of one of
               our reels (facts from our own records only, never invented), plus rotating pool images
               (assets/stories/<brand>/, 20 per brand, never the same one within POOL_GAP_DAYS).
               Built once a publishing day (first run after 12:00 Prague) for BOTH brands, in ONE
               Netlify draft deploy (no credits), then scheduled in Buffer at their times.
Records: stories_state.json {brand: {"reposts": {clip: ...}, "days": {day: ...}, "pool": {file: day}}}.

  python stories.py den [--ostre] [--znacka bananas|fanpage]   # default: Buffer drafts
  python stories.py den --zkouska                               # plan + frames into out/_stories, no Buffer / Netlify
"""

import argparse
import json
import random
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import publikuj as P

HERE = P.HERE
STATE = HERE / "stories_state.json"
POOL_DIR = HERE / "assets" / "stories"
FONTS = HERE / "fonts" / "cap"
W, H = 1080, 1920

STORY_NETS = ("instagram",)   # 18. 11.: FB story was cross-shared to IG by Meta -> the same reel twice on the IG story
REPOST_DELAY = (20, 60)                      # min after the reel
REPOST_MAX = 6                               # reposts per publishing day per brand
REPOST_LEAD = 15                             # schedule once the reel is due within this many minutes
REPOST_MAX_AGE_H = 6                         # an older reel is not reposted any more
STORY_MAX_S = 60                             # Instagram story video limit
INTEREST_N = (2, 4)
POOL_GAP_DAYS = 5
MAX_FAILS = 2
# Prague time windows of the publishing day (12:00 .. 11:59 next day): EU evening, UK late / US east
# prime, US west evening, AU/NZ evening (= Prague morning).
WINDOWS = [("17:05", "19:55"), ("21:05", "23:55"), ("02:05", "04:55"), ("07:05", "09:55")]

BRAND_LOOK = {
    "bananas": {"font": "montserrat-900.ttf", "box": (255, 225, 53), "ink": (17, 17, 17),
                "accent": (17, 17, 17), "sub_box": (17, 17, 17), "sub_ink": (255, 255, 255), "top": 1040},
    "fanpage": {"font": "anton.ttf", "box": (17, 12, 20), "ink": (255, 79, 216),
                "accent": (255, 255, 255), "sub_box": (255, 79, 216), "sub_ink": (17, 12, 20), "top": 640},
}

# Copy: English, short, linted with SlopMonster deslop.py (5/5, 18. 11.). {a}/{b} = real streamer names,
# {n}/{avg} = real numbers from our records. Never anything that is not in the data.
COPY = {
    "bananas": {
        "question": ["Who was funnier yesterday?", "{a} or {b}", "Reply with a name"],
        "fact_top": ["Yesterday's top streamer", "{a}", "Banana score {avg}/10 from {n} clips"],
        "fact_most": ["Most clipped yesterday", "{a}", "{n} clips on our page"],
        "rate": ["Rate yesterday's #1 clip", "{a}", "Send us a number from 1 to 10"],
        "guess": ["Guess the banana score", "{a}'s clip from yesterday", "The answer is in the video on our page"],
    },
    "fanpage": {
        "question": ["Who made you laugh more?", "{a} or {b}", "Tell me in the replies"],
        "fact_most": ["Most clipped yesterday", "{a}", "{n} clips on our page"],
        "rate": ["Rate this one", "{a}, yesterday", "Send me a number, 1 to 10"],
    },
}


def log(msg):
    P.log(f"stories: {msg}")


def load_state():
    return P.load_json(STATE, {})


def save_state(st):
    P.save_json(STATE, st)


def bstate(st, znacka):
    b = st.setdefault(znacka, {})
    for k in ("reposts", "days", "pool"):
        b.setdefault(k, {})
    return b


def story_channels(znacka):
    """The brand's Buffer channels that take stories (instagram, facebook)."""
    cfg = P.brands_cfg().get(znacka) or {}
    chans = P.load_json(HERE / cfg.get("channels", "buffer.json"), [])
    return [c for c in chans if c.get("service", "").lower() in STORY_NETS and not c.get("paused")]


def ts(s):
    try:
        t = datetime.fromisoformat(s)
        return t if t.tzinfo else t.replace(tzinfo=P.TZ)
    except (TypeError, ValueError):
        return None


def off_grid_min(t):
    """Never on the full / half hour."""
    while t.minute in (0, 30):
        t += timedelta(minutes=1)
    return t


# -----------------------------------------------------------------------------
# Buffer input

def story_input(ch, asset, due, draft, alt=""):
    """asset: {"video": url} or {"image": url}."""
    service = ch["service"].lower()
    if "video" in asset:
        assets = [{"video": {"url": asset["video"]}}]
    else:
        assets = [{"image": {"url": asset["image"], "metadata": {"altText": alt or "GTA 6 story"}}}]
    inp = {"text": "", "channelId": ch["id"], "schedulingType": P.Enum("automatic"), "assets": assets}
    if draft:
        inp["mode"], inp["saveToDraft"] = P.Enum("addToQueue"), True
    else:
        inp["mode"] = P.Enum("customScheduled")
        inp["dueAt"] = due.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    if service == "instagram":
        inp["metadata"] = {"instagram": {"type": P.Enum("story"), "shouldShareToFeed": False}}
    elif service == "facebook":
        inp["metadata"] = {"facebook": {"type": P.Enum("story")}}
    return inp


def post_all(chans, asset, due, draft, posts, alt=""):
    """Create the story on every channel not in `posts` yet; {channel id: post id} grows in place.
    Returns the number of failures."""
    fails = 0
    for ch in chans:
        if ch["id"] in posts:
            continue
        try:
            posts[ch["id"]] = P.create_post(story_input(ch, asset, due, draft, alt), ch["key"])
        except Exception as e:
            fails += 1
            log(f"  {ch['service']}: {str(e)[:200]}")
    return fails


# -----------------------------------------------------------------------------
# REPOST: published reels -> stories

def video_seconds(url):
    """Length of a published mp4 (ffprobe reads only the header); None = unknown."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", url],
                           capture_output=True, text=True, timeout=25)
        return float(r.stdout.strip())
    except Exception:
        return None


def repost_delay(clip_id, znacka):
    return random.Random(f"{clip_id}:{znacka}:story").randint(*REPOST_DELAY)


def repost_due(rec, clip_id, znacka, now):
    due = ts(rec.get("due"))
    if not due:
        return None
    t = off_grid_min(due + timedelta(minutes=repost_delay(clip_id, znacka)))
    return t if t > now + timedelta(minutes=2) else P.fresh_due(t)


def reposts_on_day(bs, day):
    return sum(1 for r in bs["reposts"].values()
               if r.get("posts") and P.pub_day(ts(r["due"])) == day)


def repost_candidates(znacka, bs, now):
    """Reels of the brand that are (nearly) live and not reposted yet, oldest first."""
    out = []
    for cid, rec in P.records(znacka).items():
        if rec.get("deleted") or rec.get("nocount") or rec.get("draft") or not rec.get("url") or not rec.get("posts"):
            continue
        due = ts(rec.get("due"))
        if not due or due > now + timedelta(minutes=REPOST_LEAD) or due < now - timedelta(hours=REPOST_MAX_AGE_H):
            continue
        r = bs["reposts"].get(cid) or {}
        if r.get("done") or r.get("skip") or r.get("fails", 0) >= MAX_FAILS:
            continue
        out.append((due, cid, rec))
    return sorted(out, key=lambda x: x[0])


def do_reposts(znacka, bs, now, draft, dry):
    chans = story_channels(znacka)
    if not chans:
        return []
    made = []
    for _, cid, rec in repost_candidates(znacka, bs, now):
        due = repost_due(rec, cid, znacka, now)
        day = P.pub_day(due)
        if reposts_on_day(bs, day) >= REPOST_MAX:
            continue
        r = bs["reposts"].setdefault(cid, {"due": due.isoformat(), "posts": {}})
        secs = rec.get("dur") or video_seconds(rec["url"])
        if secs and secs > STORY_MAX_S:
            r["skip"] = f"{secs:.0f} s > {STORY_MAX_S} s"
            continue
        log(f"{znacka}: repost {cid} at {due:%d.%m. %H:%M}{' (draft)' if draft else ''}")
        if dry:
            bs["reposts"].pop(cid, None)
            made.append((cid, due))
            continue
        r["due"] = due.isoformat()
        fails = post_all(chans, {"video": rec["url"]}, due, draft, r["posts"], f"{P.display_name(rec)} GTA 6 clip")
        r["draft"] = draft
        if fails:
            r["fails"] = r.get("fails", 0) + 1
        else:
            r["done"] = True
        made.append((cid, due))
    return made


# -----------------------------------------------------------------------------
# INTERESTING: the day's plan

def day_times(day, znacka):
    """2-4 story times of the publishing day, one per window, minutes drawn per (day, brand), never :00 / :30."""
    rnd = random.Random(f"{day}:{znacka}:stories")
    n = rnd.randint(*INTEREST_N)
    wins = sorted(rnd.sample(range(len(WINDOWS)), n))
    out = []
    for i in wins:
        a, b = (datetime.strptime(x, "%H:%M") for x in WINDOWS[i])
        span = int((b - a).total_seconds() // 60)
        t = a + timedelta(minutes=rnd.randint(0, span))
        d = day + timedelta(days=1 if t.hour < 12 else 0)
        out.append(off_grid_min(datetime.combine(d, t.time(), P.TZ)))
    return out


def yesterday_stats(day, znacka):
    """The brand's own data of the previous publishing day ("on our page" = that account):
    {"clips": [rec...], "count": {channel: n}, "score": {channel: [ratings]}}. Live chat clips have only
    a proxy banana score -> counted for 'most clipped', left out of the banana score (as liga.py)."""
    prev = day - timedelta(days=1)
    clips = {}
    for f in P.rec_files(znacka):
        for cid, rec in P.load_json(f, {}).items():
            if rec.get("deleted") or rec.get("draft") or not rec.get("channel"):
                continue
            due = ts(rec.get("due"))
            if due and P.pub_day(due) == prev:
                clips.setdefault(cid, {**rec, "id": cid})
    count, score = {}, {}
    for cid, rec in clips.items():
        ch = rec["channel"].lower()
        count[ch] = count.get(ch, 0) + 1
        if rec.get("rating") and not (cid.startswith("live_") or rec.get("live")):
            score.setdefault(ch, []).append(rec["rating"])
    return {"clips": list(clips.values()), "count": count, "score": score}


def name(ch):
    return P.display_name({"channel": ch})


def fill(kind, znacka, stats):
    """(lines, frame clip) for a kind, or None when the data does not support it (never invented)."""
    copy = COPY[znacka].get(kind)
    if not copy:
        return None
    cnt, sc = stats["count"], stats["score"]
    ranked = sorted(cnt, key=lambda c: (-cnt[c], c))
    best_clip = max(stats["clips"], key=lambda r: (r.get("rating") or 0, r.get("views") or 0), default=None)
    def clip_of(ch):
        cs = [r for r in stats["clips"] if r["channel"].lower() == ch]
        return max(cs, key=lambda r: (r.get("rating") or 0, r.get("views") or 0)) if cs else None
    v = None
    if kind == "question" and len(ranked) >= 2:
        v = {"a": name(ranked[0]), "b": name(ranked[1])}, clip_of(ranked[0])
    elif kind == "fact_most" and ranked and cnt[ranked[0]] >= 2 and (len(ranked) < 2 or cnt[ranked[0]] > cnt[ranked[1]]):
        v = {"a": name(ranked[0]), "n": cnt[ranked[0]]}, clip_of(ranked[0])
    elif kind == "fact_top":
        ok = {c: s for c, s in sc.items() if len(s) >= 2}
        if ok:
            top = max(ok, key=lambda c: (sum(ok[c]) / len(ok[c]), len(ok[c])))
            v = {"a": name(top), "avg": f"{sum(ok[top]) / len(ok[top]):.1f}".rstrip("0").rstrip("."),
                 "n": len(ok[top])}, clip_of(top)
    elif kind in ("rate", "guess") and best_clip and best_clip.get("rating"):
        v = {"a": P.display_name(best_clip)}, best_clip
    if not v:
        return None
    vals, clip = v
    return [line.format(**vals) for line in copy], clip


def pool_files(znacka):
    d = POOL_DIR / znacka
    return sorted(p for p in d.glob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png")) if d.exists() else []


def pool_pick(znacka, bs, day):
    """A pool image not used in the last POOL_GAP_DAYS days: never used first, then the longest unused."""
    free = []
    for p in pool_files(znacka):
        last = bs["pool"].get(p.name)
        if last and (day - datetime.fromisoformat(last).date()).days < POOL_GAP_DAYS:
            continue
        free.append((last or "", random.Random(f"{day}:{p.name}").random(), p))
    return min(free)[2] if free else None


SAME = [{"fact_top", "fact_most"}, {"rate", "guess"}]       # one of each group a day (same data / clip)


def plan_day(day, znacka, bs, stats):
    """[{due, kind, lines?, clip?, pool?}] for the publishing day - each kind at most once."""
    times = day_times(day, znacka)
    rnd = random.Random(f"{day}:{znacka}:kinds")
    kinds = list(COPY[znacka]) + ["pool"]
    rnd.shuffle(kinds)
    pick = pool_pick(znacka, bs, day)
    if pick:                                   # the owner's images come first in the queue
        kinds.remove("pool")
        kinds.insert(rnd.randint(0, 1), "pool")
    items, used_kinds = [], set()
    for due in times:
        for k in kinds:
            if k in used_kinds or any(k in g and used_kinds & g for g in SAME):
                continue
            if k == "pool":
                if pick:
                    items.append({"due": due, "kind": "pool", "pool": pick})
                    used_kinds.add(k)
                    break
                continue
            f = fill(k, znacka, stats)
            if f:
                items.append({"due": due, "kind": k, "lines": f[0], "clip": f[1]})
                used_kinds.add(k)
                break
    return items


# -----------------------------------------------------------------------------
# frames (Pillow): a real frame of our reel, the text in solid brand boxes left-aligned in the lower
# third (no centred title on a gradient), the mascot / the model as a small cut-out. IG safe zones:
# nothing in the top 250 px or the bottom 340 px.

def grab_frame(url, times=(3.5, 6.0, 1.0, 9.0)):
    """A frame of a published reel (ffmpeg seeks by HTTP range, no full download); a black / flat frame
    (fade, flash) -> the next time. PIL image or None."""
    from PIL import Image, ImageStat
    import io
    for at in times:
        try:
            r = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(at), "-i", url, "-frames:v", "1",
                                "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True, timeout=60)
            if not r.stdout:
                continue
            img = Image.open(io.BytesIO(r.stdout)).convert("RGB")
            st = ImageStat.Stat(img.convert("L"))
            if 30 < st.mean[0] < 235 and st.stddev[0] > 20:
                return img
        except Exception:
            continue
    return None


def cover(img):
    from PIL import Image
    s = max(W / img.width, H / img.height)
    img = img.resize((round(img.width * s), round(img.height * s)), Image.LANCZOS)
    l, t = (img.width - W) // 2, (img.height - H) // 2
    return img.crop((l, t, l + W, t + H))


def fit_font(text, path, size, maxw):
    from PIL import ImageFont
    while size > 30:
        f = ImageFont.truetype(str(path), size)
        if f.getlength(text) <= maxw:
            return f
        size -= 4
    return ImageFont.truetype(str(path), size)


def mascot(znacka, seed):
    """bananas: the round profile mascot; fanpage: a reaction cut-out of the model (waist up)."""
    from PIL import Image, ImageDraw
    if znacka == "bananas":
        src = HERE / "assets" / "brand" / "pfp_bananas.webp"
        if not src.exists():
            return None
        im = Image.open(src).convert("RGB").resize((300, 300), Image.LANCZOS)
        mask = Image.new("L", (300, 300), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, 299, 299), fill=255)
        out = Image.new("RGBA", (312, 312), (0, 0, 0, 0))
        ImageDraw.Draw(out).ellipse((0, 0, 311, 311), fill=(255, 225, 53, 255))
        out.paste(im, (6, 6), mask)
        return out
    pngs = sorted((HERE / "assets" / "model_reakce").glob("*.png"))
    if not pngs:
        return None
    im = Image.open(random.Random(seed).choice(pngs)).convert("RGBA")
    im = im.crop((0, 0, im.width, int(im.height * 0.62)))         # head to waist
    s = 760 / im.height
    return im.resize((round(im.width * s), 760), Image.LANCZOS)


def build_frame(lines, znacka, bg, seed="x"):
    """lines = [headline, name/value, small line]. Returns a 1080x1920 RGB image."""
    from PIL import Image, ImageDraw, ImageEnhance
    look = BRAND_LOOK[znacka]
    font = FONTS / look["font"]
    img = cover(bg) if bg is not None else Image.new("RGB", (W, H), (20, 20, 24))
    img = ImageEnhance.Brightness(img).enhance(0.82)
    d = ImageDraw.Draw(img)
    x, maxw = 64, W - 64 - 96
    up = [lines[0].upper(), lines[1].upper(), lines[2]]
    f0 = fit_font(up[0], font, 74, maxw - 48)
    f1 = fit_font(up[1], font, 128, maxw - 48)
    f2 = fit_font(up[2], font, 46, maxw - 48)
    y = look["top"]
    for text, f, box, ink, pad in ((up[0], f0, look["sub_box"], look["sub_ink"], 18),
                                   (up[1], f1, look["box"], look["ink"], 24),
                                   (up[2], f2, look["sub_box"], look["sub_ink"], 16)):
        l, t, r, b = d.textbbox((0, 0), text, font=f)
        d.rectangle((x, y, x + (r - l) + 2 * pad, y + (b - t) + 2 * pad), fill=box)
        d.text((x + pad - l, y + pad - t), text, font=f, fill=ink)
        y += (b - t) + 2 * pad + 14
    m = mascot(znacka, seed)
    if m is not None:
        if znacka == "bananas":
            img.paste(m, (W - m.width - 56, 1040 - m.height - 30), m)
        else:
            img.paste(m, (W - m.width + 40, H - m.height), m)     # under the text, standing on the bottom edge
    return img


def pool_frame(path):
    from PIL import Image
    return cover(Image.open(path).convert("RGB"))


# -----------------------------------------------------------------------------
# run

def build_and_post(day, brands, st, now, draft, dry, deploy=None, grab=grab_frame):
    """Build every brand's interesting stories of `day` (not built yet) and schedule them. One Netlify
    deploy for all brands. dry: frames into out/_stories, nothing posted, the state untouched."""
    outdir = HERE / "out" / ("_stories" if dry else f"_stories_{day:%Y%m%d}")
    outdir.mkdir(parents=True, exist_ok=True)
    todo, files = [], {}
    for z in brands:
        bs = bstate(st, z)
        if (bs["days"].get(day.isoformat()) or {}).get("built"):
            continue
        P.set_brand(z)
        for i, it in enumerate(plan_day(day, z, bs, yesterday_stats(day, z))):
            if it["due"] < now + timedelta(minutes=5):
                continue                                   # built late: that window has passed
            if it["kind"] == "pool":
                img, alt = pool_frame(it["pool"]), "GTA 6 story"
            else:
                clip = it.get("clip") or {}
                bg = grab(clip["url"]) if clip.get("url") else None
                img, alt = build_frame(it["lines"], z, bg, f"{day}:{z}:{i}"), " ".join(it["lines"])
            rel = f"stories/{day:%Y%m%d}/{z}_{i}_{it['kind']}.jpg"
            path = outdir / Path(rel).name
            img.save(path, "JPEG", quality=90)
            files[rel] = str(path)
            todo.append((z, it, rel, alt))
            log(f"{z}: {it['kind']:<9} {it['due']:%d.%m. %H:%M}  {' | '.join(it.get('lines') or [it['pool'].name])}")
    if dry or not todo:
        return todo
    base = (deploy or (lambda fs: P.inbox_deploy(fs, b"[]", title=P.MEDIA_TITLE)))(files)
    for z, it, rel, alt in todo:
        bs = bstate(st, z)
        rec = bs["days"].setdefault(day.isoformat(), {"items": {}})
        url = f"{base.rstrip('/')}/{rel}"
        r = rec["items"].setdefault(rel, {"kind": it["kind"], "due": it["due"].isoformat(), "url": url,
                                          "alt": alt, "posts": {}})
        r["draft"] = draft
        if post_all(story_channels(z), {"image": url}, it["due"], draft, r["posts"], alt):
            r["fails"] = r.get("fails", 0) + 1
        if it["kind"] == "pool":
            bs["pool"][it["pool"].name] = day.isoformat()
    for z in brands:
        rec = bstate(st, z)["days"].setdefault(day.isoformat(), {"items": {}})
        rec["built"] = now.isoformat(timespec="minutes")
    return todo


def retry_failed(z, bs, now, draft):
    """A story whose post failed on some channel: try again next run (MAX_FAILS times, only while
    its time is still ahead)."""
    chans = story_channels(z)
    for rec in bs["days"].values():
        for r in (rec.get("items") or {}).values():
            due = ts(r.get("due"))
            if not r.get("fails") or r["fails"] >= MAX_FAILS or not due or due < now + timedelta(minutes=5):
                continue
            if all(c["id"] in r["posts"] for c in chans):
                continue
            if post_all(chans, {"image": r["url"]}, due, draft, r["posts"], r.get("alt", "")):
                r["fails"] += 1
            else:
                r.pop("fails")


def cmd_den(a):
    P.load_env()
    if a.cas:                                        # dry run of another moment (e.g. tomorrow 13:00)
        if not a.zkouska:
            sys.exit("--cas only with --zkouska")
        P.NOW = datetime.fromisoformat(a.cas).replace(tzinfo=P.TZ)
    now = P.now_tz()
    day = P.pub_day(now)
    brands = [a.znacka] if a.znacka else [z for z in ("bananas", "fanpage") if story_channels(z)]
    draft = not a.ostre
    st = load_state()
    for z in brands:
        P.set_brand(z)
        try:
            do_reposts(z, bstate(st, z), now, draft, a.zkouska)
            if not a.zkouska:
                retry_failed(z, bstate(st, z), now, draft)
        except Exception as e:                       # stories never break anything else
            log(f"{z}: reposts failed ({str(e)[:200]})")
    try:
        build_and_post(day, brands, st, now, draft, a.zkouska)
    except Exception as e:
        log(f"interesting stories failed ({str(e)[:200]})")
    if not a.zkouska:
        save_state(st)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("den", help="reposts + the day's stories")
    d.add_argument("--ostre", action="store_true", help="schedule for real (default: Buffer drafts)")
    d.add_argument("--zkouska", action="store_true", help="dry run: plan + frames, no Buffer / Netlify")
    d.add_argument("--znacka", choices=["bananas", "fanpage"], help="one brand only (default: both)")
    d.add_argument("--cas", help="with --zkouska: pretend it is this Prague time (2026-11-19T13:00)")
    a = ap.parse_args()
    {"den": cmd_den}[a.cmd](a)


if __name__ == "__main__":
    main()
