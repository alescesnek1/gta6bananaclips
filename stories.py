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
               Every story asks for a reply (people answer by DM, which lifts the story's reach); every
               2nd-3rd pool story carries the follow line instead. Text never over a face or a chest.
  LONG-FORM    the brand's new long-form video (long_episodes.json _last, committed by long.yml after the
               e-mail went out) -> one story "NEW: <title>" + "Full video on YouTube, link in bio",
               PROMO_AFTER_MIN after the e-mail's publish time. The clickable version (a link sticker, not in
               any API) is link_story(): posli_mail.py attaches it to the long-form e-mail for Aleš.
               Needs once: the Instagram bio link of each account = its YouTube channel.
Records: stories_state.json {brand: {"reposts": {clip: ...}, "days": {day: ...}, "pool": {file: day},
                                     "long": {long-form key: ...}}}.

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
# {n}/{avg} = real numbers from our records. Never anything that is not in the data. The last line of
# every kind asks for a reply (REPLY_WORDS, Aleš 18. 11.: stories must make people react).
COPY = {
    "bananas": {
        "question": ["Who was funnier yesterday?", "{a} or {b}", "Reply with a name"],
        "fact_top": ["Yesterday's top streamer", "{a}", "Banana score {avg}/10 from {n} clips", "Agree? Reply yes or no"],
        "fact_most": ["Most clipped yesterday", "{a}", "{n} clips on our page", "Who beats them today? Reply with a name"],
        "rate": ["Rate yesterday's #1 clip", "{a}", "Reply with a number from 1 to 10"],
        "guess": ["Guess the banana score", "{a}'s clip from yesterday", "Reply with your guess, 1 to 10"],
    },
    "fanpage": {
        "question": ["Who made you laugh more?", "{a} or {b}", "Tell me in the replies"],
        "fact_most": ["Most clipped yesterday", "{a}", "{n} clips on our page", "Who's next? Tell me"],
        "rate": ["Rate this one", "{a}, yesterday", "Send me a number, 1 to 10"],
    },
}
REPLY_WORDS = ("reply", "tell me", "send me")


def log(msg):
    P.log(f"stories: {msg}")


def load_state():
    return P.load_json(STATE, {})


def save_state(st):
    P.save_json(STATE, st)


