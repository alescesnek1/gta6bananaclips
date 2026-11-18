"""
Long-form guards before the first GTA 6 run (18. 11.): the first-reactions window, sentence-safe cuts
(start / end / jump / cold open / beat), the league subset, crash -> failed note, titles / tags /
description limits, the QA of the texts and the state roll-back. Pure helpers + mocked build, no
ffmpeg / Whisper / network.
  .venv/Scripts/python -m unittest tests.test_long
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from argparse import Namespace
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tools" / "long"))
import kompilace as C                                      # noqa: E402
import klipuj as K                                         # noqa: E402
import zabava                                              # noqa: E402

PEAK = 20.0
# "We go now." | "Look at that car." | "He is crazy" (0.6 s pause) | ... payoff ... "Oh my god!" "No way."
W = [(8.0, 8.3, "We"), (8.3, 8.6, "go"), (8.6, 9.0, "now."),
     (10.2, 10.5, "Look"), (10.5, 10.7, "at"), (10.7, 11.0, "that"), (11.0, 11.4, "car."),
     (12.0, 12.4, "He"), (12.4, 12.7, "is"), (12.7, 13.4, "crazy"),
     (14.0, 14.3, "Hold"), (14.3, 14.6, "on"), (14.6, 15.0, "bro."),
     (16.0, 16.3, "Watch"), (16.3, 16.6, "this"), (16.6, 17.0, "one"), (17.1, 17.5, "please."),
     (20.5, 20.8, "Oh"), (20.8, 21.0, "my"), (21.0, 21.6, "god!"),
     (22.6, 22.9, "No"), (22.9, 23.2, "way.")]


def assert_safe(tc, cutter, s, e):
    tc.assertTrue(cutter.ok_start(s), f"start {s} not safe")
    tc.assertTrue(cutter.ok_end(e), f"end {e} not safe")
    tc.assertIsNone(K.in_word(cutter.w, s))
    tc.assertIsNone(K.in_word(cutter.w, e))


class Window(unittest.TestCase):
    def a(self, **kw):
        return Namespace(od=None, do=None, hra=None, **kw)

    def test_game_out_timestamp(self):                     # 14
        self.assertEqual(datetime.fromtimestamp(C.GAME_OUT_TS, timezone.utc),
                         datetime(2026, 11, 18, 11, 0, tzinfo=timezone.utc))

    def test_first_reactions_window_not_empty(self):       # 1: tonight's run (19. 11.) makes 20. 11.
        od, do, t0, t1 = C.window_for("first-reactions", date(2026, 11, 19), self.a())
        self.assertEqual(od, date(2026, 11, 18))
        self.assertEqual(do, date(2026, 11, 18))
        self.assertEqual(t0, C.GAME_OUT_TS)
        self.assertGreater(t1 - t0, 11 * 3600)

    def test_daily_window_clamped_to_unlock(self):         # 14
        od, do, t0, t1 = C.window_for("funny-day", date(2026, 11, 19), self.a())
        self.assertEqual(t0, C.GAME_OUT_TS)
        od, do, t0, t1 = C.window_for("funny-day", date(2026, 11, 21), self.a())
        self.assertEqual(od, date(2026, 11, 20))
        self.assertGreater(t0, C.GAME_OUT_TS)

    def test_test_category_not_clamped(self):
        a = Namespace(od="2026-11-16", do="2026-11-17", hra="Grand Theft Auto V")
        od, do, t0, t1 = C.window_for("funny-day", date(2026, 11, 19), a)
        self.assertLess(t0, C.GAME_OUT_TS)


class SnapSegment(unittest.TestCase):                      # 5, 6
    def test_normal_moment_is_sentence_safe(self):
        c = zabava.Cutter(W, 30.0)
        s, e = zabava.snap_segment(W, (0.0, 30.0), PEAK, 30.0, c)
        assert_safe(self, c, s, e)
        self.assertTrue(PEAK - 12.6 <= s <= PEAK - 7.0)
        self.assertTrue(PEAK + 1.5 <= e <= PEAK + 3.7)

    def test_no_start_in_window_searches_back_to_15s(self):
        # one long sentence 6.0-19.0 (no punctuation, tiny gaps): only its start at 6.0 is a sentence start
        w = [(6.0 + 0.4 * i, 6.3 + 0.4 * i, "word") for i in range(33)] + [(20.5, 21.0, "boom!")]
        c = zabava.Cutter(w, 30.0)
        s, e = zabava.snap_segment(w, (0.0, 30.0), PEAK, 30.0, c)
        self.assertLess(s, 6.0)
        self.assertGreaterEqual(s, PEAK - 15.0)
        assert_safe(self, c, s, e)

    def test_impossible_start_drops_the_moment(self):
        # talking from 2 s to the payoff without a sentence start after 4.5 s: no safe start -> None
        w = [(2.0 + 0.4 * i, 2.3 + 0.4 * i, "word") for i in range(45)]
        c = zabava.Cutter(w, 30.0)
        self.assertIsNone(zabava.snap_segment(w, (0.0, 30.0), PEAK, 30.0, c))

    def test_source_end_mid_sentence(self):
        # the viewer's clip ends while he talks: never end there, take the last sentence end before it
        w = W[:17] + [(20.2, 20.6, "Wow."), (21.4, 21.7, "this"), (21.7, 21.98, "is")]
        c = zabava.Cutter(w, 22.0)
        self.assertFalse(c.ok_end(22.0))
        s, e = zabava.snap_segment(w, (0.0, 22.0), PEAK, 22.0, c)
        self.assertLess(e, 21.4)
        assert_safe(self, c, s, e)

    def test_source_end_mid_sentence_no_way_out(self):
        w = W[:17] + [(20.2, 20.6, "this"), (20.6, 21.0, "is"), (21.0, 21.98, "so")]
        c = zabava.Cutter(w, 22.0)
        self.assertIsNone(zabava.snap_segment(w, (0.0, 22.0), PEAK, 22.0, c))

    def test_source_start_while_talking_is_not_a_start(self):
        w = [(0.1, 0.4, "and"), (0.4, 0.8, "then."), (1.5, 1.8, "Go")]
        c = zabava.Cutter(w, 10.0)
        self.assertFalse(c.ok_start(0.0))
        self.assertTrue(c.ok_start(1.38))

    def test_loud_pause_is_not_a_cut_point(self):          # 13: speech Whisper dropped
        rt = np.arange(0, 30, 0.025) + 0.025
        rms = np.full(rt.size, 100.0)
        for w0, w1, _ in W:
            rms[(rt >= w0) & (rt <= w1)] = 1000.0
        rms[(rt >= 9.2) & (rt <= 10.15)] = 1000.0          # someone talks in the pause before "Look"
        c = zabava.Cutter(W, 30.0, rt, rms).window(5.0, 28.0)
        self.assertFalse(c.ok_start(10.08))
        self.assertTrue(c.ok_start(11.75))                 # "He" after a quiet pause
        s, e = zabava.snap_segment(W, (0.0, 30.0), PEAK, 30.0, zabava.Cutter(W, 30.0, rt, rms))
        self.assertFalse(9.15 <= s <= 10.2)


class Jumps(unittest.TestCase):                            # 12
    def test_jump_only_after_a_finished_sentence(self):
        c = zabava.Cutter(W, 30.0)
        self.assertTrue(c.ok_jump(9.3, 10.08))             # "now." | "Look"
        self.assertTrue(c.ok_jump(13.6, 13.9))             # "crazy" (0.6 s pause) | "Hold": capital = new sentence
        w = W[:10] + [(14.0, 14.3, "and")] + W[11:]
        self.assertFalse(zabava.Cutter(w, 30.0).ok_jump(13.6, 13.9))   # lowercase "and": the same sentence
        self.assertFalse(c.ok_jump(10.6, 10.65))           # inside "Look at"

    def test_two_halves_of_one_sentence_never_joined(self):
        w = [(1.0, 1.4, "I"), (1.4, 1.8, "was"), (1.8, 2.2, "going"), (5.0, 5.3, "to"), (5.3, 5.8, "say.")]
        c = zabava.Cutter(w, 10.0)
        self.assertFalse(c.ok_jump(2.6, 4.7))              # 2.8 s pause inside the sentence
        self.assertEqual(c.safe_pieces([(0.5, 2.6), (4.7, 6.0)]), [(0.5, 6.0)])

    def test_report(self):
        c = zabava.Cutter(W, 30.0)
        self.assertEqual(c.report([(10.08, 11.7), (11.75, 15.3)]), [])
        self.assertTrue(c.report([(10.6, 15.3)]))          # start inside "Look at"


class ColdOpen(unittest.TestCase):                         # 7
    def test_cold_shot(self):
        w = W[:17] + [(18.0, 18.4, "Now"), (18.4, 18.8, "we"), (18.8, 19.3, "jump"), (19.9, 20.45, "boom"),
                      (20.5, 20.8, "Oh")]
        c = zabava.Cutter(w, 30.0)
        s, e = zabava.cold_shot(c, PEAK, 2.5)
        self.assertTrue(c.ok_start(s))
        self.assertIsNone(K.in_word(w, e))
        self.assertLessEqual(e - s, 2.5 + 1e-6)
        self.assertGreaterEqual(e, PEAK - 0.3)
        self.assertTrue(PEAK - 4.0 <= s <= PEAK - 1.9)


class Beat(unittest.TestCase):                             # 11
    def test_extends_to_a_safe_beat(self):
        c = zabava.Cutter(W, 30.0)
        keep = [(10.08, 23.5)]
        new = zabava.beat_fit(c, keep, 0.5, 0.1, 100.0, PEAK)
        L = sum(y - x for x, y in new)
        self.assertGreaterEqual(L, 23.5 - 10.08 - 1e-6)    # extended, not shortened
        self.assertAlmostEqual(((100.0 + L) - 0.1) / 0.5 % 1, 0.0, places=3)
        self.assertTrue(c.ok_end(new[-1][1]))

    def test_never_ends_inside_a_word(self):
        w = W + [(23.6, 25.0, "loooong."), (25.2, 26.0, "words.")]
        c = zabava.Cutter(w, 26.1)
        new = zabava.beat_fit(c, [(10.08, 23.45)], 0.5, 0.0, 0.0, PEAK)
        if new:
            self.assertTrue(c.ok_end(new[-1][1]))


class LeagueSubset(unittest.TestCase):                     # 8
    def test_only_moments_in_the_video(self):
        picked = [{"id": "a", "rank": 2}, {"id": "b"}, {"id": "c", "rank": 1}]
        info = {"moments": [{"id": "a"}, {"id": "c"}]}
        self.assertEqual([m["id"] for m in C.video_moments(picked, info)], ["c", "a"])


class Texts(unittest.TestCase):                            # 10, 21, 22
    def moments(self, n, long=False):
        nm = "Averyveryverylongstreamername" if long else "Streamer"
        return [{"id": f"m{i}", "rank": i + 1, "channel": f"{nm.lower()}{i}", "name": f"{nm}{i}", "reach": 100 - i,
                 "platform": "twitch", "url": "https://clips.twitch.tv/x"} for i in range(n)]

    def test_every_title_has_gta6_and_fits(self):
        for z in ("bananas", "fanpage"):
            for sk in C.SERIES:
                for long in (False, True):
                    t = C.titles_texts(z, sk if sk != "streamer" else "streamer:xqc", 12, date(2026, 11, 20),
                                       self.moments(12, long))
                    self.assertIn("GTA 6", t, sk)
                    self.assertLessEqual(len(t), 100, t)
                    self.assertNotIn("()", t)
                    self.assertTrue(t.count("(") == t.count(")"), t)

    def test_first_reactions_title(self):
        t = C.titles_texts("bananas", "first-reactions", 1, date(2026, 11, 20), self.moments(3))
        self.assertTrue(t.startswith("GTA 6: Streamers' First Reactions to Vice City #1 ("), t)

    def test_tags_limit_and_keywords(self):
        tags = C.yt_tags("bananas", "first-reactions", self.moments(30, True))
        self.assertLessEqual(C.tags_len(tags), 500)
        for k in ("gta 6", "gta vi", "grand theft auto 6", "vice city"):
            self.assertIn(k, tags)

    def test_description_limits_story_intro_and_chapters(self):
        ms = self.moments(40, True)
        ch = [(0, "#40 x"), (15, "#39 y #fyp https://x.y"), (30, "#38 fuck z")]
        d = C.description("fanpage", "first-reactions", date(2026, 11, 18), ms, ch)
        self.assertLessEqual(len(d), C.DESC_MAX)
        self.assertNotIn("counted down", d)
        self.assertIn("Grand Theft Auto VI", d)
        self.assertNotIn("#fyp", d)
        self.assertNotIn("http://x", d)
        self.assertNotIn("fuck", d)
        self.assertIn("\n0:00 #40 x", d)
        self.assertEqual(C.qa_texts("GTA 6 x", d, ["gta 6"], ch), [])

    def test_qa_texts_catches(self):
        self.assertTrue(C.qa_texts("Funniest Moments", "0:00 a", [], [(0, "a")]))
        self.assertTrue(C.qa_texts("GTA 6 " + "x" * 100, "\n0:00 a\n0:10 b\n0:20 c", [], [(0, "a"), (10, "b"), (20, "c")]))
        self.assertTrue(C.qa_texts("GTA 6", "x" * 5001, [], [(0, "a"), (10, "b"), (20, "c")]))

    def test_slot_on_publish_day(self):                    # 23
        t = C.suggested_slot("bananas", "first-reactions", date(2026, 11, 18), date(2026, 11, 20))
        self.assertEqual(t.date(), date(2026, 11, 20))
        self.assertNotIn(t.minute, (0, 30))


class Budget(unittest.TestCase):                           # 3
    def test_share(self):
        self.assertEqual(C.thumb_variants(6, 1), 2)
        self.assertEqual(C.thumb_variants(6, 0), 3)
        self.assertEqual(C.thumb_variants(1, 1), 1)
        self.assertEqual(C.thumb_variants(3, 1), 1)
        self.assertEqual(C.thumb_variants(0, 0), 0)


class Failures(unittest.TestCase):                         # 9, 18
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.old = C.OUT, C.USED, C.EPISODES, C.RESERVE, C.plan_day, C.build
        C.OUT = self.tmp / "out"
        C.USED, C.EPISODES, C.RESERVE = self.tmp / "u.json", self.tmp / "e.json", self.tmp / "r.json"

    def tearDown(self):
        C.OUT, C.USED, C.EPISODES, C.RESERVE, C.plan_day, C.build = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def args(self):
        return Namespace(znacka="bananas", serie=None, kalendar=0, zkouska=False, bez_overeni=True,
                         min_momentu_hlavni=3, min_momentu=25, bez_nahledu=True, out=None, hra=None, znovu=False)

    def test_second_run_same_night_skips(self):            # long.yml runs twice a night
        C.EPISODES.write_text(json.dumps({"_built": {"bananas": "2026-11-20"}}), encoding="utf-8")
        C.plan_day = lambda *a, **k: self.fail("planned although the day is built")
        C.make(self.args(), date(2026, 11, 19), date(2026, 11, 20))
        self.assertTrue((C.OUT / "bananas" / "2026-11-20.skip.json").exists())
        self.assertFalse(list((C.OUT / "bananas").glob("*.failed.json")))

    def test_failed_run_does_not_block_a_retry(self):
        C.plan_day = lambda *a, **k: {"day": "2026-11-20", "serie": None, "planned": "first-reactions", "note": "x"}
        C.make(self.args(), date(2026, 11, 19), date(2026, 11, 20))
        self.assertNotIn("_built", json.loads(C.EPISODES.read_text(encoding="utf-8")) if C.EPISODES.exists() else {})

    def test_rollback_frees_the_publish_day(self):
        used, eps, res = {}, {"_built": {"bananas": "2026-11-17"}}, {}
        plan = {"serie": "funny-day", "episode": 1, "day": "2026-11-20", "picked": [{"id": "x", "score": 1}], "rest": []}
        undo = C.commit_plan(Namespace(min_momentu=25), "bananas", plan, used, eps, res, date(2026, 11, 19))
        undo["built"] = eps["_built"]["bananas"]
        eps["_built"]["bananas"] = "2026-11-20"
        C.undo_plan(undo, used, eps, res)
        self.assertEqual(eps["_built"]["bananas"], "2026-11-17")

    def test_build_crash_writes_failed_note(self):
        rows = [{"id": f"m{i}", "channel": f"c{i}", "score": 1.0, "fun": 0.6, "seg": [0, 15], "play": 15}
                for i in range(25)]
        C.plan_day = lambda *a, **k: {"day": "2026-11-20", "serie": "first-reactions", "planned": "first-reactions",
                                      "note": "", "od": "2026-11-18", "do": "2026-11-18", "picked": rows, "rest": [],
                                      "episode": 1, "lead": None, "length": 400}

        def boom(*a, **k):
            raise KeyError("rank")
        C.build = boom
        C.make(self.args(), date(2026, 11, 19), date(2026, 11, 20))
        f = C.OUT / "bananas" / "2026-11-20_first-reactions.failed.json"
        self.assertTrue(f.exists())
        self.assertIn("crash: KeyError", json.loads(f.read_text(encoding="utf-8"))["duvod"])
        self.assertFalse(C.EPISODES.exists())              # no number taken

    def test_too_little_material_writes_failed_note(self):
        C.plan_day = lambda *a, **k: {"day": "2026-11-20", "serie": None, "planned": "first-reactions",
                                      "note": "Žádné video: málo materiálu"}
        C.make(self.args(), date(2026, 11, 19), date(2026, 11, 20))
        self.assertTrue((C.OUT / "bananas" / "2026-11-20_first-reactions.failed.json").exists())

    def test_calendar_day_off_is_a_skip_not_a_failure(self):
        C.plan_day = lambda *a, **k: {"day": "2026-11-21", "serie": None, "planned": None, "note": "není"}
        C.make(self.args(), date(2026, 11, 20), date(2026, 11, 21))
        self.assertFalse(list((C.OUT / "bananas").glob("*.failed.json")))
        self.assertTrue((C.OUT / "bananas" / "2026-11-21.skip.json").exists())

    def test_rollback_keeps_the_other_brand(self):
        a = Namespace(min_momentu=25)
        used, eps, res = {}, {}, {}
        plan = {"serie": "first-reactions", "episode": 1, "day": "2026-11-20",
                "picked": [{"id": "x", "score": 1}], "rest": [{"id": "r", "score": 0.5}]}
        undo = C.commit_plan(a, "bananas", plan, used, eps, res, date(2026, 11, 19))
        plan2 = {"serie": "first-reactions", "episode": 1, "day": "2026-11-20",
                 "picked": [{"id": "y", "score": 1}], "rest": []}
        C.commit_plan(a, "fanpage", plan2, used, eps, res, date(2026, 11, 19))
        undo = json.loads(json.dumps(undo))                # it travels through the meta.json
        C.undo_plan(undo, used, eps, res)
        self.assertNotIn("x", used)
        self.assertNotIn("r", used)
        self.assertIn("y", used)
        self.assertNotIn("first-reactions", eps.get("bananas", {}))
        self.assertEqual(eps["fanpage"]["first-reactions"], 1)


class Vision(unittest.TestCase):                           # 4
    def test_themed_without_vision_is_out(self):
        import nahled_ai
        import kategorie
        old = nahled_ai.ask_gameplay, kategorie.verify_frames
        nahled_ai.ask_gameplay = lambda *a, **k: (None, None)
        kategorie.verify_frames = lambda *a, **k: True
        try:
            r = {"id": "twitch_x", "source": "x.mp4", "peak": 10, "seg": [0, 12], "fun": 0.8,
                 "signals": {"static": 0.05, "dark": 0.0, "luma": 100}, "proven": ["first-reaction"],
                 "cats": {"first-reaction": 1.0}}
            ok, why = C.qualifies(dict(r), "first-reactions", "bananas", True, {})
            self.assertFalse(ok)
            self.assertIn("vision unavailable", why)
            ok, why = C.qualifies(dict(r), "funny-day", "bananas", True, {})
            self.assertTrue(ok)                            # main series: static filter only
        finally:
            nahled_ai.ask_gameplay, kategorie.verify_frames = old


if __name__ == "__main__":
    unittest.main()
