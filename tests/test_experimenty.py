"""
Gating logic of experimenty.py on synthetic data (no network, no Buffer, no repo state touched).

  .venv/Scripts/python -m unittest tests.test_experimenty -v

Multi-POV uses real (tiny, synthetic) videos made with ffmpeg's test sources: three streamers share
the same first seconds and the same NPC line (the anchor), then each run goes somewhere else; the
canned case is the identical video for two streamers (barber chair, shop menu, cutscene).
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.environ.setdefault("KLIPY_EXP", str(Path(tempfile.gettempdir()) / "exp_test_state.json"))

import experimenty as E                                    # noqa: E402

NOW = time.time()
GAME = "Grand Theft Auto VI"
ANCHOR = "pull over right now and step out of the car slowly"

def ctx(**kw):
    c = {"brand": "fanpage", "now": NOW, "good_score": 1000, "fun_min": E.FUN_MIN, "game_rx": "Grand Theft Auto VI",
         "blocked": set(), "other": "bananas", "other_published": set(), "published": set(),
         "prepare": lambda r: r, "visual": False}
    c.update(kw)
    return c

def row(i, ch, views=20000, age_h=2, **kw):
    r = {"id": f"twitch_{i}", "channel": ch, "views": views, "created": NOW - age_h * 3600, "category": GAME,
         "platform": "twitch", "fun": 0.7}
    r.update(kw)
    return r

def words(text, t0=1.0):
    return [(t0 + 0.3 * k, t0 + 0.3 * k + 0.25, w) for k, w in enumerate(text.split())]

class Videos:
    """7 s clips: the same 2 s start for everyone, then a different 5 s outcome per source."""
    # a still outcome, a fast one and a slow one: the runs must differ in picture AND motion
    OUTCOMES = ["smptehdbars=s=320x180:rate=30", "cellauto=s=320x180:rate=30:rule=110",
                "rgbtestsrc=s=320x180:rate=30,scroll=horizontal=0.04"]

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="exp_test_"))
        start = self.dir / "start.mp4"
        self._ff(["-f", "lavfi", "-i", "testsrc2=s=320x180:rate=30:d=2"], start)
        self.files = []
        for k, src in enumerate(self.OUTCOMES):
            end = self.dir / f"end{k}.mp4"
            self._ff(["-f", "lavfi", "-i", f"{src}", "-t", "5", "-vf", "hue=h=" + str(70 * k)], end)
            out = self.dir / f"clip{k}.mp4"
            self._ff(["-i", str(start), "-i", str(end), "-filter_complex",
                      "[0:v]format=yuv420p[a];[1:v]format=yuv420p,scale=320:180[b];[a][b]concat=n=2:v=1[v]",
                      "-map", "[v]"], out)
            self.files.append(out)

    @staticmethod
    def _ff(args, out):
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error"] + args + ["-pix_fmt", "yuv420p", str(out)],
                       check=True, timeout=120)

class MultiPov(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v = Videos()

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.v.dir, ignore_errors=True)

    def pov(self, i, ch, f, **kw):
        return row(i, ch, source=str(f), seg=[0.0, 7.0], words=words(ANCHOR), title="", **kw)

    def test_three_streamers_same_place_different_outcome(self):
        rows = [self.pov(k, ch, self.v.files[k]) for k, ch in enumerate(["xqc", "kaicenat", "jynxzi"])]
        plan, why = E.gate_multi_pov(rows, ctx())
        self.assertIsNotNone(plan, why)
        self.assertEqual({r["channel"] for r in plan["items"]}, {"xqc", "kaicenat", "jynxzi"})

    def test_same_video_twice_is_canned(self):
        rows = [self.pov(0, "xqc", self.v.files[0]), self.pov(1, "kaicenat", self.v.files[0]),
                self.pov(2, "jynxzi", self.v.files[2])]
        plan, why = E.gate_multi_pov(rows, ctx())
        self.assertIsNone(plan, "canned pair must not make a multi-POV short")

    def test_weak_reaction_or_low_virality_out(self):
        rows = [self.pov(0, "xqc", self.v.files[0]), self.pov(1, "kaicenat", self.v.files[1], fun=0.1),
                self.pov(2, "jynxzi", self.v.files[2], views=300)]
        plan, _ = E.gate_multi_pov(rows, ctx())
        self.assertIsNone(plan)

    def test_other_brand_clip_stays_out(self):
        rows = [self.pov(k, ch, self.v.files[k]) for k, ch in enumerate(["xqc", "kaicenat", "jynxzi"])]
        rows[0]["rendered"] = ["bananas"]
        plan, _ = E.gate_multi_pov(rows, ctx())
        self.assertIsNone(plan)

    def test_cheap_prefilter_needs_three_streamers(self):
        rows = [row(0, "xqc"), row(1, "xqc"), row(2, "kaicenat")]
        plan, why = E.gate_multi_pov(rows, ctx(prepare=None))
        self.assertIsNone(plan)
        self.assertIn("< 3", why)

class Story(unittest.TestCase):
    def test_four_good_moments_one_stream(self):
        rows = [row(k, "xqc", views=v, video_id="v1", vod_offset=off) for k, (v, off) in
                enumerate([(9000, 3000), (30000, 600), (15000, 1800), (12000, 5000), (500, 7000)])]
        plan, why = E.gate_story(rows, ctx())
        self.assertIsNotNone(plan, why)
        self.assertEqual([r["vod_offset"] for r in plan["items"]], [600, 1800, 5000])   # best 3, stream order

    def test_three_moments_not_enough(self):
        rows = [row(k, "xqc", video_id="v1", vod_offset=600 * k) for k in range(3)]
        self.assertIsNone(E.gate_story(rows, ctx())[0])

    def test_two_viewers_one_moment_count_once(self):
        rows = [row(k, "xqc", video_id="v1", vod_offset=off) for k, off in enumerate([100, 110, 900, 2000])]
        self.assertIsNone(E.gate_story(rows, ctx())[0])

    def test_different_streams_do_not_mix(self):
        rows = [row(k, "xqc", video_id=f"v{k % 2}", vod_offset=600 * k) for k in range(4)]
        self.assertIsNone(E.gate_story(rows, ctx())[0])

class First(unittest.TestCase):
    def test_found_by_three_earliest_first(self):
        rows = [row(1, "kaicenat", age_h=3, tags=["glitch"], title="the UFO over the desert"),
                row(2, "xqc", age_h=5, tags=["glitch"], title="UFO glitch??"),
                row(3, "jynxzi", age_h=1, tags=["glitch"], quote="yo there's a ufo up there"),
                row(4, "caseoh_", age_h=2, tags=["funny"], title="UFO lol")]
        plan, why = E.gate_first(rows, ctx(brand="bananas", other="fanpage"))
        self.assertIsNotNone(plan, why)
        self.assertEqual([r["channel"] for r in plan["items"]], ["xqc", "kaicenat", "jynxzi"])
        self.assertEqual(plan["labels"], ["FIRST", "+2h", "+4h"])

    def test_two_streamers_only(self):
        rows = [row(1, "kaicenat", tags=["glitch"], title="UFO"), row(2, "xqc", tags=["glitch"], title="UFO")]
        self.assertIsNone(E.gate_first(rows, ctx(brand="bananas"))[0])

    def test_generic_word_does_not_link(self):
        rows = [row(k, ch, tags=["glitch"], title=f"glitch {w}") for k, (ch, w) in
                enumerate([("xqc", "car"), ("kaicenat", "tree"), ("jynxzi", "bridge")])]
        self.assertIsNone(E.gate_first(rows, ctx(brand="bananas"))[0])

    def test_labels_minutes(self):
        ms = [{"created": 0}, {"created": 25 * 60}, {"created": 3 * 3600}]
        self.assertEqual(E.first_labels(ms), ["FIRST", "+25m", "+3h"])

class Tier3(unittest.TestCase):
    def test_three_unposted(self):
        rows = [row(k, ch, views=v) for k, (ch, v) in enumerate([("xqc", 600000), ("kaicenat", 40000), ("jynxzi", 6000)])]
        plan, why = E.gate_tier3(rows, ctx(brand="bananas"))
        self.assertIsNotNone(plan, why)
        self.assertEqual([r["channel"] for r in plan["items"]], ["jynxzi", "kaicenat", "xqc"])   # weakest first
        self.assertEqual(plan["tiers"], ["B", "A", "S"])

    def test_posted_ones_stay_out(self):
        rows = [row(k, ch) for k, ch in enumerate(["xqc", "kaicenat", "jynxzi"])]
        self.assertIsNone(E.gate_tier3(rows, ctx(brand="bananas", published={"twitch_0"}))[0])
        rows[1]["rendered"] = ["bananas"]
        self.assertIsNone(E.gate_tier3(rows, ctx(brand="bananas"))[0])

    def test_not_gta6_out(self):
        rows = [row(k, ch, category="Grand Theft Auto V") for k, ch in enumerate(["xqc", "kaicenat", "jynxzi"])]
        self.assertIsNone(E.gate_tier3(rows, ctx(brand="bananas"))[0])

class Measurement(unittest.TestCase):
    def posts(self, exp_views, n_exp=5, cls_views=100, n_cls=6):
        p = {}
        for k in range(n_cls):
            p[f"bananas:c{k}"] = {"brand": "bananas", "concept": "classic", "due_ts": NOW - 86400 * (k % 3), "views": cls_views}
        for k in range(n_exp):
            p[f"bananas:e{k}"] = {"brand": "bananas", "concept": "tier3", "due_ts": NOW - 3600 * k, "views": exp_views}
        return {"posts": p, "concepts": {}}

    def test_below_60_percent_turns_off(self):
        st = self.posts(50)
        ch = E.evaluate(st, NOW)
        self.assertEqual(ch[0][:4], ("bananas", "tier3", "on", "off"))
        self.assertEqual(E.concept_state("bananas", "tier3", st), "off")
        self.assertFalse(E.enabled("bananas", "tier3", {"bananas": {"exp_tier3": True}}, st))

    def test_above_classic_two_a_day(self):
        st = self.posts(150)
        E.evaluate(st, NOW)
        self.assertEqual(E.per_day("bananas", "tier3", {"bananas": {"exp_max_per_day": 1}}, st), 2)

    def test_needs_five_posts(self):
        st = self.posts(10, n_exp=4)
        self.assertEqual(E.evaluate(st, NOW), [])
        self.assertEqual(E.per_day("bananas", "tier3", {"bananas": {"exp_max_per_day": 1}}, st), 1)

    def test_off_is_sticky_until_reset(self):
        st = self.posts(50)
        E.evaluate(st, NOW)
        for p in st["posts"].values():
            p["views"] = 500
        E.evaluate(st, NOW)
        self.assertEqual(E.concept_state("bananas", "tier3", st), "off")

    def test_flag_off_wins(self):
        self.assertFalse(E.enabled("bananas", "tier3", {"bananas": {"exp_tier3": False}}, {"concepts": {}}))

    def test_post_views(self):
        v, e = E.post_views({"youtube": {"views": 552, "reactions": 10}, "facebook": {"impressions": 326, "reach": 295},
                             "twitter": {"impressions": 0}, "threads": {"views": 13, "reposts": 1}})
        self.assertEqual((v, e), (891, 11))

if __name__ == "__main__":
    unittest.main()