def merge_state(local, remote):
    """Deep merge of two stories_state.json dicts: every key of either side stays; on a clash the remote
    (origin) value wins, nested dicts merge key by key - so a story origin already has (its posts, its
    done flag) counts as done, and posts both sides made are all kept."""
    out = dict(local)
    for k, v in remote.items():
        out[k] = merge_state(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def sync_state(st):
    """Under the publishing lock (GitHub runs): the checkout's stories_state.json can be stale (an earlier
    run pushed after this one checked out, 18. 11.: the same reel reposted twice) -> merge origin/main's
    state in before deciding anything. Read-only for git; keeps the local state when git fails."""
    if not (HERE / P.LOCK_UNTIL).exists():
        return st                                    # no lock in use (local runs, tests)
    if P.git("fetch", "-q", "origin", "main")[0]:
        log("git fetch failed, using the local state")
        return st
    rc, out, _ = P.git("show", f"origin/main:{STATE.name}")
    if rc != 0 or not out:
        return st
    try:
        remote = json.loads(out)
    except json.JSONDecodeError:
        return st
    return merge_state(st, remote) if isinstance(remote, dict) else st


def bstate(st, znacka):
    b = st.setdefault(znacka, {})
    for k in ("reposts", "days", "pool", "long"):
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


def repost_candidates(znacka, bs, now, chans=None):
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
        if chans and all(c["id"] in (r.get("posts") or {}) for c in chans):
            continue                                 # every story channel has this reel already
        out.append((due, cid, rec))
    return sorted(out, key=lambda x: x[0])


def do_reposts(znacka, bs, now, draft, dry):
    chans = story_channels(znacka)
    if not chans:
        return []
    made = []
    for _, cid, rec in repost_candidates(znacka, bs, now, chans):
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
# frames (Pillow): a real frame of our reel, the text in solid brand boxes left-aligned (no centred title
# on a gradient), the mascot / the model as a small cut-out. The text goes into the calmest band that
# touches no face and no chest (protected_boxes) inside the IG safe zone: nothing in the top SAFE_TOP px
# (profile row) or the bottom SAFE_BOTTOM px (reply bar). No safe place = no text (pool) / no story.

SAFE_TOP, SAFE_BOTTOM = 250, 340


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


def missing_glyphs(text, path):
    """Characters of `text` the font cannot draw (they would come out as tofu boxes, e.g. emoji)."""
    from PIL import ImageFont
    f = ImageFont.truetype(str(path), 60)
    tofu = bytes(f.getmask(""))                         # private use area: never in our fonts
    return sorted({ch for ch in text if not ch.isspace() and (ord(ch) > 0xFFFF or bytes(f.getmask(ch)) == tofu)})


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
    s = 640 / im.height
    return im.resize((round(im.width * s), 640), Image.LANCZOS)


def pane_split(img):
    """y of the seam between the facecam pane and the game in our reels (a strong full-width horizontal
    edge between y 300 and 1200), or None."""
    import numpy as np
    g = np.asarray(img.convert("L"), dtype=np.float32)
    rows = np.abs(g[1:] - g[:-1]).mean(axis=1)
    i = int(np.argmax(rows[300:1200])) + 300
    return i + 1 if rows[i] > max(12.0, 5 * float(np.median(rows))) else None


SEAM_PAD = 130


def protected_boxes(img, reel=False):
    """Face and chest areas the story text must never cover (Aleš 18. 11.): OpenCV Haar faces (real and
    painted faces both get a chance, frontal + profile); chest = below each face, ~2.6 face heights,
    1.9 face widths wide. reel=True (a frame of our reel): the whole facecam pane down to the seam plus
    SEAM_PAD (our captions sit on the seam) is protected even when Haar misses the face (headphones,
    looking down); a face's chest box stops at the seam (the game below is not the streamer's body).
    Returns [(x0, y0, x1, y1)] in 1080x1920 pixels."""
    import cv2
    import numpy as np
    g = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2GRAY)
    split = pane_split(img) if reel else None
    out = [(0.0, 0.0, float(W), float(split + SEAM_PAD))] if split else []     # the facecam pane + captions on the seam
    for name in ("haarcascade_frontalface_default.xml", "haarcascade_profileface.xml"):
        cas = cv2.CascadeClassifier(cv2.data.haarcascades + name)
        for (x, y, w, h) in cas.detectMultiScale(g, 1.1, 5, minSize=(90, 90)):
            x, y, w, h = float(x), float(y), float(w), float(h)
            face = (x - 0.25 * w, y - 0.35 * h, x + 1.25 * w, y + 1.15 * h)                    # face + hair
            chest = (x - 0.45 * w, y + h, x + 1.45 * w, y + h + 2.6 * h)
            if split and y + h / 2 < split:
                face = (0.0, 0.0, float(W), float(split))
                chest = (chest[0], chest[1], chest[2], float(split))
            out += [face, chest]
    return out


def free_band(img, boxes, band_h, x0=0, x1=W):
    """y of the band (band_h tall, columns x0..x1) for the text inside the IG safe zone: never touching a
    protected box; among the allowed ones the calmest (lowest edge density, so no subject behind it)."""
    import cv2
    import numpy as np
    edges = cv2.Canny(cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2GRAY), 80, 160)
    best = None
    for y in range(SAFE_TOP, H - SAFE_BOTTOM - band_h + 1, 20):
        if any(not (y + band_h < b[1] or y > b[3] or x1 < b[0] or x0 > b[2]) for b in boxes):
            continue
        busy = float(edges[y:y + band_h, x0:x1].mean())
        if best is None or busy < best[0]:
            best = (busy, y)
    return None if best is None else best[1]


def wrap(text, font, maxw):
    from cenzura import cenzura                       # 18. 11.: swears / sexual words starred
    text = cenzura(text)
    lines, cur = [], ""
    for word in text.split():
        t = f"{cur} {word}".strip()
        if cur and font.getlength(t) > maxw:
            lines.append(cur)
            cur = word
        else:
            cur = t
    return lines + ([cur] if cur else [])


ROW = {"head": (64, 44, 1, 18), "main": (104, 60, 3, 22), "sub": (46, 34, 1, 16)}   # size, min size, lines, pad
X0, MAXW, GAP = 64, W - 2 * 64, 12
TEXT_SCALES = (1.0, 0.8, 0.64)
REF = "HÁgy"                                               # line height reference (caps, accents, descenders)


