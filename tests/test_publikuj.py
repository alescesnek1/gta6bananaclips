"""
Regression tests from the pre-launch audit (17. 11.): no network, nothing published.
  .venv/Scripts/python -m unittest tests.test_publikuj
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
