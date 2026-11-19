"""
Long-form guards (18. 11.): the trigger "as soon as there is a stock of clips" (rotation, cap, retry wait,
target length, window since the series' last episode, NAHRAT HNED e-mail + slot), sentence-safe cuts
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


def prague(*a):
    return datetime(*a, tzinfo=C.TZ)


class Window(unittest.TestCase):                           # Aleš 18. 11.: since the series' last episode
    def a(self, **kw):
        return Namespace(od=None, do=None, hra=None, **kw)

    def test_game_out_timestamp(self):                     # 14
        self.assertEqual(datetime.fromtimestamp(C.GAME_OUT_TS, timezone.utc),
                         datetime(2026, 11, 18, 11, 0, tzinfo=timezone.utc))

    def test_first_reactions_window(self):
        od, do, t0, t1 = C.window_for("first-reactions", prague(2026, 11, 18, 20, 5), self.a())
        self.assertEqual(t0, C.GAME_OUT_TS)
        self.assertAlmostEqual(t1, prague(2026, 11, 18, 20, 5).timestamp())
        od, do, t0, t1 = C.window_for("first-reactions", prague(2026, 11, 23, 9, 0), self.a())
        self.assertEqual(t1 - t0, C.FIRST_HOURS * 3600)     # only the first hours after the unlock
        self.assertEqual(od, date(2026, 11, 18))

    def test_window_since_last_episode_of_the_series(self):
        now = prague(2026, 11, 21, 14, 25)
        od, do, t0, t1 = C.window_for("funny-day", now, self.a(), {}, "bananas")
        self.assertEqual(t0, C.GAME_OUT_TS)                 # no episode yet: since the unlock, not "yesterday"
        self.assertEqual(t1, now.timestamp())
        eps = {"_series_at": {"bananas": {"funny-day": "2026-11-20T08:25:00+01:00"}}}
        od, do, t0, t1 = C.window_for("funny-day", now, self.a(), eps, "bananas")
        self.assertEqual(t0, prague(2026, 11, 20, 8, 25).timestamp())
        od, do, t0, t1 = C.window_for("funny-day", now, self.a(), eps, "fanpage")
        self.assertEqual(t0, C.GAME_OUT_TS)                 # the other channel has its own window

    def test_window_lookback_capped(self):
        now = prague(2026, 12, 10, 12, 0)
        od, do, t0, t1 = C.window_for("after-dark", now, self.a(), {}, "fanpage")
        self.assertEqual(t1 - t0, C.LOOKBACK_DAYS * 86400)

    def test_window_clamped_to_unlock(self):
        od, do, t0, t1 = C.window_for("funny-day", prague(2026, 11, 18, 14, 0), self.a(), {}, "bananas")
        self.assertEqual(t0, C.GAME_OUT_TS)

    def test_test_category_not_clamped(self):
        a = Namespace(od="2026-11-16", do="2026-11-17", hra="Grand Theft Auto V")
        od, do, t0, t1 = C.window_for("funny-day", prague(2026, 11, 19, 1, 0), a)
        self.assertLess(t0, C.GAME_OUT_TS)
        od, do, t0, t1 = C.window_for("funny-day", prague(2026, 11, 17, 1, 0),
                                      Namespace(od=None, do=None, hra="Grand Theft Auto V"))
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

    def test_e_mail_subject_says_upload_now(self):       # Aleš 18. 11.
        import posli_mail
        m = {"znacka": "bananas", "kanal": "gta6bananaclips", "serie": "funny-day", "title": "GTA 6 Funniest Moments #3",
             "description": "x", "tags": ["gta 6"], "nahrat": "2026-11-21T18:47:00+01:00", "playlist": "funny-day",
             "duration_s": 540}
        subject, body, _ = posli_mail.texts_for(m)
        self.assertEqual(subject, "[YouTube gta6bananaclips] NAHRÁT HNED – GTA 6 Funniest Moments #3")
        self.assertIn("NAHRÁT HNED", body)
        self.assertIn("18:47", body)
        self.assertIn("KONCOVÁ OBRAZOVKA", body)
        self.assertIn("--- PROMPT ---", body)


class Slot(unittest.TestCase):                             # Aleš 18. 11.: the next good slot >= 45 min ahead
    def check(self, now, z="bananas"):
        t = C.asap_slot(z, "funny-day", now)
        self.assertGreaterEqual((t - now).total_seconds(), 45 * 60, (now, t))
        self.assertNotIn(t.minute, (0, 30))
        return t

    def test_daytime_goes_to_tonights_prime(self):
        for h in (10, 13, 17):
            t = self.check(prague(2026, 11, 21, h, 15))
            self.assertEqual(t.date(), date(2026, 11, 21))
            self.assertTrue(18 * 60 + 40 <= t.hour * 60 + t.minute <= 19 * 60 + 41, t)
            t = self.check(prague(2026, 11, 21, h, 15), "fanpage")
            self.assertTrue(19 * 60 + 40 <= t.hour * 60 + t.minute <= 20 * 60 + 41, t)

    def test_evening_and_night_right_away(self):
        for now in (prague(2026, 11, 21, 18, 10), prague(2026, 11, 21, 23, 0), prague(2026, 11, 22, 1, 52)):
            t = self.check(now)
            self.assertLessEqual((t - now).total_seconds(), 48 * 60, (now, t))

    def test_early_morning_au_slot(self):
        t = self.check(prague(2026, 11, 22, 4, 0))
        self.assertTrue(8 * 60 + 10 <= t.hour * 60 + t.minute <= 9 * 60 + 41, t)
        t = self.check(prague(2026, 11, 22, 7, 50))       # 08:36 is inside the AU slot already
        self.assertLessEqual((t - prague(2026, 11, 22, 7, 50)).total_seconds(), 48 * 60)

    def test_never_round(self):
        for m in range(0, 60):
            self.check(prague(2026, 11, 21, 22, m))
            self.check(prague(2026, 11, 22, 8, m))


class Budget(unittest.TestCase):                           # 3
    def test_share(self):
        self.assertEqual(C.thumb_variants(6, 1), 2)
        self.assertEqual(C.thumb_variants(6, 0), 3)
        self.assertEqual(C.thumb_variants(1, 1), 1)
        self.assertEqual(C.thumb_variants(3, 1), 1)
        self.assertEqual(C.thumb_variants(0, 0), 0)


CFG = {"long_min_gap_h": 20, "long_retry_h": 6, "long_target_min": 8,
       "long_calendar": {"rotation": ["funny-day", "streamer-week", "banana-league"], "first": "first-reactions"}}
NOW = prague(2026, 11, 21, 14, 25)


class Failures(unittest.TestCase):                         # 9, 18 + the trigger / cap (Aleš 18. 11.)
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.old = C.OUT, C.USED, C.EPISODES, C.RESERVE, C.VISION, C.plan_run, C.build, C.brand_cfg
        C.OUT = self.tmp / "out"
        C.USED, C.EPISODES, C.RESERVE = self.tmp / "u.json", self.tmp / "e.json", self.tmp / "r.json"
        C.VISION = self.tmp / "v.json"
        C.brand_cfg = lambda z, key, default: CFG.get(key, default)

    def tearDown(self):
        C.OUT, C.USED, C.EPISODES, C.RESERVE, C.VISION, C.plan_run, C.build, C.brand_cfg = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def args(self, **kw):
        a = dict(znacka="bananas", serie=None, kalendar=0, zkouska=False, bez_overeni=True, min_momentu_hlavni=3,
                 min_momentu=25, bez_nahledu=True, out=None, hra=None, znovu=False, od=None, do=None)
        a.update(kw)
        return Namespace(**a)

    def eps(self):
        return json.loads(C.EPISODES.read_text(encoding="utf-8")) if C.EPISODES.exists() else {}

    def test_cap_skips_within_20h(self):                   # two triggers / runs within the cap
        C.EPISODES.write_text(json.dumps({"_last": {"bananas": {"serie": "funny-day", "next": 1,
                                                                "at": "2026-11-20T19:00:00+01:00"}}}), encoding="utf-8")
        C.plan_run = lambda *a, **k: self.fail("planned although the cap is on")
        C.make(self.args(), NOW)
        self.assertTrue(list((C.OUT / "bananas").glob("*.skip.json")))
        self.assertFalse(list((C.OUT / "bananas").glob("*.failed.json")))

    def test_cap_over_after_20h(self):
        eps = {"_last": {"bananas": {"serie": "funny-day", "next": 1, "at": "2026-11-20T18:00:00+01:00"}}}
        self.assertIsNone(C.gate("bananas", eps, NOW))      # 20.4 h
        self.assertIn("cap", C.gate("bananas", eps, prague(2026, 11, 21, 13, 0)))
        self.assertIsNone(C.gate("fanpage", eps, NOW))      # per brand

    def test_retry_wait_after_failure(self):
        eps = {"_fail": {"bananas": "2026-11-21T10:00:00+01:00"}}
        self.assertIn("failed", C.gate("bananas", eps, NOW))
        self.assertIsNone(C.gate("bananas", eps, prague(2026, 11, 21, 16, 5)))

    def test_not_enough_material_is_a_silent_skip(self):
        C.plan_run = lambda *a, **k: {"day": "2026-11-21", "serie": None, "planned": "funny-day", "note": "málo"}
        C.make(self.args(), NOW)
        self.assertTrue(list((C.OUT / "bananas").glob("*.skip.json")))
        self.assertFalse(list((C.OUT / "bananas").glob("*.failed.json")))
        self.assertNotIn("_fail", self.eps())               # waiting for material is not a failure

    def test_manual_run_without_material_mails(self):
        C.plan_run = lambda *a, **k: {"day": "2026-11-21", "serie": None, "planned": "chases", "note": "málo"}
        C.make(self.args(serie="chases"), NOW)
        self.assertTrue(list((C.OUT / "bananas").glob("*_chases.failed.json")))

    def test_build_crash_writes_failed_note_and_retry_wait(self):
        rows = [{"id": f"m{i}", "channel": f"c{i}", "score": 1.0, "fun": 0.6, "seg": [0, 15], "play": 15}
                for i in range(25)]
        C.plan_run = lambda *a, **k: {"day": "2026-11-21", "serie": "first-reactions", "planned": "first-reactions",
                                      "note": "", "od": "2026-11-18", "do": "2026-11-18", "picked": rows, "rest": [],
                                      "episode": 1, "lead": None, "length": 500, "next": 0}

        def boom(*a, **k):
            raise KeyError("rank")
        C.build = boom
        C.make(self.args(), NOW)
        f = list((C.OUT / "bananas").glob("*_first-reactions.failed.json"))
        self.assertTrue(f)
        self.assertIn("crash: KeyError", json.loads(f[0].read_text(encoding="utf-8"))["duvod"])
        eps = self.eps()
        self.assertNotIn("bananas", eps)                    # no number taken
        self.assertNotIn("_last", eps)                      # no cap
        self.assertIn("bananas", eps["_fail"])              # the next try only after long_retry_h

    def test_commit_and_rollback_of_the_cap(self):
        used, res = {}, {}
        eps = {"_last": {"bananas": {"serie": "funny-day", "next": 1, "at": "2026-11-19T20:00:00+01:00"}}}
        plan = {"serie": "streamer-week", "episode": 1, "day": "2026-11-21", "picked": [{"id": "x", "score": 1}],
                "rest": [], "next": 2}
        undo = C.commit_plan(Namespace(min_momentu=25), "bananas", plan, used, eps, res, NOW.date(), NOW)
        self.assertEqual(eps["_last"]["bananas"]["next"], 2)
        self.assertEqual(eps["_series_at"]["bananas"]["streamer-week"], NOW.isoformat(timespec="seconds"))
        self.assertIn("cap", C.gate("bananas", eps, prague(2026, 11, 22, 1, 0)))
        undo = json.loads(json.dumps(undo))                # it travels through the meta.json
        C.undo_plan(undo, used, eps, res)
        self.assertEqual(eps["_last"]["bananas"]["at"], "2026-11-19T20:00:00+01:00")
        self.assertNotIn("streamer-week", eps["_series_at"]["bananas"])
        self.assertIsNone(C.gate("bananas", eps, NOW))

    def test_old_meta_rollback_still_works(self):          # metas from before 18. 11. (no cap fields)
        used, eps, res = {"x": {"znacka": "bananas", "serie": "funny-day", "datum": "2026-11-20"}}, {}, {}
        C.undo_plan({"z": "bananas", "s": "funny-day", "eps": None, "res": None, "used": {"x": None}, "promo": None,
                     "built": None}, used, eps, res)
        self.assertNotIn("x", used)

    def test_rollback_keeps_the_other_brand(self):
        a = Namespace(min_momentu=25)
        used, eps, res = {}, {}, {}
        plan = {"serie": "first-reactions", "episode": 1, "day": "2026-11-20",
                "picked": [{"id": "x", "score": 1}], "rest": [{"id": "r", "score": 0.5}]}
        undo = C.commit_plan(a, "bananas", plan, used, eps, res, date(2026, 11, 19), NOW)
        plan2 = {"serie": "first-reactions", "episode": 1, "day": "2026-11-20",
                 "picked": [{"id": "y", "score": 1}], "rest": []}
        C.commit_plan(a, "fanpage", plan2, used, eps, res, date(2026, 11, 19), NOW)
        undo = json.loads(json.dumps(undo))
        C.undo_plan(undo, used, eps, res)
        self.assertNotIn("x", used)
        self.assertNotIn("r", used)
        self.assertIn("y", used)
        self.assertNotIn("first-reactions", eps.get("bananas", {}))
        self.assertEqual(eps["fanpage"]["first-reactions"], 1)
        self.assertNotIn("bananas", eps["_last"])
        self.assertIn("fanpage", eps["_last"])


class Rotation(unittest.TestCase):                         # Aleš 18. 11.: the rotation, not weekdays
    def setUp(self):
        self.old = C.brand_cfg, C.candidates, C.pick_episode
        C.brand_cfg = lambda z, key, default: CFG.get(key, default)
        self.calls = []

    def tearDown(self):
        C.brand_cfg, C.candidates, C.pick_episode = self.old

    def a(self, **kw):
        base = {"serie": None, "hra": None, "znovu": False, "od": None, "do": None, "min_momentu": 25,
                "min_momentu_hlavni": 3}
        base.update(kw)
        return Namespace(**base)

    def rows(self, n, fun=0.6, play=15.0):
        return [{"id": f"m{i}", "channel": f"c{i % 9}", "score": 1.0 - i / 100, "fun": fun, "seg": [0, play],
                 "play": play} for i in range(n)]

    def fake(self, material):
        """material: {series: number of good moments}; the pool holds 2x that many candidates."""
        C.candidates = lambda a, s, t0, t1, used: self.rows(2 * material.get(s, 0))

        def pick(a, s, cands, z, verify=True, vis=None, reserve_ids=()):
            self.calls.append(s)
            return self.rows(material.get(s, 0)), [], {}, None
        C.pick_episode = pick

    def test_due_series(self):
        self.assertEqual(C.due("bananas", {}), ("funny-day", 0, "first-reactions"))
        eps = {"_last": {"bananas": {"serie": "first-reactions", "next": 0, "at": "x"}}}
        self.assertEqual(C.due("bananas", eps), ("funny-day", 0, None))
        eps["_last"]["bananas"]["next"] = 2
        self.assertEqual(C.due("bananas", eps)[:2], ("banana-league", 2))
        eps["_last"]["bananas"]["next"] = 3
        self.assertEqual(C.due("bananas", eps)[:2], ("funny-day", 0))

    def test_first_episode_is_first_reactions(self):
        self.fake({"first-reactions": 36, "funny-day": 36})
        plan = C.plan_run(self.a(), "bananas", NOW, {}, {}, {}, {}, False)
        self.assertEqual(plan["serie"], "first-reactions")
        self.assertEqual(plan["next"], 0)                   # the rotation did not move

    def test_first_reactions_thin_falls_to_rotation(self):
        self.fake({"first-reactions": 3, "funny-day": 36})
        plan = C.plan_run(self.a(), "bananas", NOW, {}, {}, {}, {}, False)
        self.assertEqual(plan["serie"], "funny-day")
        self.assertEqual(plan["next"], 1)

    def test_first_episode_waits_while_first_reactions_fill(self):
        self.fake({"first-reactions": 3, "funny-day": 36})
        plan = C.plan_run(self.a(), "bananas", prague(2026, 11, 19, 10, 0), {}, {}, {}, {}, False)
        self.assertIsNone(plan["serie"])                    # within the first 36 h: no other series instead
        self.assertEqual(self.calls, [])

    def test_due_series_built_moves_the_rotation(self):
        self.fake({"streamer-week": 36})
        eps = {"_last": {"bananas": {"serie": "funny-day", "next": 1, "at": "x"}}}
        plan = C.plan_run(self.a(), "bananas", NOW, {}, eps, {}, {}, False)
        self.assertEqual((plan["serie"], plan["next"]), ("streamer-week", 2))

    def test_thin_due_series_main_fallback_keeps_the_slot(self):
        self.fake({"streamer-week": 4, "funny-day": 36})
        eps = {"_last": {"bananas": {"serie": "funny-day", "next": 1, "at": "x"}}}
        plan = C.plan_run(self.a(), "bananas", NOW, {}, eps, {}, {}, False)
        self.assertEqual((plan["serie"], plan["next"]), ("funny-day", 1))

    def test_below_target_waits(self):                     # ~6.8 min: over the 5 min floor, under the 8 min target
        self.fake({"funny-day": 24})
        eps = {"_last": {"bananas": {"serie": "funny-day", "next": 0, "at": "x"}}}
        plan = C.plan_run(self.a(), "bananas", NOW, {}, eps, {}, {}, False)
        self.assertIsNone(plan["serie"])
        self.assertIn("8 min", plan["note"])
        plan = C.plan_run(self.a(serie="funny-day"), "bananas", NOW, {}, eps, {}, {}, False)
        self.assertEqual(plan["serie"], "funny-day")        # a manual run: the 5 min floor only

    def test_thin_pool_never_downloads(self):              # cheap pre-check: no pick (download / Whisper / vision)
        self.fake({"funny-day": 5})
        eps = {"_last": {"bananas": {"serie": "funny-day", "next": 0, "at": "x"}}}
        plan = C.plan_run(self.a(), "bananas", NOW, {}, eps, {}, {}, False)
        self.assertIsNone(plan["serie"])
        self.assertEqual(self.calls, [])

    def test_weak_number_one_waits(self):
        self.fake({"funny-day": 40})
        rows = self.rows(40, fun=0.3)
        C.pick_episode = lambda *a, **k: (rows, [], {}, None)
        eps = {"_last": {"bananas": {"serie": "funny-day", "next": 0, "at": "x"}}}
        self.assertIsNone(C.plan_run(self.a(), "bananas", NOW, {}, eps, {}, {}, False)["serie"])


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
                 "cats": {"first-reaction": 1.0, "funny": 0.6}}
            ok, why = C.qualifies(dict(r), "first-reactions", "bananas", True, {})
            self.assertFalse(ok)
            self.assertIn("vision unavailable", why)
            ok, why = C.qualifies(dict(r), "funny-day", "bananas", True, {})
            self.assertTrue(ok)                            # main series: static filter only
        finally:
            nahled_ai.ask_gameplay, kategorie.verify_frames = old


if __name__ == "__main__":
    unittest.main()