def layout(rows, look, scale=1.0):
    """rows [(text, "head"|"main"|"sub")] -> [(line, font, box colour, ink, pad)] + the block height.
    A row shrinks until it fits its number of lines (head / sub: one line, wrapped only at the minimum
    size); head / sub rows in the small boxes, main in the big."""
    from PIL import ImageFont
    font = FONTS / look["font"]
    out = []
    for text, kind in rows:
        size, small, nmax, pad = (round(v * scale) if i != 2 else v for i, v in enumerate(ROW[kind]))
        text = text if kind == "sub" else text.upper()
        while True:
            f = ImageFont.truetype(str(font), size)
            lines = wrap(text, f, MAXW - 2 * pad)
            if size <= small or (len(lines) <= nmax and all(f.getlength(l) <= MAXW - 2 * pad for l in lines)):
                break
            size -= 4
        box, ink = (look["box"], look["ink"]) if kind == "main" else (look["sub_box"], look["sub_ink"])
        out += [(l, f, box, ink, pad) for l in lines]
    height = sum(line_h(f) + 2 * pad + GAP for _, f, _, _, pad in out) - GAP
    return out, height


def line_h(f):
    _, t, _, b = f.getbbox(REF)
    return b - t


def draw_rows(img, laid, y):
    """Draw the laid-out lines from y down; returns the right edge of the widest box."""
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    right = X0
    for text, f, box, ink, pad in laid:
        l, _, r, _ = d.textbbox((0, 0), text, font=f)
        top = f.getbbox(REF)[1]
        d.rectangle((X0, y, X0 + (r - l) + 2 * pad, y + line_h(f) + 2 * pad), fill=box)
        d.text((X0 + pad - l, y + pad - top), text, font=f, fill=ink)
        right = max(right, X0 + (r - l) + 2 * pad)
        y += line_h(f) + 2 * pad + GAP
    return right


def place_text(img, rows, znacka, reel=False, reserve=0):
    """Draw the rows where they cover no face / chest. reserve = free px kept right under the text (the
    link sticker of the manual story). Returns (text box (x0, y0, x1, y1), top of the reserved area or
    None) or None when there is no such place (nothing drawn)."""
    look = BRAND_LOOK.get(znacka) or BRAND_LOOK["bananas"]
    boxes = protected_boxes(img, reel)
    for scale in TEXT_SCALES:                              # smaller text before no text
        laid, hb = layout(rows, look, scale)
        y = free_band(img, boxes, hb + reserve, X0, W - X0)
        if y is not None:
            break
    else:
        return None
    right = draw_rows(img, laid, y)
    return (X0, y, right, y + hb), (y + hb + 24 if reserve else None)


def paste_mascot(img, znacka, seed, text_box, reel=False):
    """The mascot / the model only where it touches neither the text nor a face (else left out)."""
    m = mascot(znacka, seed)
    if m is None:
        return None
    boxes = protected_boxes(img, reel) + [tuple(text_box)]
    spots = ([(W - m.width - 56, text_box[1] - m.height - 24), (W - m.width - 56, text_box[3] + 24)]
             if znacka == "bananas" else [(W - m.width + 40, H - m.height)])        # the model on the bottom edge
    for x, y in spots:
        r = (x, y, x + m.width, y + m.height)
        if y < SAFE_TOP or (znacka == "bananas" and r[3] > H - SAFE_BOTTOM):
            continue
        if any(not (r[2] < b[0] or r[0] > b[2] or r[3] < b[1] or r[1] > b[3]) for b in boxes):
            continue
        img.paste(m, (x, y), m)
        return r
    return None


def build_frame(lines, znacka, bg, seed="x"):
    """lines = [headline, name/value, small line(s)...]. A 1080x1920 RGB image, or None when no place for
    the text is free of faces / chests (the story is then left out)."""
    from PIL import Image, ImageEnhance
    img = cover(bg) if bg is not None else Image.new("RGB", (W, H), (20, 20, 24))
    img = ImageEnhance.Brightness(img).enhance(0.82)
    rows = [(lines[0], "head"), (lines[1], "main")] + [(l, "sub") for l in lines[2:]]
    placed = place_text(img, rows, znacka, reel=bg is not None)
    if not placed:
        return None
    paste_mascot(img, znacka, seed, placed[0], reel=bg is not None)
    return img


