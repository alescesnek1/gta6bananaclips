"""
A viewer's Twitch clip that ends too soon after the payoff is extended from the VOD (18. 11.):
when it is needed, the VOD windows, the payoff's shift into the extended source, the audio match,
and the fallback to the clip as it is.
  .venv/Scripts/python -m unittest tests.test_vod_extend
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import klipuj as K                                         # noqa: E402

# "Go go go. (payoff ~6 s) Oh my god. No way."
W = [(1.0, 1.2, "Go"), (1.2, 1.4, "go"), (1.4, 1.7, "go."), (6.0, 6.2, "Oh"), (6.2, 6.3, "my"),
     (6.3, 6.7, "god."), (9.2, 9.4, "No"), (9.4, 9.8, "way.")]
PK = 29.2                                                  # payoff 0.8 s before the clip ends (30 s)


class NeedsVod(unittest.TestCase):
    def test_no_payoff(self):
        self.assertFalse(K.needs_vod(W, None, 10.0))

    def test_too_little_after_the_payoff(self):
        self.assertTrue(K.needs_vod(W, 9.0, 10.0))                 # 1.0 s left

    def test_sentence_end_after_floor(self):
        self.assertFalse(K.needs_vod(W, 5.9, 12.0))                # "way." ends at 9.8 >= 8.9

    def test_speech_runs_to_the_end(self):
        # the last sentence is cut by the source's end: no sentence end after payoff + 3 s
        ws = W[:3] + [(6.0, 6.2, "Oh"), (6.2, 6.3, "my"), (6.3, 6.7, "god"), (6.7, 7.2, "what"), (7.2, 7.6, "is"),
                      (7.6, 8.2, "he"), (8.2, 8.8, "doing"), (8.8, 9.3, "and"), (9.3, 9.98, "then")]
        self.assertTrue(K.needs_vod(K.cut_words(ws, 10.0), 5.9, 10.0))

    def test_pause_after_payoff_is_a_sentence_end(self):
        ws = W[:6] + [(9.0, 9.3, "and"), (9.3, 9.98, "then")]       # 2.3 s pause after "god."
        self.assertFalse(K.needs_vod(K.cut_words(ws, 11.0), 5.9, 11.0))


class Windows(unittest.TestCase):
    def test_probe_holds_both_readings(self):
        s, e = K.vod_probe_window(1000.0, 30.0)
        self.assertLessEqual(s, 1000.0 - 30.0 - 3)                 # vod_offset = the clip's end
        self.assertGreaterEqual(e, 1000.0 + 30.0 + 3)              # vod_offset = the clip's start

    def test_probe_never_negative(self):
        self.assertEqual(K.vod_probe_window(10.0, 30.0)[0], 0.0)

    def test_short_clip(self):
        s, e, s0 = K.vod_window(500.0, 30.0, 28.0)
        self.assertEqual(s0, 4.0)                                  # 24 s before the payoff are kept
        self.assertAlmostEqual(s, 500.0 + 4.0 - 1.0)
        self.assertAlmostEqual(e, 500.0 + 30.0 + K.VOD_POST)
        self.assertLessEqual(e - s, K.VOD_MAX)

    def test_early_payoff_starts_at_clip_start(self):
        s, e, s0 = K.vod_window(500.0, 20.0, 18.5)
        self.assertEqual(s0, 0.0)
        self.assertAlmostEqual(s, 499.0)

    def test_long_clip_capped(self):
        s, e, s0 = K.vod_window(500.0, 60.0, 20.0)                 # payoff early, 40 s of speech after it
        self.assertLessEqual(e - s, K.VOD_MAX + 1e-6)
        self.assertLess(s0, 20.0)                                  # never past the payoff
        self.assertAlmostEqual(s, 500.0 + s0 - 1.0)


class PeakShift(unittest.TestCase):
    def test_cut_at_lag(self):
        # clip time s0 = 4 sits 1.3 s into the section, the extended source starts there
        self.assertAlmostEqual(K.vod_map(28.0, 4.0, 1.3, 1.3), 24.0)

    def test_whole_clip(self):
        self.assertAlmostEqual(K.vod_map(27.15, 0.0, 0.92, 0.92), 27.15)

    def test_cut_earlier_than_lag(self):
        self.assertAlmostEqual(K.vod_map(10.0, 0.0, 2.0, 0.5), 11.5)


class AudioLag(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(7)

    def test_finds_the_snippet(self):
        sr = K.VOD_SR
        b = self.rng.normal(0, 3000, sr * 20)
        a = 0.6 * b[int(7.25 * sr): int(15.25 * sr)] + self.rng.normal(0, 200, sr * 8)   # re-encoded, quieter
        lag, ncc = K.audio_lag(a, b)
        self.assertAlmostEqual(lag, 7.25, places=3)
        self.assertGreater(ncc, 0.9)

    def test_other_audio_does_not_match(self):
        sr = K.VOD_SR
        lag, ncc = K.audio_lag(self.rng.normal(0, 3000, sr * 5), self.rng.normal(0, 3000, sr * 20))
        self.assertLess(ncc, K.VOD_NCC)

    def test_section_shorter_than_clip(self):
        self.assertEqual(K.audio_lag(np.ones(K.VOD_SR * 5), np.ones(K.VOD_SR * 3)), (None, 0.0))


def sentences(starts, length=1.6):
    """Short sentences ('one two three.') starting at the given times, 0.5 s+ pauses between them."""
    ws = []
    for s in starts:
        ws += [(s, s + 0.4, "one"), (s + 0.4, s + 0.9, "two"), (s + 0.9, s + length, "three.")]
    return ws


class ShortLength(unittest.TestCase):                      # competitor analysis 18. 11.: 12-20 s, cap 25
    def test_setup_near_8_s(self):
        ws = sentences([1, 4, 7, 10, 13, 16, 19, 22, 25])  # payoff at 22, end 27
        t = K.short_start(ws, 22.0, 0.0, 27.0)
        self.assertTrue(K.ok_start(ws, t))
        self.assertTrue(K.SETUP_MIN <= 22.0 - t <= K.SETUP_MAX, t)
        self.assertTrue(K.SHORT_MIN <= 27.0 - t <= K.SHORT_MAX, 27.0 - t)

    def test_never_before_the_trim_start(self):
        ws = sentences([1, 4, 7, 10])
        self.assertEqual(K.short_start(ws, 9.0, 3.5, 13.0), 3.5)

    def test_capped_without_a_story(self):
        # sentences only far before the payoff and right before it (setup window empty), long clip
        ws = sentences([1, 5, 9]) + [(20.0, 20.4, "wait."), (24.5, 25.0, "what"), (25.0, 26.0, "is"), (26.0, 30.0, "THAT.")]
        t = K.short_start(ws, 30.5, 0.0, 34.0)
        self.assertLessEqual(34.0 - t, K.SHORT_CAP)

    def test_story_may_run_longer(self):
        # one stream of speech (no pause, no punctuation) from 6 s into the payoff at 30 s
        ws = sentences([1, 3.4]) + [(6.0 + 0.5 * k, 6.5 + 0.5 * k, "and") for k in range(48)]
        self.assertTrue(K.story_run(ws, 30.0))
        t = K.short_start(ws, 30.0, 0.0, 33.5)
        self.assertGreater(33.5 - t, K.SHORT_CAP)
        self.assertLessEqual(33.5 - t, K.STORY_CAP)
        self.assertTrue(K.ok_start(ws, t))

    def test_story_longer_than_the_cap_keeps_the_sentence(self):
        ws = sentences([1]) + [(3.0 + 0.5 * k, 3.5 + 0.5 * k, "and") for k in range(54)]
        t = K.short_start(ws, 30.0, 0.0, 33.5)
        self.assertTrue(K.ok_start(ws, t), t)                      # never mid-sentence, even over the cap

    def test_no_payoff_cap_only(self):
        ws = sentences(range(1, 40, 3))
        t = K.short_start(ws, None, 0.0, 40.0)
        self.assertLessEqual(40.0 - t, K.SHORT_CAP)
        self.assertTrue(K.ok_start(ws, t))


class Extend(unittest.TestCase):
    """extend_from_vod with the network, ffmpeg and Whisper mocked."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.src = self.d / "source.mp4"
        self.src.write_bytes(b"clip")
        self.clip = {"id": "twitch_x", "url": "https://www.twitch.tv/x/clip/Abc", "platform": "twitch",
                     "video_id": "123", "vod_offset": 1000}
        self.words = K.Words(W[:6])

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _run(self, lags, fail_dl=False, durs=(30.0, 37.0)):
        def run(cmd, timeout=None):
            if fail_dl:
                raise RuntimeError("yt-dlp failed (1): ERROR: This video is only available for subscribers")
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"vod")

        def encode(cmd, out):
            Path(out).write_bytes(b"extended")

        dur = iter(durs)
        with mock.patch.object(K, "audio_peak", return_value=PK), \
                mock.patch.object(K, "duration", side_effect=lambda p: next(dur)), \
                mock.patch.object(K, "run", side_effect=run), \
                mock.patch.object(K, "pcm", return_value=np.zeros(10)), \
                mock.patch.object(K, "audio_lag", side_effect=lags), \
                mock.patch.object(K, "encode", side_effect=encode), \
                mock.patch.object(K, "transcribe", return_value=K.Words(W)), \
                mock.patch.object(K, "log"):
            return K.extend_from_vod(self.clip, self.d, self.src, self.words)

    def test_extended(self):
        # probe: the clip starts 30.9 s into the audio window (vod_offset was its end); section: 0.92 s in
        src, words, peak = self._run([(30.9, 0.99), (0.92, 0.98)])
        self.assertEqual(src, self.src)
        self.assertEqual(src.read_bytes(), b"extended")
        self.assertEqual((self.d / "source_clip.mp4").read_bytes(), b"clip")
        self.assertAlmostEqual(peak, 24.0)                         # s0 = payoff - 24
        self.assertEqual(len(words), len(W))
        vod = K.load_json(self.d / "vod.json", {})
        self.assertAlmostEqual(vod["clip_start"], 1000 - 30 - K.VOD_PRE + 30.9, places=2)
        self.assertFalse((self.d / "vod_raw.mp4").exists() or (self.d / "vod_audio.mp4").exists())

    def test_not_needed(self):
        with mock.patch.object(K, "needs_vod", return_value=False):
            src, words, peak = self._run([])
        self.assertEqual((src, peak), (self.src, PK))
        self.assertIs(words, self.words)

    def test_no_vod(self):
        self.clip.update(video_id=None, vod_offset=None, url="https://example.com/x")
        src, words, peak = self._run([])
        self.assertEqual((src.read_bytes(), peak), (b"clip", PK))

    def test_download_fails(self):
        src, words, peak = self._run([], fail_dl=True)
        self.assertEqual((src.read_bytes(), peak), (b"clip", PK))
        self.assertIs(words, self.words)
        self.assertFalse((self.d / "vod.json").exists())

    def test_audio_does_not_match(self):
        src, words, peak = self._run([(12.0, 0.08)])
        self.assertEqual((src.read_bytes(), peak), (b"clip", PK))
        self.assertFalse((self.d / "source_vod.mp4").exists())

    def test_section_adds_nothing(self):
        src, words, peak = self._run([(30.9, 0.99), (0.92, 0.98)], durs=(30.0, 25.0))
        self.assertEqual((src.read_bytes(), peak), (b"clip", PK))
        self.assertFalse((self.d / "source_vod.mp4").exists())


