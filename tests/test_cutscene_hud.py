"""
"No HUD = cutscene" (18. 11., GTA 6 launch): the minimap detector on labelled real frames and the
whole cutscene decision on short real clips (640x360, 4 fps, gray, first 30 s of each):
  cutscene_tyrone        - the live short that went out (story cutscene, no letterbox): must be a cutscene
  cutscene_lacy          - the same cutscene on Lacy's stream, gameplay with the minimap at the end
  gameplay_hud_xqc       - xQc chased by the police, minimap all the time: must pass
  gameplay_nohud_shower  - Lacy in the shower (no HUD, static camera): must pass
  gameplay_nohud_robbery - Lacy's bank robbery (no HUD, scripted takedown with cuts): must pass
Frames in tests/data/hud: hud_* show the minimap, nohud_* do not (labelled by eye).
  .venv/Scripts/python -m unittest tests.test_cutscene_hud
"""
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import klipuj as K                                         # noqa: E402

DATA = Path(__file__).resolve().parent / "data"
K.log = lambda *a, **k: None


def frame(p):
    return cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)


class HudFrames(unittest.TestCase):
    def test_labelled_frames(self):
        files = sorted((DATA / "hud").glob("*.jpg"))
        self.assertGreaterEqual(len(files), 10)
        gs = np.array([frame(p) for p in files])
        score = K.hud_rim(gs)
        for p, s in zip(files, score):
            with self.subTest(frame=p.name, score=round(float(s), 2)):
                self.assertEqual(s > K.HUD_RIM, p.name.startswith("hud_"))

    def test_no_minimap_on_flat_or_noisy_picture(self):
        rng = np.random.default_rng(1)
        flat = np.full((2, 360, 640), 90, np.uint8)
        noise = rng.integers(0, 255, (2, 360, 640), dtype=np.uint8)
        self.assertTrue((K.hud_rim(flat) < K.HUD_RIM).all())
        self.assertTrue((K.hud_rim(noise) < K.HUD_RIM).all())

    def test_synthetic_rim(self):                          # a drawn rounded rectangle at HUD_BOX is found
        g = np.full((1, 360, 640), 60, np.uint8)
        x0, y0, x1, y1 = [int(round(v * d)) for v, d in zip(K.HUD_BOX, (640, 360, 640, 360))]
        cv2.rectangle(g[0], (x0, y0), (x1, y1), 200, 1)
        self.assertGreater(K.hud_rim(g)[0], K.HUD_RIM)

    def test_covered_by_static_overlay(self):
        rng = np.random.default_rng(2)
        g = rng.integers(0, 255, (10, 360, 640), dtype=np.uint8)     # the game moves everywhere ...
        g[:, 280:360, 0:120] = 128                                     # ... except a still overlay box
        self.assertTrue(K.hud_covered(g))
        self.assertFalse(K.hud_covered(rng.integers(0, 255, (10, 360, 640), dtype=np.uint8)))


class Cuts(unittest.TestCase):
    def test_hard_cut_vs_motion(self):
        a = np.full((90, 160), 40, np.uint8)
        b = np.full((90, 160), 200, np.uint8)
        seq = np.array([a] * 6 + [b] * 6)                  # one cut after 1.5 s at 4 fps
        self.assertEqual(K.shot_cuts(seq, 4), [1.5])
        ramp = np.array([np.full((90, 160), 40 + 12 * i, np.uint8) for i in range(12)])   # a slow pan / fade
        self.assertEqual(K.shot_cuts(ramp, 4), [])

    def test_flash_is_no_cut(self):                       # a muzzle flash / explosion goes there and back
        a = np.full((90, 160), 40, np.uint8)
        w = np.full((90, 160), 250, np.uint8)
        self.assertEqual(K.shot_cuts(np.array([a] * 6 + [w] + [a] * 6), 4), [])

    def test_failed_decode_is_neutral(self):
        res = K.subprocess.CompletedProcess([], 1, stdout=bytes(640 * 360 * 20), stderr=b"error")
        with mock.patch.object(K.subprocess, "run", return_value=res), \
                mock.patch.object(K, "src_norm", return_value="null"):
            self.assertEqual(K.hud_cutscene("x.mp4")[0], 0.0)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg missing")
class RealClips(unittest.TestCase):
    def check(self, name):
        return K.cutscene_check(DATA / "cutscene" / name, {"platform": "twitch"})

    def test_tyrone_live_is_cutscene(self):              # regression: the published live short
        share, why = self.check("cutscene_tyrone.mp4")
        self.assertGreater(share, K.CUTSCENE_MAX, why)
        self.assertEqual(why["by"], "no HUD + cinematic cuts")

    def test_lacy_same_cutscene(self):
        share, why = self.check("cutscene_lacy.mp4")
        self.assertGreater(share, K.CUTSCENE_MAX, why)

    def test_gameplay_passes(self):
        for name in ("gameplay_hud_xqc.mp4", "gameplay_nohud_shower.mp4", "gameplay_nohud_robbery.mp4"):
            with self.subTest(clip=name):
                share, why = self.check(name)
                self.assertLessEqual(share, K.CUTSCENE_MAX, why)

    def test_youtube_only_letterbox(self):                 # trailer reactions: the HUD rule is off
        share, why = K.cutscene_check(DATA / "cutscene" / "cutscene_tyrone.mp4", {"platform": "youtube"})
        self.assertEqual(share, 0.0)
        self.assertNotIn("no_hud", why)


if __name__ == "__main__":
    unittest.main()