# Pool lines (Aleš 18. 11.: stories must make people react): they ask for a reply (people answer by DM,
# which lifts the story's reach); on FOLLOW_DAYS the follow line instead. SlopMonster deslop 5/5.
POOL_TEXT = {
    "bananas": ["Reply with the streamer you want next", "Kai or Speed? Reply with one name",
                "Rate today's clips 1-10 in a reply", "Playing GTA 6 tonight? Reply yes or no",
                "Reply with the funniest clip you saw today", "Which streamer wins GTA 6 today? Reply with a name"],
    "fanpage": ["Tell me who to watch tonight", "Reply with your GTA 6 main", "Who keeps you up tonight? Tell me",
                "Chaos or chill tonight? Reply with one word", "Up late in Vice City? Tell me why",
                "Send me the streamer you'd stay up for"],
}
FOLLOW_TEXT = {"bananas": "New GTA 6 clips every day. Follow", "fanpage": "New GTA 6 clips every night. Follow"}
FOLLOW_DAYS = (1, 3)                 # day.toordinal() % 5 in these -> the follow line (2 of 5 = every 2nd-3rd)


def pool_line(znacka, path_name, seed):
    """The pool story's line: the follow line on FOLLOW_DAYS, else a reply line (rotates per image + day)."""
    try:
        day = datetime.fromisoformat(str(seed)).date()
    except ValueError:
        day = None
    if day and day.toordinal() % 5 in FOLLOW_DAYS:
        return FOLLOW_TEXT.get(znacka) or FOLLOW_TEXT["bananas"]
    return random.Random(f"{path_name}:{seed}").choice(POOL_TEXT.get(znacka) or POOL_TEXT["bananas"])


def pool_frame(path, znacka="bananas", seed=""):
    """A pool image with one short line, placed where it covers no face and no chest."""
    from PIL import Image
    img = cover(Image.open(path).convert("RGB"))
    if not place_text(img, [(pool_line(znacka, path.name, seed), "main")], znacka):
        log(f"pool {path.name}: no text area free of face/chest, posted without text")
    return img


# -----------------------------------------------------------------------------
# LONG-FORM PROMO: a new long-form video on YouTube -> stories that send people there.
# Link stickers are not in Buffer's API (only manual reminder publishing), so (Aleš 18. 11.):
#   automatic  "NEW: <title>" + "Full video on YouTube, link in bio", PROMO_AFTER_MIN after the publish
#              time of the e-mail (a manual link story, if Aleš posts one, comes first). Built from what
#              long.yml commits: long_episodes.json _last (series, build start) + the episode number,
#              long_used.json (its moments -> a frame of one of our reels with them, else a pool image).
#   manual     link_story(meta): the same look with room for the link sticker, attached to the long-form
#              e-mail by posli_mail.py (story_odkaz.jpg + a 30 s step for Aleš).

LONG_EPISODES = HERE / "long_episodes.json"
LONG_USED = HERE / "long_used.json"
PROMO_AFTER_MIN = 120
PROMO_JITTER = (3, 25)
PROMO_MAX_LATE_H = 6                 # later than this after the publish time: no promo any more
LONG_BUILD_MIN = (10, 30, 60, 90, 120)   # long.yml: _last.at = build start, the e-mail's slot is taken after
                                         # the build -> the latest slot any of these build times gives (the
                                         # promo may come late, never before the video is out)
PROMO_LINE = "Full video on YouTube, link in bio"
PROMO_RETRY_H = 3                    # a failed promo whose time passed: retried this long after it


def short_title(title):
    """The long-form title without the streamer list: 'GTA 6 Funniest Moments #3 (Kai Cenat, xQc)' ->
    'GTA 6 Funniest Moments #3'."""
    t = re.sub(r"\s*\([^()]*\)\s*$", "", str(title or "")).strip(" -:")
    return t or "New GTA 6 video"