class Loop(unittest.TestCase):                             # owner 18. 11.: end once the payoff is resolved
    def test_reaction_end(self):
        ts = np.arange(0, 12, 0.1) + 0.25
        rms = np.where((ts > 6.0) & (ts < 7.6), 1.0, 0.1)          # a scream from 6 to 7.6 s
        self.assertAlmostEqual(K.reaction_end(ts, rms, 6.5), 7.65, places=1)

    def test_reaction_end_capped(self):
        ts = np.arange(0, 20, 0.1) + 0.25
        self.assertAlmostEqual(K.reaction_end(ts, np.ones_like(ts), 6.0), 10.0, places=1)
        self.assertIsNone(K.reaction_end(ts, np.zeros(0), 6.0))

    def test_end_soon_after_the_payoff(self):
        # "... Oh my god." finishes at 6.7, the payoff at 5.9: the clip ends ~1 s after it, not 3+ s
        a, b = K.trim_points(W, 5.9, 12.0, lo=5.0)
        self.assertTrue(K.ok_end(W, b), b)
        self.assertGreaterEqual(b - 5.9, K.AFTER_PEAK - 0.01)
        self.assertLess(b, 9.2)                                    # not into "No way."

    def test_end_waits_for_the_reaction(self):
        a, b = K.trim_points(W, 5.9, 12.0, lo=5.0, resolve=8.9)
        self.assertGreaterEqual(b, 8.9)
        self.assertTrue(K.ok_end(W, b), b)


