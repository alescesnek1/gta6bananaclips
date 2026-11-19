"""
stories.py (18. 11.): plan times, caps, never :00 / :30, repost timing, pool rotation, Buffer input,
no secrets, reply lines, no tofu glyphs, text never over faces / chests, the long-form promo (once per
long-form, timing), the link story of the long-form e-mail, the stale state merge (no double repost). No network, nothing published - Buffer / Netlify / ffmpeg are mocked.
  .venv/Scripts/python -m unittest tests.test_stories
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import publikuj as P                                       # noqa: E402
import stories as S                                        # noqa: E402

CH = {"bananas": [{"id": "ig_b", "service": "instagram", "key": "BUFFER_API_KEY_1"},
                  {"id": "fb_b", "service": "facebook", "key": "BUFFER_API_KEY_2"}],
      "fanpage": [{"id": "ig_f", "service": "instagram", "key": "BUFFER_API_KEY_3"},
                  {"id": "fb_f", "service": "facebook", "key": "BUFFER_API_KEY_4"}]}
SECRET = "sk-test-SECRET-value-123"


def rec(due, channel="kaicenat", rating=8, **kw):
    return {"url": f"https://media.example/{channel}.mp4", "due": due.isoformat(), "draft": False,
            "posts": {"x": "y"}, "channel": channel, "rating": rating, **kw}


class Base(unittest.TestCase):
    def setUp(self):
        self.posted = []
        def create_post(inp, key):
            if P.lock_lost():
                raise RuntimeError("lock lost")
            self.posted.append((inp, key))
            return f"p{len(self.posted)}"
        self.patches = [mock.patch.object(P, "create_post", side_effect=create_post),
                        mock.patch.object(S, "story_channels", side_effect=lambda z: CH[z]),
                        mock.patch.object(S, "video_seconds", return_value=25.0),
                        mock.patch.dict(os.environ, {k: SECRET for k in ("BUFFER_API_KEY_1", "BUFFER_API_KEY_2",
                                                                         "BUFFER_API_KEY_3", "BUFFER_API_KEY_4")})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        P.NOW = None


class PlanTimes(unittest.TestCase):
    def test_times_windows_off_grid(self):
        counts = set()
        for i in range(60):
            day = date(2026, 11, 19) + timedelta(days=i)
            for z in ("bananas", "fanpage"):
                ts = S.day_times(day, z)
                counts.add(len(ts))
                self.assertTrue(2 <= len(ts) <= 4)
                self.assertEqual(ts, sorted(ts))
                self.assertEqual(ts, S.day_times(day, z))                    # deterministic per run
                for t in ts:
                    self.assertNotIn(t.minute, (0, 30))
                    self.assertEqual(P.pub_day(t), day)                       # inside the publishing day
                    hm = t.strftime("%H:%M")
                    self.assertTrue(any(a <= hm <= b for a, b in S.WINDOWS), hm)
        self.assertEqual(counts, {2, 3, 4})

    def test_brands_and_days_differ(self):
        d = date(2026, 11, 19)
        self.assertNotEqual(S.day_times(d, "bananas"), S.day_times(d, "fanpage"))
        self.assertNotEqual([t.time() for t in S.day_times(d, "bananas")],
                            [t.time() for t in S.day_times(d + timedelta(days=1), "bananas")])


class Reposts(Base):
    NOW = datetime(2026, 11, 19, 20, 0, tzinfo=P.TZ)

    def run_once(self, recs, bs, now=None, draft=False):
        P.NOW = now or self.NOW
        with mock.patch.object(P, "records", return_value=recs),                 mock.patch.object(S, "texted_reel", side_effect=lambda rec, z, cid, day: rec["url"]):
            return S.do_reposts("bananas", bs, P.NOW, draft, dry=False)

    def test_repost_without_text_is_not_posted(self):
        recs = {"c0": rec(self.NOW)}
        bs = S.bstate({}, "bananas")
        P.NOW = self.NOW
        with mock.patch.object(P, "records", return_value=recs),                 mock.patch.object(S, "texted_reel", side_effect=RuntimeError("ffmpeg")):
            S.do_reposts("bananas", bs, P.NOW, False, dry=False)
        self.assertEqual((self.posted, bs["reposts"]), ([], {}))

    def test_timing_20_60_off_grid(self):
        recs = {f"c{i}": rec(self.NOW - timedelta(minutes=i)) for i in range(0, 15, 5)}
        bs = S.bstate({}, "bananas")
        made = self.run_once(recs, bs)
        self.assertEqual(len(made), 3)
        for cid, due in made:
            reel = datetime.fromisoformat(recs[cid]["due"])
            self.assertGreaterEqual(due - reel, timedelta(minutes=20))
            self.assertLessEqual(due - reel, timedelta(minutes=61))
            self.assertNotIn(due.minute, (0, 30))
        inp = self.posted[0][0]
        self.assertEqual(inp["assets"], [{"video": {"url": recs["c0"]["url"]}}])  # the same Netlify mp4
        self.assertEqual(str(inp["metadata"]["instagram"]["type"]), "story")
        self.assertEqual({i["channelId"] for i, _ in self.posted}, {"ig_b", "fb_b"})

    def test_not_twice_and_only_when_live(self):
        recs = {"soon": rec(self.NOW + timedelta(minutes=10)), "later": rec(self.NOW + timedelta(hours=2)),
                "old": rec(self.NOW - timedelta(hours=7)), "gone": rec(self.NOW, deleted=True),
                "drafted": rec(self.NOW, draft=True)}
        bs = S.bstate({}, "bananas")
        self.assertEqual([c for c, _ in self.run_once(recs, bs)], ["soon"])
        self.assertEqual(self.run_once(recs, bs), [])                     # next run: nothing again
        self.assertEqual(len(self.posted), 2)

    def test_cap_six_per_day(self):
        bs = S.bstate({}, "bananas")
        for step in range(10):                                            # a reel every 30 min, run after each
            now = self.NOW + timedelta(minutes=30 * step)
            recs = {f"c{i}": rec(self.NOW + timedelta(minutes=30 * i)) for i in range(step + 1)}
            self.run_once(recs, bs, now)
        self.assertEqual(sum(1 for r in bs["reposts"].values() if r.get("done")), S.REPOST_MAX)

    def test_long_video_skipped(self):
        bs = S.bstate({}, "bananas")
        with mock.patch.object(S, "video_seconds", return_value=75.0):
            self.assertEqual(self.run_once({"c": rec(self.NOW)}, bs), [])
        self.assertIn("skip", bs["reposts"]["c"])
        self.assertEqual(self.posted, [])

    def test_failure_retried_then_given_up(self):
        bs = S.bstate({}, "bananas")
        with mock.patch.object(P, "create_post", side_effect=RuntimeError("Buffer down")):
            self.run_once({"c": rec(self.NOW)}, bs)
            self.run_once({"c": rec(self.NOW)}, bs)
            self.run_once({"c": rec(self.NOW)}, bs)
        self.assertEqual(bs["reposts"]["c"]["fails"], S.MAX_FAILS)

    def test_lock_lost_posts_nothing(self):
        bs = S.bstate({}, "bananas")
        with mock.patch.object(P, "lock_lost", return_value=True):
            self.run_once({"c": rec(self.NOW)}, bs)
        self.assertEqual(self.posted, [])
        self.assertFalse(bs["reposts"]["c"].get("done"))


class Input(unittest.TestCase):
    def test_story_inputs(self):
        due = datetime(2026, 11, 19, 21, 17, tzinfo=P.TZ)
        ig = S.story_input(CH["bananas"][0], {"image": "https://m/x.jpg"}, due, draft=False, alt="Who?")
        self.assertEqual(str(ig["mode"]), "customScheduled")
        self.assertEqual(ig["dueAt"], "2026-11-19T20:17:00.000Z")
        self.assertNotIn("saveToDraft", ig)
        self.assertFalse(ig["metadata"]["instagram"]["shouldShareToFeed"])
        self.assertEqual(ig["assets"][0]["image"]["metadata"]["altText"], "Who?")
        fb = S.story_input(CH["bananas"][1], {"video": "https://m/x.mp4"}, due, draft=True)
        self.assertEqual(str(fb["metadata"]["facebook"]["type"]), "story")
        self.assertTrue(fb["saveToDraft"])
        self.assertEqual(str(fb["mode"]), "addToQueue")
        self.assertNotIn("dueAt", fb)
        self.assertNotIn("stickerFields", json.dumps(P.gql_value(ig)))   # stickers: reminder publishing only


class Pool(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "bananas").mkdir()
        for i in range(20):
            (self.tmp / "bananas" / f"b_{i:02d}.jpg").write_bytes(b"x")
        self.p = mock.patch.object(S, "POOL_DIR", self.tmp)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.tmp)

    def test_rotation_gap_and_all_used(self):
        bs = S.bstate({}, "bananas")
        seen = {}
        for i in range(40):                                  # two pool stories a day = harsh
            day = date(2026, 11, 19) + timedelta(days=i)
            for _ in range(2):
                p = S.pool_pick("bananas", bs, day)
                self.assertIsNotNone(p)
                if p.name in seen:
                    self.assertGreaterEqual((day - seen[p.name]).days, S.POOL_GAP_DAYS)
                seen[p.name] = day
                bs["pool"][p.name] = day.isoformat()
        self.assertEqual(len(seen), 20)

    def test_empty_pool(self):
        self.assertIsNone(S.pool_pick("fanpage", S.bstate({}, "fanpage"), date(2026, 11, 19)))


class Facts(unittest.TestCase):
    DAY = date(2026, 11, 20)

    def stats(self, rows):
        tmp = Path(tempfile.mkdtemp())
        f = tmp / "publikovano.json"
        f.write_text(json.dumps(rows), encoding="utf-8")
        try:
            with mock.patch.object(P, "rec_files", return_value=[f]):
                return S.yesterday_stats(self.DAY, "bananas")
        finally:
            shutil.rmtree(tmp)

    def test_real_numbers_only(self):
        y = datetime(2026, 11, 19, 18, 13, tzinfo=P.TZ)
        rows = {"a1": rec(y, "kaicenat", 9), "a2": rec(y, "kaicenat", 7), "a3": rec(y, "kaicenat", 8),
                "b1": rec(y, "ishowspeed", 10), "live_x": rec(y, "xqc", 9, live=True),
                "old": rec(y - timedelta(days=2), "xqc", 10), "del": rec(y, "xqc", 10, deleted=True)}
        st = self.stats(rows)
        self.assertEqual(st["count"], {"kaicenat": 3, "ishowspeed": 1, "xqc": 1})
        lines, clip = S.fill("fact_most", "bananas", st)
        self.assertEqual(lines[1:3], ["Kai Cenat", "3 clips on our page"])
        lines, _ = S.fill("fact_top", "bananas", st)                       # >= 2 clips to lead
        self.assertEqual(lines[1:3], ["Kai Cenat", "Banana score 8/10 from 3 clips"])
        lines, _ = S.fill("question", "bananas", st)
        self.assertEqual(lines[1], "Kai Cenat or IShowSpeed")

    def test_no_data_no_fact(self):
        st = self.stats({})
        for k in ("question", "fact_most", "fact_top", "rate", "guess"):
            self.assertIsNone(S.fill(k, "bananas", st))
        bs = S.bstate({}, "bananas")
        with mock.patch.object(S, "pool_files", return_value=[]):
            self.assertEqual(S.plan_day(self.DAY, "bananas", bs, st), [])

    def test_tie_is_not_most_clipped(self):
        y = datetime(2026, 11, 19, 18, 13, tzinfo=P.TZ)
        st = self.stats({"a": rec(y, "kaicenat"), "b": rec(y, "kaicenat"), "c": rec(y, "xqc"), "d": rec(y, "xqc")})
        self.assertIsNone(S.fill("fact_most", "bananas", st))

    def test_copy_has_no_handles_or_hashtags(self):
        for z, kinds in S.COPY.items():
            for k, lines in kinds.items():
                for line in lines:
                    self.assertNotRegex(line, r"@|#[A-Za-z]")              # "#1" is a rank, not a hashtag


class BuildDay(Base):
    DAY = date(2026, 11, 20)

    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        y = datetime(2026, 11, 19, 18, 13, tzinfo=P.TZ)
        rows = {"a1": rec(y, "kaicenat", 9), "a2": rec(y, "kaicenat", 7), "a3": rec(y, "kaicenat", 8),
                "b1": rec(y, "ishowspeed", 10)}
        f = self.tmp / "publikovano.json"
        f.write_text(json.dumps(rows), encoding="utf-8")
        pool = self.tmp / "pool" / "fanpage"
        pool.mkdir(parents=True)
        from PIL import Image
        Image.new("RGB", (900, 1600), (200, 50, 120)).save(pool / "f_01.jpg")
        self.deploys = []
        self.more = [mock.patch.object(P, "rec_files", return_value=[f]),
                     mock.patch.object(S, "POOL_DIR", self.tmp / "pool"),
                     mock.patch.object(S, "HERE", self.tmp)]
        for p in self.more:
            p.start()

    def tearDown(self):
        for p in self.more:
            p.stop()
        shutil.rmtree(self.tmp)
        super().tearDown()

    def deploy(self, files):
        self.deploys.append(dict(files))
        return "https://abc--media.netlify.app"

    def test_one_deploy_both_brands_once_a_day(self):
        now = datetime.combine(self.DAY, time(12, 7), P.TZ)
        P.NOW = now
        st = {}
        grab = mock.Mock(return_value=None)
        todo = S.build_and_post(self.DAY, ["bananas", "fanpage"], st, now, False, False, self.deploy, grab)
        self.assertEqual(len(self.deploys), 1)                             # one Netlify deploy for both
        self.assertTrue(todo)
        self.assertEqual({z for z, *_ in todo}, {"bananas", "fanpage"})
        for z in ("bananas", "fanpage"):
            n = sum(1 for t in todo if t[0] == z)
            self.assertTrue(2 <= n <= 4, n)
            self.assertTrue(st[z]["days"][self.DAY.isoformat()]["built"])
        for inp, key in self.posted:
            self.assertTrue(inp["assets"][0]["image"]["url"].startswith("https://abc--media.netlify.app/stories/"))
            self.assertNotIn(inp["dueAt"][14:16], ("00", "30"))
            self.assertIn(key, ("BUFFER_API_KEY_1", "BUFFER_API_KEY_2", "BUFFER_API_KEY_3", "BUFFER_API_KEY_4"))
        self.assertEqual(st["fanpage"]["pool"], {"f_01.jpg": self.DAY.isoformat()})
        for z in ("bananas", "fanpage"):
            kinds = {it["kind"] for zz, it, *_ in todo if zz == z}
            self.assertEqual(len(kinds), sum(1 for t in todo if t[0] == z))  # each kind once a day
            self.assertFalse({"rate", "guess"} <= kinds or {"fact_top", "fact_most"} <= kinds)
        # second run the same day: nothing new
        n = len(self.posted)
        S.build_and_post(self.DAY, ["bananas", "fanpage"], st, now + timedelta(minutes=30), False, False, self.deploy, grab)
        self.assertEqual((len(self.deploys), len(self.posted)), (1, n))
        # no secret anywhere in what we post or store
        blob = json.dumps(st) + json.dumps([P.gql_value(i) for i, _ in self.posted])
        self.assertNotIn(SECRET, blob)

    def test_late_build_skips_passed_windows(self):
        now = datetime.combine(self.DAY + timedelta(days=1), time(6, 0), P.TZ)    # only the AU window left
        P.NOW = now
        todo = S.build_and_post(self.DAY, ["bananas"], {}, now, False, False, self.deploy, mock.Mock(return_value=None))
        for _, it, *_ in todo:
            self.assertGreater(it["due"], now)

    def test_dry_run_touches_nothing(self):
        now = datetime.combine(self.DAY, time(12, 7), P.TZ)
        st = {}
        S.build_and_post(self.DAY, ["bananas", "fanpage"], st, now, False, True, self.deploy, mock.Mock(return_value=None))
        self.assertEqual((self.deploys, self.posted), ([], []))
        self.assertFalse(any(b["days"] for b in st.values()))
        self.assertTrue(list((self.tmp / "out" / "_stories").glob("*.jpg")))


class Copy(unittest.TestCase):
    def test_every_story_asks_for_a_reply(self):
        for z, kinds in S.COPY.items():
            for k, lines in kinds.items():
                self.assertTrue(any(w in lines[-1].lower() for w in S.REPLY_WORDS), (z, k, lines[-1]))
        for z, lines in S.POOL_TEXT.items():
            self.assertGreaterEqual(len(lines), 5)
            for line in lines:
                self.assertTrue(any(w in line.lower() for w in S.REPLY_WORDS), line)

    def test_follow_line_every_2nd_3rd_pool_story(self):
        days = [date(2026, 11, 19) + timedelta(days=i) for i in range(30)]
        for z in ("bananas", "fanpage"):
            got = [S.pool_line(z, "x.jpg", d.isoformat()) == S.FOLLOW_TEXT[z] for d in days]
            self.assertEqual(sum(got), 12)                                   # 2 of every 5 days
            for i in range(len(got) - 3):
                self.assertTrue(any(got[i:i + 3]), i)                        # never 3 pool stories without it
                self.assertFalse(all(got[i:i + 2]), i)                       # never twice in a row

    def test_no_tofu_in_any_copy(self):
        texts = {z: [l for lines in S.COPY[z].values() for l in lines] + S.POOL_TEXT[z]
                 + [S.FOLLOW_TEXT[z], S.PROMO_LINE, "NEW VIDEO", "NEW: GTA 6 Funniest Moments #3"]
                 for z in ("bananas", "fanpage")}
        for z, lines in texts.items():
            font = S.FONTS / S.BRAND_LOOK[z]["font"]
            for line in lines:
                for t in (line, line.upper()):
                    self.assertEqual(S.missing_glyphs(t, font), [], (z, t))
            self.assertEqual(S.missing_glyphs("Reply \U0001F525", font), ["\U0001F525"])     # the check works

    def test_new_copy_has_no_handles_or_hashtags(self):
        for line in [l for v in S.POOL_TEXT.values() for l in v] + list(S.FOLLOW_TEXT.values()) + [S.PROMO_LINE]:
            self.assertNotRegex(line, r"@|#[A-Za-z]")


class Placement(unittest.TestCase):
    def img(self):
        from PIL import Image
        return Image.new("RGB", (S.W, S.H), (90, 110, 130))

    def test_text_never_over_a_protected_box(self):
        face = (300.0, 200.0, 800.0, 1150.0)
        for z in ("bananas", "fanpage"):
            img = self.img()
            with mock.patch.object(S, "protected_boxes", return_value=[face]):
                box, _ = S.place_text(img, [("Most clipped yesterday", "head"), ("Kai Cenat", "main"),
                                            ("Agree? Reply yes or no", "sub")], z)
            self.assertTrue(box[1] > face[3] or box[3] < face[1], box)
            self.assertGreaterEqual(box[1], S.SAFE_TOP)
            self.assertLessEqual(box[3], S.H - S.SAFE_BOTTOM)

    def test_no_free_place_no_text(self):
        img = self.img()
        before = img.tobytes()
        with mock.patch.object(S, "protected_boxes", return_value=[(0.0, 0.0, float(S.W), float(S.H))]):
            self.assertIsNone(S.place_text(img, [("Reply with a name", "main")], "bananas"))
            self.assertIsNone(S.build_frame(["a", "b", "c"], "bananas", self.img()))
        self.assertEqual(img.tobytes(), before)                                   # nothing drawn

    def test_reserve_for_link_sticker_is_free_too(self):
        face = (0.0, 1200.0, float(S.W), 1500.0)
        with mock.patch.object(S, "protected_boxes", return_value=[face]):
            box, top = S.place_text(self.img(), [("NEW VIDEO", "head"), ("GTA 6 Funniest Moments #3", "main")],
                                    "bananas", reserve=300)
        self.assertTrue(top + 276 < face[1] or box[1] > face[3], (box, top))

    def test_pane_split_finds_the_facecam_seam(self):
        from PIL import ImageDraw
        img = self.img()
        ImageDraw.Draw(img).rectangle((0, 0, S.W, 640), fill=(230, 200, 220))
        self.assertIn(S.pane_split(img), range(638, 644))
        self.assertIsNone(S.pane_split(self.img()))


LONG_AT = datetime(2026, 11, 20, 10, 5, tzinfo=P.TZ)


class LongPromo(Base):
    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        self.eps, self.used = self.tmp / "long_episodes.json", self.tmp / "long_used.json"
        self.write("funny-day", 3, LONG_AT)
        self.used.write_text(json.dumps({"m1": {"znacka": "bananas", "serie": "funny-day", "datum": "2026-11-20"},
                                         "m9": {"znacka": "bananas", "serie": "funny-day", "datum": "2026-11-20",
                                                "reserve": True}}), encoding="utf-8")
        self.recs = {"m1": rec(LONG_AT - timedelta(hours=5), "kaicenat", 9)}
        self.deploys = []
        self.more = [mock.patch.object(S, "LONG_EPISODES", self.eps), mock.patch.object(S, "LONG_USED", self.used),
                     mock.patch.object(P, "records", side_effect=lambda z=None: self.recs),
                     mock.patch.object(S, "POOL_DIR", self.tmp / "pool"), mock.patch.object(S, "HERE", self.tmp),
                     mock.patch.object(S, "yt_published", side_effect=lambda z, title, since: self.seen)]
        self.seen = LONG_AT + timedelta(minutes=20)       # when the video showed up on the brand's YouTube channel
        for p in self.more:
            p.start()

    def tearDown(self):
        for p in self.more:
            p.stop()
        shutil.rmtree(self.tmp)
        super().tearDown()

    def write(self, serie, n, at):
        self.eps.write_text(json.dumps({"bananas": {serie: n}, "_last": {"bananas": {
            "serie": serie, "at": at.isoformat(timespec="seconds"), "next": 1}}}), encoding="utf-8")

    def deploy(self, files):
        self.deploys.append(dict(files))
        return "https://abc--media.netlify.app"

    def run_once(self, bs, now, dry=False):
        P.NOW = now
        grab = mock.Mock(return_value=None)
        return S.do_long_promo("bananas", bs, now, False, dry, self.deploy, grab)

    def test_once_per_long_form_after_the_publish_time(self):
        import kompilace as KP
        info = S.long_latest("bananas")
        self.assertEqual(info["title"], "GTA 6 Funniest Moments #3")
        publish = max(KP.asap_slot("bananas", "funny-day", LONG_AT + timedelta(minutes=b)) for b in S.LONG_BUILD_MIN)
        self.assertEqual(info["publish"], publish)
        bs = S.bstate({}, "bananas")
        due = self.run_once(bs, LONG_AT + timedelta(hours=1))
        self.assertGreaterEqual(due - self.seen, timedelta(hours=2))          # 2 h after the REAL upload
        self.assertLessEqual(due - self.seen, timedelta(hours=2, minutes=30))
        self.assertNotIn(due.minute, (0, 30))
        self.assertEqual(len(self.deploys), 1)
        inp = self.posted[0][0]
        self.assertEqual(str(inp["metadata"]["instagram"]["type"]), "story")
        self.assertTrue(inp["assets"][0]["image"]["url"].startswith("https://abc--media.netlify.app/stories/"))
        self.assertIn("GTA 6 Funniest Moments #3", inp["assets"][0]["image"]["metadata"]["altText"])
        n = len(self.posted)
        self.assertIsNone(self.run_once(bs, LONG_AT + timedelta(hours=1, minutes=30)))     # not twice
        self.assertEqual((len(self.posted), len(self.deploys)), (n, 1))
        self.write("funny-day", 4, LONG_AT + timedelta(days=2))                             # the next long-form
        self.seen = LONG_AT + timedelta(days=2, minutes=20)
        self.assertIsNotNone(self.run_once(bs, LONG_AT + timedelta(days=2, hours=1)))
        self.assertEqual(len(bs["long"]), 2)
        self.assertNotIn(SECRET, json.dumps(bs))
    def test_failed_promo_retried_with_a_fresh_time(self):
        bs = S.bstate({}, "bananas")
        now = LONG_AT + timedelta(hours=1)
        with mock.patch.object(P, "create_post", side_effect=RuntimeError("Buffer down")):
            self.run_once(bs, now)
        r = list(bs["long"].values())[0]
        self.assertEqual((r["fails"], r["posts"]), (1, {}))
        later = datetime.fromisoformat(r["due"]) + timedelta(minutes=20)       # its time has passed
        P.NOW = later
        S.retry_failed("bananas", bs, later, False)
        self.assertTrue(r.get("done"))
        self.assertEqual(len(self.posted), 2)
        self.assertGreater(datetime.fromisoformat(r["due"]), later)
        self.assertNotIn(datetime.fromisoformat(r["due"]).minute, (0, 30))

    def test_too_late_is_skipped_and_dry_touches_nothing(self):
        bs = S.bstate({}, "bananas")
        self.assertIsNotNone(self.run_once(bs, LONG_AT + timedelta(hours=1), dry=True))
        self.assertEqual((bs["long"], self.posted, self.deploys), ({}, [], []))
        self.assertTrue(list((self.tmp / "out" / "_stories").glob("bananas_long_*.jpg")))
        self.assertIsNone(self.run_once(bs, LONG_AT + timedelta(days=1)))
        self.assertEqual(list(bs["long"].values())[0]["skip"], "too late after the upload")
        self.assertEqual(self.posted, [])

    def test_waits_until_the_video_is_on_youtube(self):
        bs = S.bstate({}, "bananas")
        self.seen = None                                  # e-mailed, not uploaded yet: no story, nothing stored
        self.assertIsNone(self.run_once(bs, LONG_AT + timedelta(hours=3)))
        self.assertEqual((bs["long"], self.posted), ({}, []))
        self.seen = LONG_AT + timedelta(hours=10)         # Ales uploads later: the story follows that
        due = self.run_once(bs, LONG_AT + timedelta(hours=10, minutes=5))
        self.assertGreaterEqual(due - self.seen, timedelta(hours=2))

    def test_no_long_form_no_promo(self):
        self.eps.write_text("{}", encoding="utf-8")
        bs = S.bstate({}, "bananas")
        self.assertIsNone(self.run_once(bs, LONG_AT))
        self.assertEqual((bs["long"], self.posted), ({}, []))

    def test_titles_without_names(self):
        self.recs = {}
        self.write("streamer-night", 2, LONG_AT)
        self.assertEqual(S.long_latest("bananas")["title"], "Night in GTA 6 Vice City #2")
        self.write("banana-league", 5, LONG_AT)
        self.assertEqual(S.long_latest("bananas")["title"], "GTA 6 Banana League Week #5 - Streamer Tier List")
        self.assertEqual(S.short_title("GTA 6 Wildest Moments #2 (Kai Cenat, xQc)"), "GTA 6 Wildest Moments #2")


class LinkStory(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def meta(self, frame=True):
        from PIL import Image
        nv = self.tmp / "nv"
        nv.mkdir(exist_ok=True)
        if frame:
            Image.new("RGB", (1920, 1080), (120, 90, 60)).save(nv / "frame.jpg")
        elif (nv / "frame.jpg").exists():
            (nv / "frame.jpg").unlink()
        return {"znacka": "fanpage", "kanal": "gta6fanpageclip", "serie": "wild-day",
                "title": "GTA 6 Wildest Moments #2 (Kai Cenat, xQc)", "description": "d", "tags": ["gta 6"],
                "nahrat": "2026-11-20T19:47:00+01:00", "duration_s": 600, "nahled_vstup": str(nv),
                "video": str(self.tmp / "2026-11-20_wild-day.mp4")}

    def test_link_story_image(self):
        from PIL import Image
        path, where = S.link_story(self.meta())
        self.assertEqual(path.name, "2026-11-20_wild-day_story_odkaz.jpg")
        with Image.open(path) as im:
            self.assertEqual(im.size, (S.W, S.H))
        self.assertTrue(where.startswith("dolů"))
        with mock.patch.object(S, "POOL_DIR", self.tmp / "nopool"):
            self.assertIsNotNone(S.link_story(self.meta(frame=False), self.tmp / "b.jpg"))

    def test_mail_attaches_it_with_the_step(self):
        import posli_mail as M
        msg, body, files = M.build(self.meta(), "https://github.com/x")
        self.assertIn("story_odkaz.jpg", [n for n, _ in files])
        self.assertIn("nálepka Odkaz", body)
        self.assertIn("story_odkaz.jpg", [p.get_filename() for p in msg.iter_attachments()])
        with mock.patch("stories.link_story", side_effect=RuntimeError("boom")):
            msg, body, files = M.build(self.meta(), "")                         # the e-mail still goes out
        self.assertNotIn("story_odkaz.jpg", [n for n, _ in files])
        self.assertNotIn("nálepka Odkaz", body)

class StaleState(Base):
    """18. 11.: two runs reposted the same reel (the second run read a stale stories_state.json)."""
    NOW = datetime(2026, 11, 19, 20, 0, tzinfo=P.TZ)

    def test_merge_origin_wins_and_keeps_both_posts(self):
        local = {"bananas": {"reposts": {"c1": {"posts": {"ig_b": "pA"}, "due": "x"}, "c2": {"posts": {}}},
                             "days": {}, "pool": {"a.jpg": "2026-11-18"}}}
        remote = {"bananas": {"reposts": {"c1": {"posts": {"fb_b": "pB"}, "done": True}},
                              "pool": {"b.jpg": "2026-11-19"}}, "fanpage": {"reposts": {}}}
        m = S.merge_state(local, remote)
        self.assertEqual(m["bananas"]["reposts"]["c1"], {"posts": {"ig_b": "pA", "fb_b": "pB"}, "due": "x", "done": True})
        self.assertIn("c2", m["bananas"]["reposts"])
        self.assertEqual(m["bananas"]["pool"], {"a.jpg": "2026-11-18", "b.jpg": "2026-11-19"})
        self.assertIn("fanpage", m)

    def test_sync_reads_origin_only_under_the_lock(self):
        tmp = Path(tempfile.mkdtemp())
        remote = {"bananas": {"reposts": {"c": {"posts": {"ig_b": "p1", "fb_b": "p2"}, "done": True}}}}
        calls = []
        def git(*args, **kw):
            calls.append(args)
            return (0, json.dumps(remote), "") if args[0] == "show" else (0, "", "")
        try:
            with mock.patch.object(S, "HERE", tmp), mock.patch.object(P, "git", side_effect=git):
                self.assertEqual(S.sync_state({"x": 1}), {"x": 1})            # no lock file: nothing fetched
                self.assertEqual(calls, [])
                (tmp / P.LOCK_UNTIL).write_text("9999999999")
                st = S.sync_state({})
                self.assertTrue(st["bananas"]["reposts"]["c"]["done"])
                self.assertEqual(calls[-1], ("show", "origin/main:stories_state.json"))
                with mock.patch.object(P, "git", return_value=(1, "", "no network")):
                    self.assertEqual(S.sync_state({"x": 1}), {"x": 1})        # git fails: local state
            # the stale run decides on the merged state: no second repost of the same reel
            P.NOW = self.NOW
            with mock.patch.object(P, "records", return_value={"c": rec(self.NOW)}):
                self.assertEqual(S.do_reposts("bananas", S.bstate(st, "bananas"), self.NOW, False, False), [])
            self.assertEqual(self.posted, [])
        finally:
            shutil.rmtree(tmp)

    def test_posts_on_every_channel_means_done(self):
        bs = S.bstate({}, "bananas")
        bs["reposts"]["c"] = {"due": self.NOW.isoformat(), "posts": {"ig_b": "p1", "fb_b": "p2"}}   # no done flag
        P.NOW = self.NOW
        with mock.patch.object(P, "records", return_value={"c": rec(self.NOW)}):
            self.assertEqual(S.do_reposts("bananas", bs, self.NOW, False, False), [])
        self.assertEqual(self.posted, [])
        bs["reposts"]["c"]["posts"].pop("fb_b")                             # only the missing channel is posted
        with mock.patch.object(P, "records", return_value={"c": rec(self.NOW)}),                 mock.patch.object(S, "texted_reel", side_effect=lambda rec, z, cid, day: rec["url"]):
            S.do_reposts("bananas", bs, self.NOW, False, False)
        self.assertEqual([i["channelId"] for i, _ in self.posted], ["fb_b"])


if __name__ == "__main__":
    unittest.main()