def long_latest(znacka):
    """The brand's latest long-form e-mailed for upload (long.yml commits _last only after the e-mail went
    out; a failed e-mail rolls it back): {key, serie, n, at, publish, title, moments} or None."""
    eps = P.load_json(LONG_EPISODES, {})
    last = (eps.get("_last") or {}).get(znacka) or {}
    at, s = ts(last.get("at")), last.get("serie")
    if not at or not s:
        return None
    n = (eps.get(znacka) or {}).get(s)
    ids = [i for i, u in P.load_json(LONG_USED, {}).items()
           if isinstance(u, dict) and u.get("znacka") == znacka and u.get("serie") == s
           and u.get("datum") == str(at.date()) and not u.get("reserve")]
    recs = P.records(znacka)
    moments = [recs[i] for i in ids if i in recs and recs[i].get("channel")]
    names = []
    for r in sorted(moments, key=lambda r: -(r.get("views") or 0)):
        if P.display_name(r) not in names:
            names.append(P.display_name(r))
    try:
        import kompilace as KP
        publish = max(KP.asap_slot(znacka, s, at + timedelta(minutes=b)) for b in LONG_BUILD_MIN)
        t = KP.SERIES.get(KP.series_key(s), {}).get("title") or ""
        t = t.get(znacka, "") if isinstance(t, dict) else t
    except Exception as e:                                # no kompilace: a safe guess and a generic title
        log(f"{znacka}: long-form promo without kompilace ({str(e)[:120]})")
        publish, t = off_grid_min(at + timedelta(minutes=max(LONG_BUILD_MIN) + 46)), ""
    lead = names[0] if names else ""
    if not lead:
        t = re.sub(r"^\{lead\}(?:'s| in)? ", "", t)
    k = len(ids) if s == "top-week" else (len(names) if moments and len(moments) == len(ids) else "Several")
    title = short_title(t.format(n=n or "", who="", lead=lead, k=k)) if t else ""
    title = re.sub(r"\s*#(?=\s|$)", "", title).strip(" -:") or "New GTA 6 video"
    return {"key": f"{s}#{n or 0}@{at:%Y%m%d%H%M}", "serie": s, "n": n, "at": at, "publish": publish,
            "title": title, "moments": moments}


def promo_due(info, znacka):
    rnd = random.Random(f"{info['key']}:{znacka}:promo")
    return off_grid_min(info["publish"] + timedelta(minutes=PROMO_AFTER_MIN + rnd.randint(*PROMO_JITTER)))


def promo_frame(rows, znacka, bg, reel, seed, reserve=0):
    """(image, placed) or (None, None): the long-form's frame -> a pool image -> plain dark."""
    from PIL import Image, ImageEnhance
    cands = [(bg, reel)] if bg is not None else []
    pool = pool_files(znacka)
    if pool:
        cands.append((Image.open(random.Random(seed).choice(pool)).convert("RGB"), False))
    cands.append((None, False))
    for src, is_reel in cands:
        img = (ImageEnhance.Brightness(cover(src)).enhance(0.85) if src is not None
               else Image.new("RGB", (W, H), (20, 20, 24)))
        placed = place_text(img, rows, znacka, reel=is_reel, reserve=reserve)
        if placed:
            return img, placed
    return None, None


def do_long_promo(znacka, bs, now, draft, dry, deploy=None, grab=None):
    """Schedule the automatic promo story of the brand's latest long-form (once per long-form). Returns
    its due time or None."""
    info = long_latest(znacka)
    if not info or info["key"] in bs["long"]:
        return None
    chans = story_channels(znacka)
    if not chans:
        return None
    if now > info["publish"] + timedelta(hours=PROMO_MAX_LATE_H):
        if not dry:
            bs["long"][info["key"]] = {"skip": "too late", "title": info["title"]}
        return None
    due = promo_due(info, znacka)
    due = due if due > now + timedelta(minutes=2) else P.fresh_due(due)
    best = max(info["moments"], key=lambda r: (r.get("rating") or 0, r.get("views") or 0), default=None)
    bg = (grab or grab_frame)(best["url"]) if best and best.get("url") else None
    img, _ = promo_frame([(f"NEW: {info['title']}", "main"), (PROMO_LINE, "sub")], znacka, bg, True, info["key"])
    if img is None:
        log(f"{znacka}: long-form promo {info['key']}: no text area free of face/chest, left out")
        return None
    day = P.pub_day(due)
    outdir = HERE / "out" / ("_stories" if dry else f"_stories_{day:%Y%m%d}")
    outdir.mkdir(parents=True, exist_ok=True)
    rel = f"stories/{day:%Y%m%d}/{znacka}_long_{info['at']:%m%d%H%M}.jpg"
    path = outdir / Path(rel).name
    img.save(path, "JPEG", quality=90)
    log(f"{znacka}: long-form promo '{info['title']}' at {due:%d.%m. %H:%M} (publish {info['publish']:%d.%m. %H:%M})"
        f"{' (draft)' if draft else ''}")
    if dry:
        return due
    base = (deploy or (lambda fs: P.inbox_deploy(fs, b"[]", title=P.MEDIA_TITLE)))({rel: str(path)})
    url = f"{base.rstrip('/')}/{rel}"
    alt = f"New GTA 6 video on YouTube: {info['title']}"
    r = bs["long"].setdefault(info["key"], {"title": info["title"], "due": due.isoformat(), "url": url,
                                            "alt": alt, "posts": {}})
    r["draft"] = draft
    if post_all(chans, {"image": url}, due, draft, r["posts"], alt):
        r["fails"] = r.get("fails", 0) + 1
    else:
        r["done"] = True
    return due


