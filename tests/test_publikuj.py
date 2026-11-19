"""
Regression tests from the pre-launch audit (17. 11.) and the just-in-time scheduling, hot path, live
fast path and publishing lock (18. 11.): no network, nothing published - Buffer / Netlify are mocked,
whole days are simulated run by run (every 30 min, every 5 min on launch days).
  .venv/Scripts/python -m unittest tests.test_publikuj
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

# quotas from the live config (18. 11.: 8 per day from the gameplay, fanpage max 10)
B = json.loads((Path(__file__).resolve().parents[1] / "brands.json").read_text(encoding="utf-8"))
def QUOTA(z, day="2026-11-19"):
    return [k for d, k in sorted(B[z]["ramp"].items()) if d <= day][-1]


HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import publikuj as P                                       # noqa: E402


class HotPath(unittest.TestCase):
    def test_hot_post_is_not_a_draft(self):
        """shareNow used to carry saveToDraft=True -> the 'hot' post would have landed as a Buffer draft."""
        P.set_brand("bananas")
        m = {"id": "x", "channel": "kaicenat", "platform": "twitch", "now": True}
        due = datetime.now(P.TZ) + timedelta(hours=1)
        inp = P.post_input({"id": "c", "service": "tiktok"}, m, "https://v/x.mp4", due, draft=False)
        self.assertEqual(str(inp["mode"]), "shareNow")
        self.assertNotIn("saveToDraft", inp)
        self.assertNotIn("dueAt", inp)

    def test_scheduled_and_draft(self):
        P.set_brand("bananas")
        m = {"id": "x", "channel": "kaicenat", "platform": "twitch"}
        due = datetime.now(P.TZ) + timedelta(hours=1)
        sch = P.post_input({"id": "c", "service": "tiktok"}, m, "u", due, draft=False)
        self.assertEqual(str(sch["mode"]), "customScheduled")
        self.assertIn("dueAt", sch)
        self.assertNotIn("saveToDraft", sch)
        dr = P.post_input({"id": "c", "service": "tiktok"}, {**m, "now": True}, "u", due, draft=True)
        self.assertTrue(dr["saveToDraft"])
        self.assertEqual(str(dr["mode"]), "addToQueue")


class DailyCaps(unittest.TestCase):
    def test_counts_every_run_of_the_day(self):
        """X / Threads cap 3 per day: earlier runs' posts count (the counter used to start at 0 per run)."""
        day = date(2026, 11, 19)
        chans = [{"id": "x1", "service": "twitter"}, {"id": "t1", "service": "threads"}, {"id": "i1", "service": "instagram"}]
        done = {
            "a": {"due": "2026-11-19T18:40:00+01:00", "posts": {"x1": "p", "t1": "p", "i1": "p"}},
            "b": {"due": "2026-11-20T02:10:00+01:00", "posts": {"x1": "p", "i1": "p"}},
            "c": {"due": "2026-11-20T18:40:00+01:00", "posts": {"x1": "p"}},      # next publishing day
        }
        used = P.day_service_counts(done, chans, day)
        self.assertEqual(used.get("twitter"), 2)
        self.assertEqual(used.get("threads"), 1)
        self.assertEqual(used.get("instagram"), 2)