class SeamlessLoop(unittest.TestCase):                     # owner 18. 11.: no teaser, the end flows into the start
    def test_crop_eases_back_to_the_first_frame(self):
        xs = [100] * 10 + [400] * 20
        out = K.loop_ease(xs, 25)
        self.assertEqual(out[24], 100)                             # last frame = first frame's framing
        self.assertTrue(all(out[i] >= out[i + 1] for i in range(19, 24)))   # eased, monotone
        self.assertEqual(out[18], 400)                             # untouched before the ease
        self.assertEqual(out[25:], [100] * 5)                      # held after the end

    def test_crop_short_list(self):
        self.assertEqual(K.loop_ease([5, 6, 7], 3), [5, 6, 7])

    def test_seam_gain_moves_to_the_start_level(self):
        head, tail = np.full(100, 0.1), np.full(100, 0.4)
        g = K.seam_gain(head, tail, 100)
        self.assertAlmostEqual(g[0], 1.0)
        self.assertAlmostEqual(g[-1], 0.25)
        self.assertTrue(np.all(np.diff(g) <= 1e-12))

    def test_seam_gain_never_pushes_the_peak(self):
        g = K.seam_gain(np.full(100, 0.5), np.full(100, 0.7), 100)
        self.assertLessEqual(g[-1] * 0.7, 0.8 + 1e-9)
        g = K.seam_gain(np.full(100, 0.9), np.full(100, 0.7), 100)
        self.assertLessEqual(g[-1] * 0.7, 0.8 + 1e-9)

    def test_seam_jump(self):
        sr = 1000
        a = np.concatenate([np.full(sr, 0.1), np.full(sr, 0.4)])
        self.assertAlmostEqual(K.seam_jump(a, sr), 20 * np.log10(4), places=3)
        self.assertEqual(K.seam_jump(np.zeros(2 * sr), sr), 0.0)

    def test_strong_start_wins(self):
        ws = sentences([1, 4, 7, 10, 13, 16, 19, 22, 25])          # payoff 22: starts at 12.85 and 15.85 qualify
        t = K.short_start(ws, 22.0, 0.0, 27.0, score=lambda x: 5.0 if abs(x - 12.85) < 0.2 else 1.0)
        self.assertAlmostEqual(t, 12.85, places=2)
        self.assertTrue(K.ok_start(ws, t))

    def test_open_score(self):
        ts = np.arange(0, 10, 0.1) + 0.25
        rms = np.where(ts > 5, 1.0, 0.1)
        self.assertGreater(K.open_score(ts, rms, 5.0), K.open_score(ts, rms, 2.0))
        self.assertEqual(K.open_score(ts, np.zeros(0), 2.0), 0.0)

    def test_no_teaser(self):
        self.assertTrue(K.LOOP)