def card_story(frame, rows, znacka):
    """A 16:9 frame (the long-form's payoff) as a full-width card on the brand's dark colour: the text
    above it on the plain colour (so never over a face or a chest), the free area for the link sticker
    under it (CARD_Y + card .. H - SAFE_BOTTOM)."""
    from PIL import Image
    look = BRAND_LOOK.get(znacka) or BRAND_LOOK["bananas"]
    dark = look["box"] if znacka == "fanpage" else look["sub_box"]
    img = Image.new("RGB", (W, H), dark)
    ch = round(frame.height * W / frame.width)
    img.paste(frame.resize((W, ch), Image.LANCZOS), (0, CARD_Y))
    for scale in TEXT_SCALES:                              # the whole block above the card
        laid, hb = layout(rows, look, scale)
        if hb <= CARD_Y - 40 - SAFE_TOP:
            break
    draw_rows(img, laid, max(SAFE_TOP, CARD_Y - 40 - hb))
    return img


CARD_Y = 740


def link_story(meta, out=None):
    """The ready-made story for the manual link sticker (posli_mail.py attaches it): the long-form's
    payoff frame (nahled_vstup/frame.jpg) with "NEW VIDEO" + the short title and a free area for the
    sticker away from faces. Returns (path, where the sticker goes: nahoru / doprostřed / dolů) or None."""
    from PIL import Image
    z = "fanpage" if meta.get("znacka") == "fanpage" else "bananas"
    d = Path(meta["nahled_vstup"]) if meta.get("nahled_vstup") else None
    bg = Image.open(d / "frame.jpg").convert("RGB") if d and (d / "frame.jpg").exists() else None
    rows = [("NEW VIDEO", "head"), (short_title(meta.get("title")), "main")]
    if bg is not None and bg.width > bg.height:            # the 16:9 payoff frame whole, as a card
        img, where = card_story(bg, rows, z), "dolů (pod obrázek)"
    else:
        img, placed = promo_frame(rows, z, bg, False, str(meta.get("title")), reserve=300)
        if img is None:
            return None
        sticker = placed[1] + 130                          # the middle of the reserved area
        where = "nahoru" if sticker < 760 else ("doprostřed" if sticker < 1220 else "dolů")
    video = Path(meta.get("video") or (HERE / "out" / "_long" / z / "long.mp4"))
    path = Path(out) if out else video.with_name(video.stem + "_story_odkaz.jpg")
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=90)
    return path, where


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
                img, alt = pool_frame(it["pool"], z, day.isoformat()), "GTA 6 story"
            else:
                clip = it.get("clip") or {}
                bg = grab(clip["url"]) if clip.get("url") else None
                img, alt = build_frame(it["lines"], z, bg, f"{day}:{z}:{i}"), " ".join(it["lines"])
                if img is None:                            # no place free of faces / chests: left out
                    log(f"{z}: {it['kind']} left out (no text area free of face/chest)")
                    continue
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
    its time is still ahead; a long-form promo whose time passed gets a fresh time within PROMO_RETRY_H)."""
    chans = story_channels(z)
    items = [(r, False) for rec in bs["days"].values() for r in (rec.get("items") or {}).values()]
    items += [(r, True) for r in bs.get("long", {}).values() if r.get("url")]   # the long-form promo too
    for r, promo in items:
        due = ts(r.get("due"))
        if not r.get("fails") or r["fails"] >= MAX_FAILS or not due:
            continue
        if due < now + timedelta(minutes=5):
            if not (promo and due > now - timedelta(hours=PROMO_RETRY_H)):
                continue
            due = P.fresh_due(due)                     # the promo still makes sense a bit later
            r["due"] = due.isoformat()
        if all(c["id"] in r["posts"] for c in chans):
            continue
        if post_all(chans, {"image": r["url"]}, due, draft, r["posts"], r.get("alt", "")):
            r["fails"] += 1
        else:
            r.pop("fails")
            r["done"] = True


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
    st = load_state() if a.zkouska else sync_state(load_state())
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
    for z in brands:
        P.set_brand(z)
        try:
            do_long_promo(z, bstate(st, z), now, draft, a.zkouska)
        except Exception as e:
            log(f"{z}: long-form promo failed ({str(e)[:200]})")
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