class OneClipOneBrand(unittest.TestCase):
    def test_other_brand_published_clip_is_skipped(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            old = (P.OUT, P.HERE, P.inbox_items, P.BRANDS)
            try:
                P.HERE, P.OUT, P.BRANDS = t, t / "out", t / "brands.json"
                (t / "brands.json").write_text(json.dumps({
                    "bananas": {"published": "pb.json"}, "fanpage": {"published": "pf.json"}}))
                (t / "sources.json").write_text(json.dumps({"game": "Grand Theft Auto VI"}))
                (t / "pb.json").write_text(json.dumps({"clip1": {"due": "2026-11-19T18:40:00+01:00"}}))
                d = t / "out" / "kai" / "x_clip1"
                d.mkdir(parents=True)
                (d / "short_fanpage.mp4").write_bytes(b"x")
                (d / "meta.json").write_text(json.dumps({
                    "id": "clip1", "channel": "kai", "platform": "twitch", "category": "Grand Theft Auto VI",
                    "cam": [1, 1, 1, 1], "views": 5000, "shorts": {"fanpage": "short_fanpage.mp4"}}))
                P.inbox_items = lambda: []
                P.set_brand("fanpage")
                self.assertEqual([m["id"] for m in P.candidates()], [])
                (t / "pb.json").write_text("{}")
                self.assertEqual([m["id"] for m in P.candidates()], ["clip1"])
            finally:
                P.OUT, P.HERE, P.inbox_items, P.BRANDS = old
                P.set_brand("bananas")


# -----------------------------------------------------------------------------
# just in time (18. 11.)

TZ = P.TZ
SERVICES = ["instagram", "twitter", "youtube", "facebook", "tiktok", "threads"]


def at(d, hm, plus_days=0):
    h, m = map(int, hm.split(":"))
    return datetime.combine(d + timedelta(days=plus_days), datetime.min.time(), TZ).replace(hour=h, minute=m)


class Sim:
    """
    A publishing day on GitHub, run by run. Each normal run: fresh runner (out/ empty), render what
    `zbyva --plan` asks for (the batch selection rules), then `den` for both brands. Fast runs
    (live hand-over): `den --rychle` for both brands. Records live in a temp dir.
    """

    def __init__(self, t):
        self.t = Path(t)
        shutil.copy(HERE / "brands.json", self.t / "brands.json")
        shutil.copy(HERE / "sources.json", self.t / "sources.json")
        for f, keys in (("buffer.json", ("BUFFER_API_KEY_1", "BUFFER_API_KEY_2")),
                        ("buffer_fanpage.json", ("BUFFER_API_KEY_3", "BUFFER_API_KEY_4"))):
            chans = [{"id": f"{f[:-5]}-{s}", "service": s.upper(), "name": s, "key": keys[i // 3]}
                     for i, s in enumerate(SERVICES)]
            (self.t / f).write_text(json.dumps(chans))
        self.pool = []          # clips found on Twitch, not rendered yet
        self.live = []          # live-inbox items
        self.posts = []         # (brand, clip id, service, input)
        self.renders = []       # (run time, n rendered)
        self.top3_calls = []
        self.exp_calls = 0

    # -- world
    def clip(self, cid, channel, views, created, arrive):
        self.pool.append({"id": cid, "channel": channel, "views": views, "created": created, "arrive": arrive})

    def live_clip(self, cid, channel, spike, arrive, brand="bananas", z=12):
        self.live.append({"id": cid, "channel": channel, "platform": "twitch", "category": "Grand Theft Auto VI",
                          "cam": [1, 1, 1, 1], "views": 0, "viewers": 40000, "live": True, "spike_z": z,
                          "created": int(spike.timestamp()), "arrive": arrive,
                          ("file" if brand == "bananas" else "file_fanpage"): f"live/{cid}.mp4",
                          "base": "https://inbox.test", "remote": True, "title": "chat explosion"})

    # -- patches
    @contextlib.contextmanager
    def patched(self):
        old = (P.HERE, P.OUT, P.BRANDS, P.EXTRA_MB, P.POOL_FILE)
        env = {k: "x" for k in ("BUFFER_API_KEY_1", "BUFFER_API_KEY_2", "BUFFER_API_KEY_3", "BUFFER_API_KEY_4",
                                "NETLIFY_AUTH_TOKEN", "NETLIFY_SITE_ID", "MEDIA_BASE_URL")}

        def create_post(inp, key):
            self.posts.append((P.ZNACKA, self._cur, inp["channelId"].split("-")[-1], inp))
            return f"post{len(self.posts)}"

        orig_post_input = P.post_input

        def post_input(ch, m, url, due, draft):
            self._cur = m["id"]
            return orig_post_input(ch, m, url, due, draft)

        def top3(day, done, plan, stage, final=True):
            # the real gate (make_top3's first line) without the ffmpeg part
            if final and P.BRAND.get("top3", True) and P.remaining(day, done) - len(plan) <= 0:
                self.top3_calls.append((P.ZNACKA, P.NOW))
            return None

        def exp(a, day, now):
            self.exp_calls += 1

        try:
            P.HERE, P.OUT, P.BRANDS = self.t, self.t / "out", self.t / "brands.json"
            P.EXTRA_MB, P.POOL_FILE = self.t / "netlify_extra.json", self.t / "long_pool.json"
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(P, "inbox_items", lambda: [dict(m) for m in self.live if m["arrive"] <= P.NOW]), \
                    mock.patch.object(P, "inbox_deploy", lambda files, inbox, title=None, essential=False: "https://media.test/d"), \
                    mock.patch.object(P, "prune_drafts", lambda days=4: None), \
                    mock.patch.object(P, "is_public", lambda url: True), \
                    mock.patch.object(P, "http", lambda *a, **k: (200, b"x")), \
                    mock.patch.object(P, "create_post", create_post), \
                    mock.patch.object(P, "post_input", post_input), \
                    mock.patch.object(P, "make_top3", top3), \
                    mock.patch.object(P, "exp_post", exp), \
                    mock.patch.object(P, "league_line", lambda m: ""), \
                    mock.patch.object(P, "log", lambda msg: None), \
                    contextlib.redirect_stdout(io.StringIO()):
                yield
        finally:
            P.HERE, P.OUT, P.BRANDS, P.EXTRA_MB, P.POOL_FILE = old
            P.NOW, P.FAST = None, False
            P.set_brand("bananas")

    # -- runs
    def plan(self, brand):
        P.set_brand(brand)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            P.cmd_zbyva(Namespace(plan=True))
        return [int(x) for x in buf.getvalue().split()]

    def render(self, now):
        """batch with the JIT plan (klipuj.cmd_batch + assign_brands --brand-need, mirrored)."""
        shutil.rmtree(self.t / "out", ignore_errors=True)       # a fresh GitHub runner
        pb, pf = self.plan("bananas"), self.plan("fanpage")
        n, owed, good_n, hot = pb[0] + pf[0], pb[1] + pf[1], pb[2] + pf[2], pb[5] + pf[5]
        good, extra, hot_score = min(pb[3], pf[3]), min(pb[4], pf[4]), pb[6]
        need = {"bananas": [pb[1], pb[0] - pb[1], pb[5]], "fanpage": [pf[1], pf[0] - pf[1], pf[5]]}
        ts = now.timestamp()

        def vir(c):
            return c["views"] / (max((ts - c["created"].timestamp()) / 3600, 0) + 1) ** 0.5
        live = sorted((c for c in self.pool if c["arrive"] <= now), key=vir, reverse=True)
        chosen, tried = [], set()

        def take(lst, limit):
            k = 0
            for c in lst:
                if k >= limit:
                    break
                if c["id"] in tried:
                    continue
                tried.add(c["id"])
                chosen.append(c)
                k += 1
            return k
        total = take(live, min(owed, n))
        total += take([c for c in live if vir(c) >= good], min(good_n, n - total))
        total += take([c for c in live if vir(c) >= max(extra, good)], n - total)
        if hot:
            take([c for c in live if vir(c) >= hot_score and ts - c["created"].timestamp() <= 3 * 3600], hot)
        for c in chosen:
            brand = None
            for part in (0, 1, 2):
                open_ = [b for b in ("bananas", "fanpage") if need[b][part] > 0]
                if open_:
                    brand = max(open_, key=lambda b: need[b][part])
                    need[brand][part] -= 1
                    break
            brand = brand or "bananas"
            d = self.t / "out" / c["channel"] / c["id"]
            d.mkdir(parents=True, exist_ok=True)
            (d / "short.mp4").write_bytes(b"x")
            (d / "meta.json").write_text(json.dumps({
                "id": c["id"], "channel": c["channel"], "platform": "twitch", "category": "Grand Theft Auto VI",
                "cam": [1, 1, 1, 1], "views": c["views"], "created": int(c["created"].timestamp()),
                "title": "clip", "shorts": {brand: "short.mp4"}}))
            self.pool.remove(c)                                  # rendered = done in state.json, never again
        self.renders.append((now, len(chosen)))

    def den(self, now, fast=False):
        P.NOW = now
        for brand in ("bananas", "fanpage"):
            P.set_brand(brand)
            P.cmd_den(Namespace(den="dnes", n=10, ostre=True, zkouska=False, rychle=fast, znacka=brand))
        P.FAST = False

    def normal(self, now):
        P.NOW = now
        self.render(now)
        self.den(now)

    def fast(self, now):
        P.NOW = now
        shutil.rmtree(self.t / "out", ignore_errors=True)
        self.den(now, fast=True)

    def day(self, d, cadence, fast_after=3):
        """Normal runs every `cadence` minutes from 12:00 to 11:59; a fast run `fast_after` min after each live clip."""
        events = []
        t = at(d, "12:00")
        while t < at(d, "12:00", 1):
            events.append((t, 0))
            t += timedelta(minutes=cadence)
        for m in self.live:
            events.append((m["arrive"] + timedelta(minutes=fast_after), 1))
        for t, kind in sorted(events):
            (self.fast if kind else self.normal)(t)

    # -- checks
    def records(self, brand):
        P.set_brand(brand)
        return P.records()


class JustInTimeDay(unittest.TestCase):
    D = date(2026, 11, 19)                  # launch day: quotas from brands.json (8 per day, max 10)

    def world(self, sim, scarce=False):
        d = self.D
        k = 0
        for h in list(range(12, 24)) + list(range(0, 11)):
            plus = 1 if h < 12 else 0
            for j in range(2):
                k += 1
                arrive = at(d, f"{h:02d}:10", plus)
                views = (300 + 37 * k % 400) if scarce else (900 + (k * 377) % 900)   # scarce: all average
                sim.clip(f"c{k}", f"streamer{k % 7}", views, arrive - timedelta(minutes=40), arrive)
        if not scarce:
            sim.clip("hot21", "kaicenat", 30000, at(d, "20:45"), at(d, "21:00"))
            sim.clip("hot0130", "xqc", 15000, at(d, "01:15", 1), at(d, "01:30", 1))
            sim.live_clip("live_jynxzi_1", "jynxzi", at(d, "22:07"), at(d, "22:10"), brand="fanpage")

    def check_day(self, sim, cadence):
        d = self.D
        ids = {}
        for brand, mx, mn in ((z, B[z]["max_per_day"], QUOTA(z)) for z in ("bananas", "fanpage")):
            P.set_brand(brand)
            recs = sim.records(brand)
            main, live = (json.loads(f.read_text(encoding="utf-8")) if f.exists() else {} for f in P.rec_files())
            self.assertFalse(set(main) & set(live), f"{brand}: a clip in both record files")
            used = P.used_today(d, recs)
            self.assertLessEqual(used, mx, f"{brand}: max_per_day exceeded")
            self.assertGreaterEqual(used, mn, f"{brand}: daily minimum missed")
            svc = P.day_service_counts(recs, json.loads((sim.t / P.CHANNELS.name).read_text()), d)
            self.assertLessEqual(svc.get("twitter", 0), 3)
            self.assertLessEqual(svc.get("threads", 0), 3)
            for cid, rec in recs.items():
                self.assertNotIn(cid, ids, f"{cid} on both brands")
                ids[cid] = brand
                if rec.get("slot"):
                    self.assertNotIn(datetime.fromisoformat(rec["due"]).minute, (0, 30))
        seen = set()
        for brand, cid, service, inp in sim.posts:
            self.assertNotIn((cid, service), seen, f"{cid} posted twice on {service}")
            seen.add((cid, service))
            if "dueAt" in inp:
                due = datetime.fromisoformat(inp["dueAt"].replace("Z", "+00:00"))
                self.assertNotIn(due.minute, (0, 30), f"{cid} {service} due on :00/:30")
        return ids

    def assert_hot(self, sim, cid, brand, arrive, within):
        rec = sim.records(brand).get(cid)
        self.assertIsNotNone(rec, f"{cid} not published")
        self.assertTrue(rec["hot"])
        self.assertLessEqual(datetime.fromisoformat(rec["due"]), arrive + timedelta(minutes=within))
        # 18. 11.: the networks 0-4 min apart - one shareNow, the rest scheduled a few minutes later
        modes = [str(i["mode"]) for b, c, s, i in sim.posts if c == cid]
        self.assertIn("shareNow", modes)
        self.assertLessEqual(set(modes), {"shareNow", "customScheduled"})
        for b, c, s, i in sim.posts:
            if c == cid and "dueAt" in i:
                due = datetime.fromisoformat(i["dueAt"].replace("Z", "+00:00"))
                self.assertLessEqual(due, datetime.fromisoformat(rec["due"]) + timedelta(minutes=6))

    def run_day(self, cadence, scarce=False):
        with tempfile.TemporaryDirectory() as t:
            sim = Sim(t)
            self.world(sim, scarce)
            with sim.patched():
                sim.day(self.D, cadence)
                ids = self.check_day(sim, cadence)
                yield sim, ids

    def test_every_30_min(self):
        for sim, ids in self.run_day(30):
            d = self.D
            self.assert_hot(sim, "hot21", ids.get("hot21", "bananas"), at(d, "21:00"), 30 + 3)
            self.assert_hot(sim, "hot0130", ids.get("hot0130", "bananas"), at(d, "01:30", 1), 30 + 3)
            self.assert_hot(sim, "live_jynxzi_1", "fanpage", at(d, "22:10"), 3 + 3)
            P.set_brand("fanpage")
            self.assertIn("live_jynxzi_1", json.loads(P.live_file(P.PUBLISHED).read_text()))   # the fast path wrote it
            # never the whole day at once: the first run renders only the slots of the next 3 h
            self.assertLessEqual(sim.renders[0][1], 4)
            self.assertLessEqual(max(n for _, n in sim.renders), 6)
            # Top 3 only bananas, only in the last stretch
            self.assertTrue(sim.top3_calls)
            self.assertTrue(all(z == "bananas" for z, _ in sim.top3_calls))
            self.assertGreaterEqual(min(t for _, t in sim.top3_calls), at(d, "01:00", 1))

    def test_every_5_min_launch(self):
        for sim, ids in self.run_day(5):
            d = self.D
            self.assert_hot(sim, "hot21", ids.get("hot21", "bananas"), at(d, "21:00"), 5 + 3)
            self.assert_hot(sim, "hot0130", ids.get("hot0130", "bananas"), at(d, "01:30", 1), 5 + 3)
            self.assert_hot(sim, "live_jynxzi_1", "fanpage", at(d, "22:10"), 3 + 3)
            self.assertLessEqual(max(n for _, n in sim.renders), 6)

    def test_scarce_day_catches_up(self):
        """Only average clips: nothing burned early, the minimum is still met (catch-up)."""
        for sim, ids in self.run_day(30, scarce=True):
            for brand in ("bananas", "fanpage"):
                recs = sim.records(brand)
                self.assertEqual(P.used_today(self.D, recs), QUOTA(brand), brand)   # exactly the minimum
                first = min(datetime.fromisoformat(r["due"]) for r in recs.values())
                self.assertGreaterEqual(first, at(self.D, "19:00"), f"{brand}: average clips burned early")

    def test_many_live_clips_never_exceed_max(self):
        with tempfile.TemporaryDirectory() as t:
            sim = Sim(t)
            self.world(sim)
            for i in range(12):                              # a chat explosion every 20 min in the evening
                spike = at(self.D, "19:00") + timedelta(minutes=20 * i)
                sim.live_clip(f"live_s{i}_{i}", f"s{i}", spike, spike + timedelta(minutes=3),
                              brand="bananas" if i % 2 else "fanpage")
            with sim.patched():
                sim.day(self.D, 5)
                self.check_day(sim, 5)
                # the first live clips (quota left) went out in their fast run
                rec = sim.records("fanpage").get("live_s0_0")
                self.assertTrue(rec and rec["hot"])


class ExplodingDay(unittest.TestCase):
    """
    Aleš 18. 11.: a stream explodes at 13:00 (15 hot clips within an hour) and explodes even more at 23:00.
    The day-long pace must not burn the posts at 13:00: posts stay free for 23:00, the biggest 23:00 clips
    go out, the minimum is met, max_per_day never exceeded, slots never :00 / :30.
    """
    D = date(2026, 11, 26)                  # minimum 8, max 10 per brand

    def test_explosion_at_13_and_bigger_at_23(self):
        d = self.D
        hs = json.loads((HERE / "sources.json").read_text(encoding="utf-8"))["hot_score"]
        with tempfile.TemporaryDirectory() as t:
            sim = Sim(t)
            JustInTimeDay.world(self, sim)                                    # the normal flow of clips
            sim.pool = [c for c in sim.pool if not c["id"].startswith(("hot", "live"))]
            sim.live = []
            for i in range(15):                                               # 13:00-14:00: hot, not exceptional
                arrive = at(d, "13:00") + timedelta(minutes=4 * i)
                sim.clip(f"e13_{i}", f"loud{i % 3}", int(hs * (1.3 + 0.1 * i)), arrive - timedelta(minutes=10), arrive)
            big = []
            rank = [3, 14, 7, 0, 11, 5, 13, 1, 9, 12, 2, 6, 10, 4, 8]       # how big, in order of arrival
            for i in range(15):                                               # 23:00-24:00: much bigger
                arrive = at(d, "23:00") + timedelta(minutes=4 * i)
                views = int(hs * (2 + 0.9 * rank[i]))
                sim.clip(f"e23_{i}", f"loud{i % 3}", views, arrive - timedelta(minutes=10), arrive)
                big.append((views, f"e23_{i}"))
            with sim.patched():
                sim.day(d, 10)
                JustInTimeDay.check_day(self, sim, 10)
                recs = {**sim.records("bananas"), **sim.records("fanpage")}
                for brand in ("bananas", "fanpage"):
                    P.set_brand(brand)
                    dues = sorted(datetime.fromisoformat(r["due"]) for k, r in sim.records(brand).items()
                                  if P.in_day(r, d) and not k.startswith(("top3_", "exp_")))
                    self.assertLessEqual(sum(x < at(d, "15:00") for x in dues), 3, f"{brand}: 13:00 burned the day")
                    self.assertLessEqual(sum(x < at(d, "16:00") for x in dues), 4, f"{brand}: 13:00 burned the day")
                    self.assertLessEqual(sum(x < at(d, "22:00") for x in dues), 7, f"{brand}: no reserve for 22:00+")
                    self.assertGreaterEqual(sum(x >= at(d, "23:00") for x in dues), 2, f"{brand}: nothing left for 23:00")
                # the biggest clips of the 23:00 explosion went out (each on one account)
                for views, cid in sorted(big, reverse=True)[:3]:
                    self.assertIn(cid, recs, f"{cid} ({views} views) not published")
                # of the 13:00 explosion, not all 15 burned the day
                self.assertLess(sum(k.startswith("e13_") for k in recs), 15)


class Concurrency(unittest.TestCase):
    def test_normal_and_fast_never_double(self):
        """A live clip is in the live-inbox: the normal run publishes it (hot), the fast run right after
        must not; and the other way round."""
        d = date(2026, 11, 19)
        for first in ("normal", "fast"):
            with tempfile.TemporaryDirectory() as t:
                sim = Sim(t)
                sim.live_clip("live_kai_9", "kaicenat", at(d, "21:58"), at(d, "22:00"))
                with sim.patched():
                    now = at(d, "22:01")
                    order = [sim.normal, sim.fast] if first == "normal" else [sim.fast, sim.normal]
                    for run in order:
                        run(now)
                        run(now + timedelta(minutes=1))
                    posted = [c for b, c, s, i in sim.posts if c == "live_kai_9"]
                    self.assertEqual(len(posted), 6, first)              # once per network, not twice
                    P.set_brand("bananas")
                    files = [json.loads(f.read_text()) for f in P.rec_files() if f.exists()]
                    self.assertEqual(sum("live_kai_9" in f for f in files), 1)

    def test_lost_lock_stops_posting(self):
        """Codex r2: when the renewal loses the lock, `den` posts nothing more."""
        d = date(2026, 11, 19)
        with tempfile.TemporaryDirectory() as t:
            sim = Sim(t)
            sim.clip("hoty", "kaicenat", 30000, at(d, "20:45"), at(d, "21:00"))
            with sim.patched():
                (sim.t / P.LOCK_LOST).write_text("1")
                with self.assertRaises(SystemExit):              # 'nothing went out' -> red run
                    sim.normal(at(d, "21:01"))
                self.assertEqual(sim.posts, [])
                self.assertEqual(sim.records("bananas"), {})

    def test_lost_lock_blocks_every_post(self):
        """Codex r3: Top 3 / experiments post through create_post too - it refuses once the lock is lost."""
        with tempfile.TemporaryDirectory() as t, mock.patch.object(P, "HERE", Path(t)), \
                mock.patch.object(P, "gql", lambda *a, **k: self.fail("must not reach Buffer")):
            (Path(t) / P.LOCK_LOST).write_text("1")
            with self.assertRaises(RuntimeError):
                P.create_post({"text": "x"}, "BUFFER_API_KEY_1")

    def test_posting_stops_before_the_lock_expires(self):
        """Codex r4: a hanging renewal must not matter - create_post stops at the confirmed expiry minus
        a margin, by the clock."""
        import time
        with tempfile.TemporaryDirectory() as t, mock.patch.object(P, "HERE", Path(t)), \
                mock.patch.object(P, "gql", lambda *a, **k: {"createPost": {"post": {"id": "p"}}}):
            self.assertFalse(P.lock_lost())                       # no lock file: local run
            P.lock_deadline(time.time() + 20 * 60)                # just taken / renewed
            self.assertFalse(P.lock_lost())
            self.assertEqual(P.create_post({"text": "x"}, "BUFFER_API_KEY_1"), "p")
            P.lock_deadline(time.time() + P.LOCK_MARGIN - 1)      # expiry in git is < margin away
            self.assertTrue(P.lock_lost())
            with self.assertRaises(RuntimeError):
                P.create_post({"text": "x"}, "BUFFER_API_KEY_1")

    def test_rate_limit_wait_past_the_lock(self):
        """Codex r5: a 429 wait that outlives the lock must not send the mutation again."""
        import urllib.error
        calls = []

        def http_429(*a, **k):
            calls.append(1)
            raise urllib.error.HTTPError("u", 429, "rate", {"Retry-After": "300"}, io.BytesIO(b""))
        with tempfile.TemporaryDirectory() as t, mock.patch.object(P, "HERE", Path(t)), \
                mock.patch.object(P, "http", http_429), mock.patch.object(P, "log", lambda msg: None), \
                mock.patch.dict(os.environ, {"BUFFER_API_KEY_1": "x"}):
            P.lock_deadline(9e9)
            with mock.patch.object(P.time, "sleep", lambda s: P.lock_deadline(0)):   # the wait passes the deadline
                with self.assertRaises(RuntimeError):
                    P.create_post({"text": "x"}, "BUFFER_API_KEY_1")
            self.assertEqual(len(calls), 1)

    def test_failed_record_push_stops_posting(self):
        """Codex r6: if a record cannot reach origin, the job posts nothing more."""
        with tempfile.TemporaryDirectory() as t, mock.patch.object(P, "HERE", Path(t)), \
                mock.patch.object(P, "log", lambda msg: None), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(P, "lock_release", lambda *a, **k: False):
            self.assertTrue(P.push_records(Path(t) / "publikovano.json"))     # no lock in use: nothing to do
            P.lock_deadline(9e9)
            self.assertFalse(P.push_records(Path(t) / "publikovano.json"))
            self.assertTrue(P.lock_lost())

    def test_renewal_error_raises_the_flag(self):
        """Codex r3: a git timeout in the renewal must not end it silently - the flag stops `den`."""
        with tempfile.TemporaryDirectory() as t, mock.patch.object(P, "HERE", Path(t)), \
                mock.patch.object(P, "log", lambda msg: None), \
                mock.patch.object(P, "lock_renew", mock.Mock(side_effect=subprocess.TimeoutExpired("git", 120))), \
                mock.patch.object(P.time, "sleep", lambda s: None), contextlib.redirect_stdout(io.StringIO()):
            clock = iter(range(0, 10 ** 6, 400))           # 400 s per renewal attempt: 3/4 of 20 min passes
            with mock.patch.object(P.time, "time", lambda: next(clock)), self.assertRaises(SystemExit):
                P.cmd_zamek(Namespace(akce="obnov", kdo="x", ttl=20, cekej=0, uloz=[]))
            self.assertTrue((Path(t) / P.LOCK_LOST).exists())

    def test_fast_path_only_live(self):
        d = date(2026, 11, 19)
        with tempfile.TemporaryDirectory() as t:
            sim = Sim(t)
            sim.clip("hotx", "kaicenat", 30000, at(d, "20:45"), at(d, "21:00"))
            with sim.patched():
                sim.render(at(d, "21:01"))                       # rendered on this runner, but ...
                P.NOW = at(d, "21:01")
                for brand in ("bananas", "fanpage"):
                    P.set_brand(brand)
                    P.cmd_den(Namespace(den="dnes", n=10, ostre=True, zkouska=False, rychle=True, znacka=brand))
                self.assertEqual(sim.posts, [])                  # ... the fast path carries live clips only


class DayWindow(unittest.TestCase):
    def test_pub_day(self):
        self.assertEqual(P.pub_day(at(date(2026, 11, 19), "12:00")), date(2026, 11, 19))
        self.assertEqual(P.pub_day(at(date(2026, 11, 20), "01:30")), date(2026, 11, 19))
        self.assertEqual(P.pub_day(at(date(2026, 11, 20), "11:59")), date(2026, 11, 19))

    def test_slots_never_on_full_or_half_hour(self):
        for k in range(40):
            d = date(2026, 11, 19) + timedelta(days=k)
            for z in ("bananas", "fanpage"):
                for s in P.slot_times(d, len(P.SLOTS)):
                    t = P.jittered(s, d, z)
                    self.assertNotIn(t.minute, (0, 30))
                    for svc in SERVICES:
                        self.assertNotIn(P.net_due(t, d, svc, z).minute, (0, 30))

    def test_top3_waits_for_the_last_stretch(self):
        P.set_brand("bananas")
        with tempfile.TemporaryDirectory() as t:
            self.assertIsNone(P.make_top3(date(2026, 11, 19), {}, [], Path(t), final=False))


class LiveViews(unittest.TestCase):
    def test_live_rating_from_chat_spike(self):
        import klipuj as K
        self.assertEqual(K.live_rating(6), 5)
        self.assertEqual(K.live_rating(24), 8)
        self.assertEqual(K.live_rating(500), 9)
        self.assertEqual(K.golden_rating({"live": True, "views": 0}, 9), (9, False))

    def test_catch_up_clip_goes_to_the_brand_that_owes(self):
        """Codex r1: an average catch-up clip must go to the brand that owes posts, not the other one."""
        import klipuj as K
        old = dict(K.BRAND_NEED)
        try:
            K.BRAND_NEED.clear()
            K.BRAND_NEED.update({"bananas": [0, 2, 0], "fanpage": [1, 1, 0]})
            self.assertEqual(K.assign_brands({"id": "x", "views": 100, "created": 0}), ["fanpage"])
            self.assertEqual(K.assign_brands({"id": "y", "views": 100, "created": 0}), ["bananas"])
            # Codex r2: a failed render gives the brand's need back
            c = {"id": "z", "views": 100, "created": 0}
            K.BRAND_NEED.update({"bananas": [0, 2, 0], "fanpage": [1, 1, 0]})
            self.assertEqual(K.assign_brands(c), ["fanpage"])
            K.need_back(c)
            self.assertEqual(K.BRAND_NEED["fanpage"], [1, 1, 0])
            self.assertEqual(K.assign_brands({"id": "w", "views": 100, "created": 0}), ["fanpage"])
        finally:
            K.BRAND_NEED.clear()
            K.BRAND_NEED.update(old)

    def test_live_clips_not_in_league(self):
        import liga
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            old = (liga.HERE, liga.POOL)
            try:
                liga.HERE, liga.POOL = t, t / "pool.json"
                now = datetime.now(TZ).isoformat()
                (t / "publikovano.json").write_text(json.dumps({
                    "a1": {"due": now, "channel": "kai", "rating": 5}, "a2": {"due": now, "channel": "kai", "rating": 5}}))
                (t / "publikovano_live.json").write_text(json.dumps({
                    "live_kai_1": {"due": now, "channel": "kai", "rating": 9, "live": True},
                    "live_kai_2": {"due": now, "channel": "kai", "rating": 9, "live": True}}))
                rows = liga.table(7)
                self.assertEqual(rows[0]["avg"], 5)
                self.assertEqual(rows[0]["n"], 2)
            finally:
                liga.HERE, liga.POOL = old


@unittest.skipUnless(shutil.which("git"), "git missing")
class GitLock(unittest.TestCase):
    """publikuj.py zamek: compare-and-swap on origin/main (a local bare repo stands in for GitHub)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        run = lambda *a, cwd=t: subprocess.run(["git", *a], cwd=cwd, capture_output=True, check=True)
        run("init", "-q", "--bare", "-b", "main", "origin.git")
        run("clone", "-q", "origin.git", "seed")
        (t / "seed" / "publikovano.json").write_text("{}")
        run("add", ".", cwd=t / "seed")
        run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init", cwd=t / "seed")
        run("push", "-q", "origin", "HEAD:main", cwd=t / "seed")
        run("clone", "-q", "origin.git", "a")
        run("clone", "-q", "origin.git", "b")
        self.a, self.b, self.t = t / "a", t / "b", t

    def tearDown(self):
        self.tmp.cleanup()

    def test_mutual_exclusion_and_records(self):
        with mock.patch.object(P, "log", lambda msg: None), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(P.lock_take("run-a", 20, 0, cwd=self.a))
            self.assertFalse(P.lock_take("run-b", 20, 0, cwd=self.b, poll=0))   # held by a
            (self.a / "publikovano.json").write_text(json.dumps({"c1": {"due": "x"}}))
            self.assertTrue(P.lock_release("run-a", ["publikovano.json"], cwd=self.a))
            self.assertTrue(P.lock_take("run-b", 20, 0, cwd=self.b))
            # b's own record file has c2; releasing merges with a's c1 on origin, nothing lost
            (self.b / "publikovano_live.json").write_text(json.dumps({"live_1": {"due": "y"}}))
            self.assertTrue(P.lock_release("run-b", ["publikovano_live.json"], cwd=self.b))
            subprocess.run(["git", "fetch", "-q", "origin", "main"], cwd=self.a, capture_output=True, check=True)
            show = lambda f: json.loads(P.git("show", f"origin/main:{f}", cwd=self.a)[1])
            self.assertIn("c1", show("publikovano.json"))
            self.assertIn("live_1", show("publikovano_live.json"))
            self.assertIsNone(show(P.LOCK_FILE)["kdo"])
            # the merged file is written back to b's work tree (the later rebase sees the same change)
            self.assertEqual(json.loads((self.b / "publikovano_live.json").read_text()), show("publikovano_live.json"))

    def test_renewal_and_lost_lock(self):
        with mock.patch.object(P, "log", lambda msg: None), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(P.lock_take("run-a", 0.001, 0, cwd=self.a))
            import time
            time.sleep(0.2)
            self.assertTrue(P.lock_take("run-b", 20, 0, cwd=self.b))      # a's lock expired
            self.assertIsNone(P.lock_renew("run-a", 20, cwd=self.a))      # a learns it lost it
            self.assertTrue(P.lock_renew("run-b", 20, cwd=self.b))

    def test_cli_zamek_parses(self):
        """Codex r1: `zamek` had no --znacka, main() crashed before taking the lock."""
        with tempfile.TemporaryDirectory() as t, \
                mock.patch.object(P, "HERE", Path(t)), mock.patch.object(P, "log", lambda msg: None), \
                mock.patch.object(sys, "argv", ["publikuj.py", "zamek", "vezmi", "--kdo", "x", "--cekej", "0"]), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as e:             # no git remote: gives up cleanly
                P.main()
            self.assertEqual(e.exception.code, 1)
        P.set_brand("bananas")

    def test_records_on_origin_before_release(self):
        """Codex r5: each published clip is pushed at once (release=False), the lock stays held."""
        with mock.patch.object(P, "log", lambda msg: None), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(P.lock_take("run-a", 20, 0, cwd=self.a))
            (self.a / "publikovano.json").write_text(json.dumps({"c9": {"due": "x"}}))
            self.assertTrue(P.lock_release("den", ["publikovano.json"], cwd=self.a, release=False))
            P.git("fetch", "-q", "origin", "main", cwd=self.b)
            self.assertIn("c9", json.loads(P.git("show", "origin/main:publikovano.json", cwd=self.b)[1]))
            self.assertEqual(json.loads(P.git("show", f"origin/main:{P.LOCK_FILE}", cwd=self.b)[1])["kdo"], "run-a")

    def test_dead_run_lock_expires(self):
        with mock.patch.object(P, "log", lambda msg: None), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(P.lock_take("dead", 0.001, 0, cwd=self.a))
            import time
            time.sleep(0.2)
            self.assertTrue(P.lock_take("run-b", 20, 0, cwd=self.b))

    def test_stale_fetch_cannot_win(self):
        """Both read the lock free; only the first push lands, the second is rejected (origin moved)."""
        with mock.patch.object(P, "log", lambda msg: None):
            build = lambda who: (lambda get: {P.LOCK_FILE: json.dumps({"kdo": who, "until": 9e9})})
            real = P.git

            raced = []

            def git_b(*a, **k):                     # b's push happens after a's: its base is stale
                if a and a[0] == "push" and k.get("cwd") == self.b and not raced:
                    raced.append(1)
                    with mock.patch.object(P, "git", real):
                        self.assertTrue(P.git_cas(build("run-a"), "a", cwd=self.a))
                return real(*a, **k)
            with mock.patch.object(P, "git", git_b):
                self.assertFalse(P.git_cas(build("run-b"), "b", cwd=self.b))
            real("fetch", "-q", "origin", "main", cwd=self.a)
            self.assertEqual(json.loads(real("show", f"origin/main:{P.LOCK_FILE}", cwd=self.a)[1])["kdo"], "run-a")


class Top4(unittest.TestCase):
    """The daily Top 4 funniest (19. 11.): own shorts with a payoff, funniest first, never a clip twice,
    labels that belong to the clip, and it costs one normal short per slot."""
    def rec(self, due, funny, payoff=True):
        return {"due": due.isoformat(), "funny": funny, "score": 1, "channel": "xqc",
                "payoff": "https://x/p.mp4" if payoff else None}

    def test_pick_order_and_no_repeat(self):
        now = datetime(2026, 11, 19, 20, 0, tzinfo=P.TZ)
        recs = {f"c{i}": self.rec(now - timedelta(hours=i + 1), i / 10) for i in range(6)}
        recs["old"] = self.rec(now - timedelta(hours=40), 0.99)           # outside the window
        recs["future"] = self.rec(now + timedelta(hours=1), 0.98)         # not out yet: never spoiled
        recs["nopay"] = self.rec(now - timedelta(hours=1), 0.97, payoff=False)
        recs["top4_x"] = {"due": now.isoformat(), "items": ["c5"]}        # already in a Top 4
        self.assertEqual([k for k, _ in P.top4_pick(recs, now)], ["c4", "c3", "c2", "c1", "c0"])

    def test_only_the_best(self):
        now = datetime(2026, 11, 19, 20, 0, tzinfo=P.TZ)
        recs = {"meh": {**self.rec(now - timedelta(hours=1), 0.3), "channel": "nobody123"},
                "funny": {**self.rec(now - timedelta(hours=1), 0.8), "channel": "nobody123"},
                "viral": {**self.rec(now - timedelta(hours=1), 0.1), "channel": "nobody123", "score": 5000},
                "speed": {**self.rec(now - timedelta(hours=1), 0.3), "channel": "ishowspeed"}}
        self.assertEqual({k for k, _ in P.top4_pick(recs, now)}, {"funny", "viral", "speed"})

    def test_accounts_never_share_a_clip(self):
        now = datetime(2026, 11, 19, 20, 0, tzinfo=P.TZ)
        mine = {f"s{i}": self.rec(now - timedelta(hours=1), 0.5) for i in range(20)}     # all on both accounts
        mine["solo"] = self.rec(now - timedelta(hours=1), 0.1)
        other = {k: dict(v) for k, v in mine.items() if k != "solo"}
        other["top4_y"] = {"due": now.isoformat(), "items": ["solo"]}                  # the other used it: never
        with mock.patch.object(P, "ZNACKA", "bananas"):
            a = {k for k, _ in P.top4_pick(mine, now, {"fanpage": other})}
        with mock.patch.object(P, "ZNACKA", "fanpage"):
            b = {k for k, _ in P.top4_pick(other, now, {"bananas": mine})}
        self.assertFalse(a & b)
        self.assertEqual(a | b, set(other) - {"top4_y"})
        self.assertNotIn("solo", a)

    def test_pool_feeds_only_its_brand(self):
        now = datetime(2026, 11, 19, 20, 0, tzinfo=P.TZ)
        with tempfile.TemporaryDirectory() as t:
            pool = Path(t) / "top4_pool.json"
            pool.write_text(json.dumps({"bananas:p1": {**self.rec(now - timedelta(hours=2), 0.9), "id": "p1"},
                                        "fanpage:p2": {**self.rec(now - timedelta(hours=2), 0.9), "id": "p2"}}))
            with mock.patch.object(P, "TOP4_POOL", pool):
                src = P.top4_sources("bananas", {"own": self.rec(now - timedelta(hours=1), 0.2)})
        self.assertEqual(set(src), {"p1", "own"})

    def test_label_grounded(self):
        facts = "What you see: a car crashes into a police car. Clip title: xQc CRASHES In GTA 6"
        self.assertTrue(P.top4_grounded("COPS CAR HAD FAMILY", facts))
        self.assertTrue(P.top4_grounded("CRASH TEST DUMMY", facts))
        self.assertFalse(P.top4_grounded("PRIVATE DANCE SHOCKER", facts))
        self.assertFalse(P.top4_grounded("FUNNY MOMENT", facts))

    def test_template_never_repeats(self):
        with mock.patch.dict(os.environ, {"CF_TEXT": "0", "CF_VISION": "0"}):
            labs = []
            for _ in range(4):
                labs.append(P.top4_label({"channel": "xqc", "tags": ["funny"]}, None, labs))
        self.assertEqual(len(set(labs)), 4)

    def test_reserve_cuts_the_quota(self):
        day = date(2026, 11, 20)
        with mock.patch.object(P, "BRAND", {"ramp": {"2026-11-19": 8}}):
            plain = P.daily_quota(day)
        with mock.patch.object(P, "BRAND", {"ramp": {"2026-11-19": 8}, "rank4": True, "rank_times": ["19:40", "23:20"]}):
            self.assertEqual(P.daily_quota(day), plain - 2)
            self.assertEqual([t.strftime("%d %H:%M") for t in P.top4_slots(day)], ["20 19:40", "20 23:20"])


if __name__ == "__main__":
    unittest.main()