class FanEnding(unittest.TestCase):
    def test_deterministic_split(self):
        b = {"outro": "x.mp4", "outro_ab": 0.5}
        picks = [K.fan_ending(f"twitch_{i}", b) for i in range(400)]
        self.assertEqual(picks, [K.fan_ending(f"twitch_{i}", b) for i in range(400)])
        self.assertTrue(150 < picks.count("outro") < 250, picks.count("outro"))

    def test_shares(self):
        self.assertEqual(K.fan_ending("a", {"outro": "x.mp4", "outro_ab": 0.0}), "short")
        self.assertEqual(K.fan_ending("a", {"outro": "x.mp4", "outro_ab": 1.0}), "outro")
        self.assertEqual(K.fan_ending("a", {}), "short")

    def test_forced(self):
        with mock.patch.dict(K.os.environ, {"KLIPY_ENDING": "outro"}):
            self.assertEqual(K.fan_ending("a", {"outro": "x.mp4", "outro_ab": 0.0}), "outro")


class NonGame(unittest.TestCase):                          # owner 18. 11.: viral GTA 6 reactions without gameplay
    CFG = {"good_score": 1000, "star_min_score": 300, "star_streamers": ["jynxzi", "lirik"]}

    def clip(self, **kw):
        c = {"channel": "someone", "views": 60000, "created": K.time.time() - 3600, "title": "",
             "category": "Just Chatting"}
        c.update(kw)
        return c

    def test_viral_gta6_reaction_passes(self):
        self.assertIsNone(K.non_game_why(self.clip(title="Jynxzi finds out GTA VI is 200GB"), "", self.CFG))

    def test_category_counts(self):
        self.assertIsNone(K.non_game_why(self.clip(category="Grand Theft Auto VI"), "so anyway", self.CFG))

    def test_transcript_counts(self):
        self.assertIsNone(K.non_game_why(self.clip(), "bro vice city looks insane", self.CFG))

    def test_not_about_gta6(self):
        # DarkViperAU talking about GT5: GTA V category, no GTA 6 in title or speech
        why = K.non_game_why(self.clip(category="Grand Theft Auto V", title="GT5 was the best"), "gran turismo five", self.CFG)
        self.assertEqual(why, "not about GTA 6")

    def test_other_game(self):
        self.assertTrue(K.non_game_why(self.clip(category="Fortnite", title="GTA 6 talk"), "gta 6", self.CFG)
                        .startswith("another game"))

    def test_not_viral(self):
        why = K.non_game_why(self.clip(views=300, title="GTA 6"), "gta 6", self.CFG)
        self.assertTrue(why.startswith("not viral"))

    def test_star_needs_less(self):
        c = self.clip(channel="lirik", views=2000, created=K.time.time() - 24 * 3600, title="More GTA thoughts",
                      category="Grand Theft Auto VI")
        self.assertIsNone(K.non_game_why(c, "", self.CFG))
        self.assertIsNotNone(K.non_game_why({**c, "channel": "nobody"}, "", self.CFG))

    def test_before_whisper_only_cheap_checks(self):
        self.assertIsNone(K.non_game_why(self.clip(), None, self.CFG))


if __name__ == "__main__":
    unittest.main()
