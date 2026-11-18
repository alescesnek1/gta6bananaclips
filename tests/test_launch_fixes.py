"""
Review fixes before the gameplay launch (18. 11.): hot pacing, the 9th / 10th post after 20:00,
experiment items never twice, no post into the past, captions (no @, X length), live-inbox order.
No network, nothing published.
  .venv/Scripts/python -m unittest tests.test_launch_fixes
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from argparse import Namespace
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import publikuj as P                                       # noqa: E402

TZ = P.TZ
SRC = json.loads((HERE / "sources.json").read_text(encoding="utf-8"))
HS = SRC.get("hot_score", 2000)


def at(d, hm, plus_days=0):
    h, m = map(int, hm.split(":"))
    return datetime.combine(d + timedelta(days=plus_days), datetime.min.time(), TZ).replace(hour=h, minute=m)


def clip(cid, channel="streamer1", score=HS + 100, live=False, z=0, age_h=0.5):
    return {"id": cid, "channel": channel, "platform": "twitch", "created": 1, "age_h": age_h,
            "score": score, "live": live, "spike_z": z}


class HotPacing(unittest.TestCase):
    D = date(2026, 11, 19)

    def test_max_two_per_run(self):
        now = at(self.D, "21:00")
        cands = [clip(f"c{i}", score=HS * 2) for i in range(6)]
        self.assertEqual(len(P.hot_pick(cands, {}, now, 10, SRC)), 1)        # the 2nd is within 30 min of the 1st
        strong = [clip(f"s{i}", score=HS * 3 + 1) for i in range(6)]
        self.assertEqual(len(P.hot_pick(strong, {}, now, 10, SRC)), SRC.get("hot_per_run", 2))

    def test_gap_after_last_hot_post(self):
        now = at(self.D, "21:00")
        recs = {"old": {"due": (now - timedelta(minutes=10)).isoformat(), "hot": True}}
        self.assertEqual(P.hot_pick([clip("a")], recs, now, 10, SRC), [])
        self.assertEqual([m["id"] for m in P.hot_pick([clip("b", score=HS * 3 + 1)], recs, now, 10, SRC)], ["b"])
        recs = {"old": {"due": (now - timedelta(minutes=31)).isoformat(), "hot": True}}
        self.assertEqual([m["id"] for m in P.hot_pick([clip("a")], recs, now, 10, SRC)], ["a"])

    def test_star_first(self):
        now = at(self.D, "21:00")
        star = SRC["star_streamers"][0]
        cands = [clip("viral", score=HS * 3 + 1), clip("star", channel=star, score=SRC.get("star_min_score", 300))]
        self.assertEqual(P.hot_pick(cands, {}, now, 10, SRC)[0]["id"], "star")

    def test_live_paced_20_min(self):
        now = at(self.D, "22:00")
        recs = {"l": {"due": (now - timedelta(minutes=12)).isoformat(), "hot": True, "live": True}}
        self.assertEqual(P.hot_pick([clip("live_x_1", live=True, z=12)], recs, now, 10, SRC), [])
        self.assertEqual(len(P.hot_pick([clip("live_x_2", live=True, z=SRC.get("hot_bypass_z", 25) + 1)],
                                        recs, now, 10, SRC)), 1)
        recs["l"]["due"] = (now - timedelta(minutes=SRC.get("live_gap_min", 20) + 1)).isoformat()
        self.assertEqual(len(P.hot_pick([clip("live_x_1", live=True, z=12)], recs, now, 10, SRC)), 1)


class NinthTenth(unittest.TestCase):
    def test_hot_room_released_after_20(self):
        d = date(2026, 11, 26)
        recs = {f"c{i}": {"due": at(d, "14:05").isoformat()} for i in range(8)}
        # hot_room only: 8 posts at 14:05 are far over the day-long pace (tested in DayPace), so it is off here
        with mock.patch.object(P, "daily_quota", lambda day: 8), mock.patch.object(P, "max_per_day", lambda day: 10), \
                mock.patch.object(P, "budget_level", lambda: 0), mock.patch.object(P, "pace_fits", lambda *a, **k: True):
            before = P.day_plan(d, at(d, "19:00"), recs)
            after = P.day_plan(d, at(d, "20:05"), recs)
        self.assertEqual(before["slots"], 0)                     # 9th / 10th kept for hot clips until 20:00
        self.assertEqual(before["cap"], 2)                       # ... which may always use them
        self.assertGreater(after["slots"], 0)                    # after 20:00 a strong clip may take them
        self.assertLessEqual(after["slots"], 2)


class DayPace(unittest.TestCase):
    """Day-long pacing (Aleš 18. 11.): the curve, the evening reserve, exceptional clips, upgrade log."""
    D = date(2026, 11, 26)

    def lim(self, hm, plus=0, hot=False):
        return P.pace_limit(self.D, at(self.D, hm, plus), SRC, hot)

    def test_curve_and_reserve(self):
        with mock.patch.object(P, "max_per_day", lambda day: 10):
            pts = [at(self.D, "12:00") + timedelta(minutes=15 * i) for i in range(96)]
            for hot in (False, True):
                lims = [P.pace_limit(self.D, t, SRC, hot) for t in pts]
                self.assertEqual(lims, sorted(lims))                          # never goes down
            self.assertLessEqual(self.lim("14:00", hot=True), 3)               # an early explosion: max 3
            self.assertLessEqual(self.lim("21:59", hot=True), 7)               # 3 stay free until 22:00
            self.assertLessEqual(self.lim("02:59", 1, hot=True), 9)            # 1 until 03:00
            self.assertEqual(self.lim("11:59", 1), 10)                         # the whole day by the end
            self.assertLess(self.lim("18:00"), self.lim("18:00", hot=True))   # the burst is for hot clips
            # the curve follows the audience: 05:00-08:00 adds little, 20:00-01:00 much
            self.assertLessEqual(self.lim("08:00", 1) - self.lim("05:00", 1), 1)
            off = {**SRC, "pace_weights": [], "reserve_until": {}}
            self.assertEqual(P.pace_limit(self.D, at(self.D, "13:00"), off), 10)

    def test_fits_counts_later_posts(self):
        with mock.patch.object(P, "max_per_day", lambda day: 10):
            now = at(self.D, "21:00")
            # 6 posts already and one scheduled at 21:40: a hot one now must keep 21:40 under the curve too
            dues = [at(self.D, "14:10"), at(self.D, "17:10"), at(self.D, "18:40"), at(self.D, "19:50"),
                    at(self.D, "20:10"), at(self.D, "20:40"), at(self.D, "21:40")]
            self.assertFalse(P.pace_fits(dues, now, self.D, SRC, hot=True))   # 8 before 22:00 > 10 - 3
            self.assertTrue(P.pace_fits(dues[:5], now, self.D, SRC, hot=True))

    def test_exceptional(self):
        star = SRC["star_streamers"][0]
        self.assertTrue(P.exceptional(clip("a", score=4 * HS), SRC))
        self.assertFalse(P.exceptional(clip("b", score=3.9 * HS), SRC))
        self.assertTrue(P.exceptional(clip("c", channel=star, score=2 * HS), SRC))
        self.assertFalse(P.exceptional(clip("d", channel=star, score=1.9 * HS), SRC))
        lz = SRC.get("exceptional", {}).get("live_z", 80)
        self.assertTrue(P.exceptional(clip("live_e", live=True, z=lz, score=0), SRC))
        self.assertFalse(P.exceptional(clip("live_f", live=True, z=lz - 1, score=0), SRC))

    def test_hot_pick_paced_but_exceptional_breaks_it(self):
        with mock.patch.object(P, "max_per_day", lambda day: 10):
            now = at(self.D, "13:30")
            recs = {f"r{i}": {"due": at(self.D, "13:0%d" % i).isoformat(), "hot": True} for i in range(3)}
            for r in recs.values():
                r["due"] = (now - timedelta(minutes=40)).isoformat()          # out of the 30 min gap
            blocked = []
            self.assertEqual(P.hot_pick([clip("x", score=2 * HS)], recs, now, 10, SRC, day=self.D, blocked=blocked), [])
            self.assertEqual([m["id"] for m in blocked], ["x"])                # the pace held it back
            big = P.hot_pick([clip("y", score=4 * HS)], recs, now, 10, SRC, day=self.D)
            self.assertEqual([m["id"] for m in big], ["y"])                    # exceptional: through
            full = {f"f{i}": {"due": (now - timedelta(minutes=40 + i)).isoformat()} for i in range(10)}
            self.assertEqual(P.hot_pick([clip("z", score=9 * HS)], full, now, 0, SRC, day=self.D), [])  # never > max

    def test_upgrade_only_logged(self):
        now = at(self.D, "21:00")
        recs = {"weak": {"due": at(self.D, "22:10").isoformat(), "score": 900, "hot": False},
                "done": {"due": at(self.D, "20:10").isoformat(), "score": 100}}
        with mock.patch.object(P, "log", lambda msg: None), contextlib.redirect_stdout(io.StringIO()):
            notes = P.upgrade_note([clip("big", score=3000)], recs, now, SRC)
            self.assertEqual(len(notes), 1)
            self.assertIn("weak", notes[0])
            self.assertIn("approval", notes[0])
            self.assertEqual(P.upgrade_note([clip("mid", score=2000)], recs, now, SRC), [])   # < 3x: nothing
        self.assertIn("weak", recs)                                            # nothing deleted


class TitlesAndStars(unittest.TestCase):
    def test_title_from_real_tags_only(self):
        m = {"id": "c1", "channel": "kaicenat", "platform": "twitch", "tags": ["crash", "wow"],
             "tag_scores": {"crash": 0.8, "wow": 0.3}}
        P.set_brand("bananas")
        t = P.title_line(m)
        self.assertTrue(t.startswith("Kai Cenat "))
        self.assertTrue(any(ph in t for ph in P.TITLE_ACTIONS["crash"]))
        m["tag_scores"]["crash"] = 0.2                                         # weak tag: claim no event
        self.assertTrue(any(ph in P.title_line(m) for ph in P.TITLE_NEUTRAL))
        self.assertTrue(any(ph in P.title_line({"id": "c2", "channel": "xqc"}) for ph in P.TITLE_NEUTRAL))

    def test_brands_differ_and_lengths(self):
        m = {"id": "c3", "channel": "rayasianboy", "platform": "twitch", "tags": ["glitch"],
             "tag_scores": {"glitch": 0.9}, "quote": "bro what is happening " * 3}
        out = {}
        with mock.patch.object(P, "league_line", lambda m: ""):
            for brand in ("bananas", "fanpage"):
                P.set_brand(brand)
                out[brand] = P.youtube_title(m)
                self.assertLessEqual(len(out[brand]), 100)
                self.assertTrue(out[brand].endswith(" #shorts"))
                for svc in P.NETS:
                    text = P.caption(m, svc)
                    self.assertTrue(text.startswith("RayAsianBoy "), f"{brand} {svc}")   # the title line first
                    self.assertNotIn("@", text)
                    if svc in ("twitter", "threads"):
                        self.assertLessEqual(P.x_len(text), 270)
                    else:
                        self.assertIn("#rayasianboy", text)
        P.set_brand("bananas")
        self.assertNotEqual(out["bananas"], out["fanpage"])

    def test_new_stars_and_crossover(self):
        for login in ("n3on", "rakai", "rayasianboy", "kelo"):
            self.assertIn(login, SRC["star_streamers"])
        self.assertTrue(P.crossover({"title": "Kai Cenat and xQc race in GTA 6"}, SRC))
        self.assertFalse(P.crossover({"title": "Kai Cenat drives into the sketch shop"}, SRC))   # 'sketch' = a word
        self.assertTrue(P.crossover({"title": "kaicenat meets Sketch"}, SRC))
        self.assertFalse(P.crossover({"title": "xqc alone"}, SRC))


class ExpItemsNeverTwice(unittest.TestCase):
    def test_clip_in_experiment_is_skipped(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            old = (P.OUT, P.HERE, P.inbox_items, P.BRANDS)
            try:
                P.HERE, P.OUT, P.BRANDS = t, t / "out", t / "brands.json"
                (t / "brands.json").write_text(json.dumps({
                    "bananas": {"published": "pb.json"}, "fanpage": {"published": "pf.json"}}))
                (t / "sources.json").write_text(json.dumps({"game": "Grand Theft Auto VI"}))
                (t / "pf.json").write_text(json.dumps({"exp_2026-11-19_story": {
                    "due": "2026-11-19T22:15:00+01:00", "platform": "exp", "items": ["clip1"]}}))
                d = t / "out" / "kai" / "x_clip1"
                d.mkdir(parents=True)
                (d / "short.mp4").write_bytes(b"x")
                (d / "meta.json").write_text(json.dumps({
                    "id": "clip1", "channel": "kai", "platform": "twitch", "category": "Grand Theft Auto VI",
                    "cam": [1, 1, 1, 1], "views": 5000, "shorts": {"bananas": "short.mp4"},
                    "tag_scores": {"chase": 0.8}}))
                P.inbox_items = lambda: []
                with mock.patch.object(P, "log", lambda msg: None):
                    P.set_brand("bananas")
                    self.assertEqual([m["id"] for m in P.candidates()], [])          # used by the other account's exp
                    (t / "pf.json").write_text("{}")
                    self.assertEqual([m["id"] for m in P.candidates()], ["clip1"])
                    (t / "publikace_chyby.json").write_text(json.dumps({"clip1": P.MAX_FAILS}))
                    self.assertEqual([m["id"] for m in P.candidates()], [])          # failed twice: skipped
            finally:
                P.OUT, P.HERE, P.inbox_items, P.BRANDS = old
                P.set_brand("bananas")


class NoPastDue(unittest.TestCase):
    def test_recompute(self):
        try:
            for hm in ("21:00", "21:28", "21:57", "13:12"):
                P.NOW = at(date(2026, 11, 19), hm)
                t = P.fresh_due(P.NOW + timedelta(minutes=1))
                self.assertGreaterEqual(t, P.NOW + timedelta(minutes=3))
                self.assertLessEqual(t, P.NOW + timedelta(minutes=7))
                self.assertNotIn(t.minute, (0, 30))
                later = P.NOW + timedelta(minutes=40)
                self.assertEqual(P.fresh_due(later), later)                     # a future slot stays
                for svc in P.NETS:
                    now_, due = P.hot_stagger("c1", svc, P.NOW)
                    self.assertTrue(now_ or (P.NOW < due <= P.NOW + timedelta(minutes=6) and due.minute not in (0, 30)))
        finally:
            P.NOW = None


class Captions(unittest.TestCase):
    def test_no_at_and_x_length(self):
        long_q = "no way bro " * 30 + "@someone 💀💀💀"
        for brand in ("bananas", "fanpage"):
            P.set_brand(brand)
            for plat in ("twitch", "youtube", "kick"):
                m = {"id": "a1", "channel": "kaicenat", "platform": plat, "quote": long_q, "rating": 7,
                     "chat_x": 14, "live": True}
                with mock.patch.object(P, "league_line", lambda m: ""):
                    for svc in P.NETS:
                        text = P.caption(m, svc)
                        self.assertNotIn("@", text, f"{brand} {plat} {svc}")
                        if svc in ("twitter", "threads"):
                            self.assertLessEqual(P.x_len(text), 280, f"{brand} {plat} {svc}")
        P.set_brand("bananas")

    def test_junk_quote_dropped(self):
        P.set_brand("bananas")
        m = {"id": "a2", "channel": "kai", "platform": "twitch", "quote": "the GTA 6 leak was real"}
        with mock.patch.object(P, "league_line", lambda m: ""):
            self.assertNotIn("leak", P.caption(m, "instagram"))

    def test_x_weights(self):
        self.assertEqual(P.x_len("twitch.tv/kaicenat"), 23)
        self.assertEqual(P.x_len("💀"), 2)
        self.assertEqual(P.x_len("abc"), 3)

    def test_exp_caption_fits_x(self):
        P.set_brand("fanpage")
        m = {"id": "exp_x", "platform": "exp", "concept": "multi_pov",
             "items": [{"id": f"i{k}", "channel": f"streamer_with_a_long_name_{k}", "platform": "twitch"} for k in range(12)]}
        self.assertLessEqual(P.x_len(P.caption(m, "twitter")), 280)
        P.set_brand("bananas")


class LiveInbox(unittest.TestCase):
    def test_newest_first_old_dropped(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            old = (P.OUT, P.HERE, P.BRANDS)
            sent = {}
            try:
                P.HERE, P.OUT, P.BRANDS = t, t / "out", t / "brands.json"
                (t / "brands.json").write_text(json.dumps({
                    "bananas": {"published": "pb.json", "max_per_day": 10, "ramp": {"2026-11-01": 8}},
                    "fanpage": {"published": "pf.json", "max_per_day": 10, "ramp": {"2026-11-01": 8}}}))
                (t / "sources.json").write_text(json.dumps({"game": "Grand Theft Auto VI", "hot_max_age_h": 3}))
                now = time.time()
                for i, age_h in enumerate((0.2, 5, 1.0, 0.5)):
                    cid = f"live_s{i}_{i}"
                    d = t / "out" / f"s{i}" / cid
                    d.mkdir(parents=True)
                    (d / "short.mp4").write_bytes(b"x")
                    (d / "meta.json").write_text(json.dumps({
                        "id": cid, "channel": f"s{i}", "platform": "twitch", "category": "Grand Theft Auto VI",
                        "live": True, "views": 0, "created": int(now - age_h * 3600), "shorts": {"bananas": "short.mp4"}}))

                def deploy(files, inbox, title=None, essential=False):
                    sent["items"] = json.loads(inbox)
                    return "https://inbox.test"
                with mock.patch.object(P, "inbox_deploy", deploy), mock.patch.object(P, "refresh_records", lambda: None), \
                        mock.patch.object(P, "log", lambda msg: None), \
                        mock.patch.dict(os.environ, {"NETLIFY_AUTH_TOKEN": "x", "NETLIFY_SITE_ID": "x"}), \
                        contextlib.redirect_stdout(io.StringIO()):
                    P.set_brand("bananas")
                    P.cmd_schranka(Namespace(dni=3, uklid=False, max=None, nazev="live-inbox", prefix="live_"))
                self.assertEqual([m["id"] for m in sent["items"]], ["live_s0_0", "live_s3_3", "live_s2_2"])
            finally:
                P.OUT, P.HERE, P.BRANDS = old
                P.set_brand("bananas")


if __name__ == "__main__":
    unittest.main()
