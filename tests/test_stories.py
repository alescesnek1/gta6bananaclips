"""
stories.py (18. 11.): plan times, caps, never :00 / :30, repost timing, pool rotation, Buffer input,
no secrets. No network, nothing published - Buffer / Netlify / ffmpeg are mocked.
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
        with mock.patch.object(P, "records", return_value=recs):
            return S.do_reposts("bananas", bs, P.NOW, draft, dry=False)

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
        self.assertEqual(lines[1:], ["Kai Cenat", "3 clips on our page"])
        lines, _ = S.fill("fact_top", "bananas", st)                       # >= 2 clips to lead
        self.assertEqual(lines[1:], ["Kai Cenat", "Banana score 8/10 from 3 clips"])
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


if __name__ == "__main__":
    unittest.main()
