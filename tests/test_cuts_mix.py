"""
Sentence-safe cuts and the speech-first mix (Aleš 18. 11.: "it's forbidden to cut in the middle of
a sentence", "on some streams the music is too loud"). Pure helpers only, no ffmpeg / Whisper.
  .venv/Scripts/python -m unittest tests.test_cuts_mix
"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import klipuj as K                                         # noqa: E402

# "I saw him. He ran (pause) and then" 1 s gap "we crashed the car! (2 s) Oh my god."
W = [(1.0, 1.2, "I"), (1.2, 1.5, "saw"), (1.5, 1.8, "him."), (1.85, 2.0, "He"), (2.0, 2.3, "ran"),
     (2.4, 2.6, "and"), (2.6, 2.9, "then"), (3.9, 4.2, "we"), (4.2, 4.6, "crashed"), (4.6, 4.8, "the"),
     (4.8, 5.2, "car!"), (7.2, 7.4, "Oh"), (7.4, 7.5, "my"), (7.5, 7.9, "god.")]


class Sentences(unittest.TestCase):
    def test_borders(self):
        self.assertTrue(K.sent_end(W, 2))           # him.
        self.assertFalse(K.sent_end(W, 4))          # ran -> 'and' 0.1 s later
        self.assertTrue(K.sent_end(W, 6))           # then + 1 s pause
        self.assertTrue(K.sent_start(W, 3))         # He (after a period)
        self.assertFalse(K.sent_start(W, 5))
        self.assertFalse(K._punct_end("so..."))

    def test_ok_points(self):
        self.assertTrue(K.ok_start(W, 0.5))
        self.assertFalse(K.ok_start(W, 2.35))       # between 'ran' and 'and'
        self.assertFalse(K.ok_start(W, 4.4))        # inside 'crashed'
        self.assertTrue(K.ok_end(W, 5.5))
        self.assertFalse(K.ok_end(W, 4.7))          # inside 'the'
        self.assertFalse(K.ok_end(W, 2.35))

    def test_snap_start_goes_back_to_sentence(self):
        a = K.snap_start(W, 2.5)                    # inside 'and', sentence 'He ran and then' at 1.85
        self.assertTrue(K.ok_start(W, a))
        self.assertLessEqual(a, 1.85)
        self.assertGreaterEqual(a, 1.8)             # never inside 'him.'

    def test_snap_end_extends_to_sentence_end(self):
        b = K.snap_end(W, 4.4, limit=20)            # inside 'crashed' -> after 'car!'
        self.assertTrue(K.ok_end(W, b))
        self.assertGreaterEqual(b, 5.2)
        self.assertLessEqual(b, 5.6)

    def test_snap_end_limit_falls_back_never_mid_word(self):
        b = K.snap_end(W, 4.4, limit=5.0, back=4.0)  # cannot reach 'car!' -> last sentence end before
        self.assertIsNone(K.in_word(W, b))
        self.assertTrue(K.ok_end(W, b))
        self.assertLessEqual(b, 4.4)

    def test_snap_cut_youtube(self):
        a, b = K.snap_cut(W, 2.3, 4.5, total=9.0, lo=2.0, hi=8.0)
        self.assertTrue(K.ok_start(W, a), a)
        self.assertTrue(K.ok_end(W, b), b)

    def test_trim_points_snaps(self):
        words = [(t, t + 0.3, "word") for t in [x * 0.4 for x in range(5, 120)]]   # one long sentence
        words[40] = (words[40][0], words[40][1], "done.")
        words[90] = (words[90][0], words[90][1], "end.")
        a, b = K.trim_points(words, peak=20.0, dur=60.0)
        self.assertIsNone(K.in_word(words, a))
        self.assertIsNone(K.in_word(words, b))     # no sentence end within +8 s: a pause, never mid-word
        words[60] = (words[60][0], words[60][1], "stop.")    # 26.0 s: reachable within +8 s
        a, b = K.trim_points(words, peak=20.0, dur=60.0)
        self.assertTrue(K.ok_end(words, b), b)
        self.assertGreaterEqual(b, 26.3)

    def test_word_edge(self):
        self.assertAlmostEqual(K.word_edge(W, 4.3), 4.18, places=2)
        self.assertAlmostEqual(K.word_edge(W, 4.55), 4.62, places=2)
        self.assertEqual(K.word_edge(W, 3.5), 3.5)


class CutCheck(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(K.cut_problems(W, 0.5, 5.5, [(0.5, 2.95), (3.7, 5.5)], (4.0 - 0.12, 5.3)), [])

    def test_finds_and_fixes(self):
        segs = [(1.6, 2.33), (2.38, 4.7)]           # start inside 'him.', jump inside a sentence, end inside 'the'
        probs = K.cut_problems(W, 1.6, 4.7, segs, (4.3, 7.45))
        self.assertEqual(len(probs), 5, probs)
        a, b, keep, hook, fixed, left = K.fix_cuts(W, 1.6, 4.7, segs, (4.3, 7.45), 20.0)
        self.assertEqual(left, [])
        self.assertEqual(len(fixed), 5)
        self.assertEqual(len(keep), 1)              # the jump inside the sentence is gone
        self.assertTrue(K.ok_start(W, a) and K.ok_end(W, b))
        self.assertIsNone(K.in_word(W, hook[0]))
        self.assertIsNone(K.in_word(W, hook[1]))

    def test_voice_stop(self):
        self.assertIsNone(K.voice_stop(W, 5.5, 6.5))        # pause: the sound may run on
        v = K.voice_stop(W, 2.95, 4.4)                      # 'we crashed' would be cut at 4.4
        self.assertIsNotNone(v)
        self.assertLess(v, 3.9)
        self.assertTrue(K.ok_end(W, v))


class Mix(unittest.TestCase):
    def test_talk_spans(self):
        sp = K.talk_spans(W, 20)
        self.assertEqual(len(sp), 3)                         # 'I saw ... then', 'we crashed the car!', 'Oh my god.'
        self.assertLess(sp[0][0], 1.0)
        self.assertGreater(sp[0][1], 2.9)

    def test_audio_mix_graph(self):
        f = K.audio_mix(True, 10.0, music_idx=1, voice_gain=6.0, music_env="0.1+(-0.09)*clip(t\\,0\\,1)", voice_end=8.0)
        self.assertIn("volume=6.00dB", f)
        self.assertIn("sidechaincompress", f)
        self.assertIn("[a0k]", f)
        self.assertIn("afade=t=out:st=7.900", f)
        st = K.audio_mix(True, 10.0, music_idx=1, voice_gain=6.0, music_env="0.1", stems=True)
        self.assertIn("[vo]", st)
        self.assertNotIn("amix", st)


if __name__ == "__main__":
    unittest.main()
