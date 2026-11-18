"""
Review fixes of the short render path (18. 11., launch day): true peak after the AAC encode, >= 3 s
after the climax, the end never snapped before the payoff, bleeps under the score card, viewer clips
starting / ending mid-sentence, one moment from two viewers' clips, hook <= 2.5 s, punctuation,
exclude_titles, swear list, corrupt state files.
  .venv/Scripts/python -m unittest tests.test_review_fixes
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import klipuj as K                                         # noqa: E402

# "So we go in. (pause) He shot me! (2 s) Oh my god. What was that."   payoff (scream) at ~6.0 s
W = [(1.0, 1.2, "So"), (1.2, 1.4, "we"), (1.4, 1.6, "go"), (1.6, 1.9, "in."), (3.0, 3.2, "He"),
     (3.2, 3.5, "shot"), (3.5, 3.8, "me!"), (6.0, 6.2, "Oh"), (6.2, 6.3, "my"), (6.3, 6.7, "god."),
     (7.4, 7.6, "What"), (7.6, 7.7, "was"), (7.7, 8.0, "that"), (8.0, 8.3, "thing.")]


class Punct(unittest.TestCase):                            # 15
    def test_bare_quote_is_no_end(self):
        self.assertFalse(K._punct_end('"'))
        self.assertFalse(K._punct_end("')"))
        self.assertFalse(K._punct_end(""))
        self.assertTrue(K._punct_end('it."'))
        self.assertFalse(K._punct_end("so..."))


class Dangling(unittest.TestCase):                         # coordinator 18. 11.: "... you got it. I" + pause
    WS = [(1.0, 1.2, "you"), (1.2, 1.4, "got"), (1.4, 1.7, "it."), (1.8, 1.9, "I"), (2.6, 2.9, "think"),
          (2.9, 3.3, "so"), (4.2, 4.5, "Wow"), (5.4, 5.6, "and"), (6.4, 6.8, "boom")]

    def test_pause_after_function_word_is_no_end(self):
        ws = self.WS
        self.assertTrue(K.sent_end(ws, 2))            # it.
        self.assertFalse(K.sent_end(ws, 3))           # I + 0.7 s pause
        self.assertFalse(K.sent_end(ws, 5))           # 'so' + 0.9 s pause: dangling too
        self.assertTrue(K.sent_end(ws, 6))            # Wow + pause: a real end
        self.assertFalse(K.sent_end(ws, 7))           # and + pause
        self.assertFalse(K.sent_start(ws, 4))         # 'think' after 'I ...' continues the sentence
        self.assertFalse(K.ok_end(ws, 2.2))
        self.assertTrue(K._dangling("I'm,") and K._dangling("It’s") and not K._dangling("car"))

    def test_snaps_back_to_the_real_end(self):
        b = K.snap_end(self.WS, 2.2, limit=2.5, back=2.0)    # cannot go forward past 2.5: back to 'it.'
        self.assertGreaterEqual(b, 1.7)
        self.assertLessEqual(b, 1.8)
        self.assertTrue(K.ok_end(self.WS, b))

    def test_last_word_dangling_is_open(self):
        ws = [(1.0, 1.3, "Look."), (1.6, 1.8, "you")]
        self.assertFalse(K.sent_end(ws, 1))

    def test_no_jump_cut_after_a_dangling_word(self):
        ws = [(1.0, 1.3, "go."), (2.0, 2.2, "I"), (5.0, 5.3, "know."), (6.0, 6.3, "ok.")]
        ts = K.np.arange(0, 8, 0.1) + 0.25
        with mock.patch.object(K, "rms_curve", return_value=(ts, K.np.where((ts > 2.3) & (ts < 4.9), 0.01, 1.0))):
            segs = K.dead_air(ws, None, 0.0, 7.0, None)
        self.assertEqual(segs, [(0.0, 7.0)])


class ExcludeRe(unittest.TestCase):                        # 16
    def test_empty_exclude_titles_matches_nothing(self):
        for src, launched in (({}, True), ({"exclude_titles": ""}, True), ({"exclude_titles": "|"}, False),
                              ({"exclude_titles": "leak|fake"}, True)):
            with mock.patch.object(K, "load_json", return_value=src), mock.patch.object(K, "launched",
                                                                                         return_value=launched):
                bad = K.exclude_re()
                if bad:
                    self.assertIsNone(K.re.search(bad, "GTA 6 is crazy", K.re.I), (src, bad))
                    self.assertNotIn("||", bad)
                    self.assertFalse(bad.startswith("|") or bad.endswith("|"))
        with mock.patch.object(K, "load_json", return_value={"exclude_titles": "leak"}), \
                mock.patch.object(K, "launched", return_value=True):
            self.assertTrue(K.re.search(K.exclude_re(), "GTA 6 Trailer reaction", K.re.I))


class Swears(unittest.TestCase):                           # 17
    def test_list(self):
        for w in ("fuck", "Fucking", "motherfucker", "clusterfuck", "shit", "bullshit", "bitches", "asshole",
                  "cunt", "pussy", "whore", "sluts", "twat", "wanker", "dick", "dickhead", "f***", "sh*t"):
            self.assertTrue(K.SWEAR_RE.match(K._tok(w)), w)
        for w in ("cockpit", "Dickens", "dictionary", "pushy", "assassin", "class", "shift",
                  "scrap", "glass", "assume"):
            self.assertFalse(K.SWEAR_RE.match(K._tok(w)), w)


class AfterPeak(unittest.TestCase):                        # 5
    def test_fix_cuts_never_ends_before_payoff_plus_3(self):
        # end at 6.5 (inside 'god.'): plain re-snap could fall back to 3.8 ('me!'), before the payoff
        a, b, segs, hook, fixed, left = K.fix_cuts(W, 0.5, 6.5, [(0.5, 6.5)], None, dur=12.0, peak=6.0)
        self.assertGreaterEqual(b, 9.0 - 0.01)
        self.assertTrue(K.ok_end(W, b), b)
        self.assertEqual(left, [])

    def test_fix_cuts_extends_forward_when_no_sentence_end_after(self):
        ws = W[:7] + [(6.0, 6.2, "Oh"), (6.2, 6.6, "my"), (6.6, 9.9, "goooood")]   # talks to the end
        a, b, segs, hook, fixed, left = K.fix_cuts(K.cut_words(ws, 10.0), 0.5, 6.4, [(0.5, 6.4)], None,
                                                   dur=10.0, peak=6.0)
        self.assertGreaterEqual(b, 9.0 - 0.01)               # moved forward as far as it must
        self.assertTrue(left)                                # but the source cuts the word: the clip fails

    def test_trim_points_floor(self):
        a, b = K.trim_points(W, 6.0, 12.0)
        self.assertGreaterEqual(b - 6.0, K.AFTER_PEAK - 0.01)
        self.assertTrue(K.ok_end(W, b))

    def test_dead_air_keeps_the_reaction(self):
        # a long quiet pause right after the payoff is never jump-cut (card - peak would shrink)
        ws = [(1.0, 1.5, "go."), (3.0, 3.4, "now."), (6.0, 6.4, "wow."), (8.5, 9.0, "nice."), (14.0, 14.4, "ok.")]
        ts = K.np.arange(0, 16, 0.1) + 0.25
        with mock.patch.object(K, "rms_curve", return_value=(ts, K.np.where((ts > 9.2) & (ts < 13.8), 0.01, 1.0))):
            segs = K.dead_air(ws, None, 0.0, 15.0, 6.0)
        for (x, y), (u, v) in zip(segs, segs[1:]):
            self.assertFalse(y < 6.0 + K.AFTER_PEAK + 0.5 and u > 5.0, segs)
        self.assertGreater(len(segs), 1)                     # the later dead pause is still cut


class QaStructure(unittest.TestCase):                      # 4, 13
    def plan(self, **kw):
        p = dict(total=20.0, main_end=17.4, card_at=17.4, card=None, heat_on=False, heat=False, loud_hook=False,
                 elements=[], face_bottom=640, cap_times=[], cap_box=(40, 560, 1040, 720), peak=14.0,
                 hook_end=2.3)
        p.update(kw)
        return p

    def qa(self, plan):
        with mock.patch.object(K, "duration", return_value=plan["total"]), \
                mock.patch.object(K, "ff_stats", return_value=([], [], -14.0, -1.5)), \
                mock.patch.object(K, "boredom", return_value=([], {"dead_s": 0})):
            return K.qa_short("x.mp4", plan)

    def test_ok(self):
        self.assertEqual(self.qa(self.plan()), [])

    def test_too_little_after_climax(self):
        probs = self.qa(self.plan(peak=15.6))
        self.assertTrue(any("after the climax" in p for p in probs), probs)

    def test_long_hook(self):
        probs = self.qa(self.plan(hook_end=2.7))
        self.assertTrue(any(p.startswith("hook 2.70") for p in probs), probs)

    def test_fix_cuts_clamps_hook(self):
        _, _, _, hook, _, left = K.fix_cuts(W, 0.5, 12.0, [(0.5, 12.0)], (4.2, 6.9), dur=12.0, peak=6.0)
        self.assertLessEqual(hook[1] - hook[0], K.HOOK_MAX + 1e-6)
        self.assertFalse([x for x in left if x.startswith("hook")])


class Bleeps(unittest.TestCase):                           # 6
    def test_swear_under_the_card_is_bleeped_not_captioned(self):
        ws = [(1.0, 1.3, "what"), (2.0, 2.4, "fuck!"), (10.5, 10.9, "shit."), (12.5, 12.9, "bitch")]
        caps, mute = K.swear_plan(ws, card_at=10.0, sound_end=12.0)
        self.assertEqual([w[2] for w in caps], ["what", "F***!"])        # captions only before the card
        self.assertIn((10.5, 10.9), mute)                                 # under the card: bleeped
        self.assertIn((2.0, 2.4), mute)
        self.assertFalse([m for m in mute if m[0] >= 12.0])               # not after the clip's sound stops


class PartialSentences(unittest.TestCase):                 # 10, 11
    def test_viewer_clip_starting_mid_speech(self):
        ws = [(0.05, 0.3, "and"), (0.3, 0.6, "then"), (0.6, 0.9, "he."), (1.6, 1.8, "Look"), (1.8, 2.2, "at"),
              (2.2, 2.5, "this."), (12.0, 12.4, "WHAT."), (14.0, 14.3, "no.")]
        a, b = K.trim_points(ws, 12.2, 18.0)
        self.assertGreaterEqual(a, 0.9)                      # the fragment 'and then he.' is gone
        self.assertLessEqual(a, 1.6)
        self.assertTrue(K.ok_start(ws, a))

    def test_partial_start_kept_when_it_would_cut_the_build_up(self):
        ws = [(0.05, 0.3, "and"), (0.3, 4.0, "then"), (4.5, 4.8, "Look."), (6.0, 6.4, "WHAT.")]
        self.assertEqual(K.skip_partial_start(ws, 0.0, 6.2, 12.0), 0.0)

    def test_last_word_touching_the_end_is_mid_sentence(self):
        ws = [(1.0, 1.4, "Go."), (2.0, 2.3, "and"), (2.3, 9.95, "then")]
        cw = K.cut_words(ws, 10.0)
        self.assertFalse(K.ok_end(cw, 10.0))
        self.assertTrue(K.ok_end(K.cut_words(ws[:2] + [(2.3, 9.95, "then.")], 10.0), 10.0))

    def test_dropped_segments_stay_occupied(self):
        ws = K.Words([(1.0, 1.4, "Go.")], busy=[(3.0, 3.4, K.BUSY), (3.4, 3.9, K.BUSY)])
        cw = K.cut_words(ws)
        self.assertIsNotNone(K.in_word(cw, 3.2))
        self.assertFalse(K.ok_start(cw, 3.42))               # between two dropped words = inside speech
        self.assertEqual(len(ws), 1)                         # captions unchanged


class SameMoment(unittest.TestCase):                       # 12
    def test_cluster_by_channel_and_created(self):
        a = {"id": "a", "channel": "kai", "created": 1000, "views": 900}
        b = {"id": "b", "channel": "kai", "created": 1020, "views": 400}
        c = {"id": "c", "channel": "kai", "created": 1100, "views": 300}
        d = {"id": "d", "channel": "xqc", "created": 1005, "views": 300}
        e = {"id": "e", "channel": "kai", "created": 1010, "views": 100, "video_id": "v", "vod_offset": 50}
        out = K.cluster([a, b, c, d, e])
        self.assertEqual(sorted(x["id"] for x in out), ["a", "c", "d"])
        self.assertEqual(next(x for x in out if x["id"] == "a")["cluster"], 3)

    def test_youtube_windows_of_one_video_are_different_moments(self):
        a = {"id": "youtube_v_100", "channel": "kai", "created": 1000, "views": 9, "platform": "youtube",
             "section": [100, 135]}
        b = {**a, "id": "youtube_v_1800", "section": [1800, 1835]}
        self.assertEqual(len(K.cluster([a, b])), 2)
        tmp = Path(tempfile.mkdtemp())
        try:
            st = tmp / "state.json"
            st.write_text("{}", encoding="utf-8")
            with mock.patch.object(K, "STATE", st):
                K.save_moment(a)
                self.assertFalse(K.moment_done(b))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_second_clip_of_a_moment_is_skipped_across_runs(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            st = tmp / "state.json"
            st.write_text("{}", encoding="utf-8")
            with mock.patch.object(K, "STATE", st):
                K.save_moment({"id": "a", "channel": "kai", "created": 1000})
                self.assertTrue(K.moment_done({"id": "b", "channel": "kai", "created": 1025}))
                self.assertFalse(K.moment_done({"id": "c", "channel": "kai", "created": 1040}))
                self.assertFalse(K.moment_done({"id": "a", "channel": "kai", "created": 1000}))
                self.assertFalse(K.moment_done({"id": "x", "channel": "xqc", "created": 1000}))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class CorruptState(unittest.TestCase):                     # 7, 3
    def test_corrupt_state_is_kept_and_raises(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            st = tmp / "state.json"
            st.write_text('{"done": {"a": 1}, ', encoding="utf-8")
            with mock.patch.object(K, "STATE", st):
                with self.assertRaises(K.CorruptJson):
                    K.mark("b", True)
            self.assertEqual(st.read_text(encoding="utf-8"), '{"done": {"a": 1}, ')   # not overwritten
            self.assertTrue((tmp / "state.json.bad").exists())
            other = tmp / "other.json"
            other.write_bytes(b"\xff\xfe{")
            self.assertEqual(K.load_json(other, {"x": 1}), {"x": 1})       # non-critical: default
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_render_failure_is_a_try_not_other_game(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            st = tmp / "state.json"
            st.write_text("{}", encoding="utf-8")
            clip = {"id": "c1", "platform": "twitch", "channel": "kai", "title": "x", "views": 9, "category": "GTA VI",
                    "url": "u", "created": 0}
            with mock.patch.object(K, "STATE", st), mock.patch.object(K, "is_game", return_value=True), \
                    mock.patch.object(K, "skip", return_value=False), \
                    mock.patch.object(K, "render", side_effect=K.RenderFailed("QA failed: true peak")):
                K.process([clip])
            s = json.loads(st.read_text(encoding="utf-8"))
            self.assertEqual(s.get("failed", {}).get("c1"), 1)
            self.assertNotIn("c1", s.get("other_game", {}))
            self.assertIn("true peak", s["failed_why"]["c1"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def make_talk_page(out, face_s=9.0, page_s=11.0, page_first=False):
    """The DarkViperAU case (18. 11.): someone's face full-frame (talking: it moves a little), then a
    static web page (a channel page: boxes and text), no game at all. 1920x1080, with sound."""
    face = HERE / "assets" / "model_reakce" / "smirk.png"
    font = str(HERE / "fonts" / "montserrat-600.ttf").replace("\\", "/").replace(":", "\\:")
    tmp = Path(out).with_suffix("")
    a, b = tmp.with_name(tmp.name + "_a.mp4"), tmp.with_name(tmp.name + "_b.mp4")
    enc = ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-r", "30"]
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=0x30343a:s=1920x1080:r=30:d={face_s}",
                    "-loop", "1", "-t", f"{face_s}", "-i", str(face), "-filter_complex",
                    "[1:v]scale=-1:1900[f];[0:v][f]overlay=x='420+18*sin(2*PI*0.7*t)':y='-260+12*sin(2*PI*1.3*t)'"
                    ":shortest=1[v]", "-map", "[v]", *enc, str(a)], check=True)
    boxes = ",".join(f"drawbox=x={60 + 300 * (i % 6)}:y={420 + 300 * (i // 6)}:w=270:h=160:color=0xd0d0d0:t=fill"
                     for i in range(12))
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=white:s=1920x1080:r=30:d={page_s}",
                    "-vf", f"drawbox=x=0:y=0:w=1920:h=90:color=0x202020:t=fill,drawbox=x=60:y=150:w=1800:h=220:color=0xb04040:t=fill,"
                    f"{boxes},drawtext=fontfile='{font}':text='DarkViperAU - Videos':x=80:y=380:fontsize=44:fontcolor=black",
                    *enc, str(b)], check=True)
    first, second = (b, a) if page_first else (a, b)
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(first), "-i", str(second), "-f", "lavfi", "-i",
                    f"sine=f=180:d={face_s + page_s}", "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]",
                    "-map", "2:a", "-shortest", *enc, "-c:a", "aac", str(out)], check=True)
    a.unlink(missing_ok=True)
    b.unlink(missing_ok=True)


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class GameplayGate(unittest.TestCase):                     # coordinator 18. 11.: DarkViperAU / GT5 talk passed QA
    def test_talking_head_and_web_page_rejected(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            for name, kw in (("mix", {}), ("page_first", {"page_first": True}), ("face_only", {"page_s": 2.0}),
                             ("page_only", {"face_s": 2.0, "page_s": 14.0})):
                f = tmp / f"{name}.mp4"
                make_talk_page(f, **kw)
                probs, st = K.gameplay_check(f)
                self.assertTrue(probs and all(p.startswith("not gameplay") for p in probs), (name, st))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_failed_probe_never_passes(self):
        with self.assertRaises(RuntimeError):
            K.gameplay_check(HERE / "no_such_file.mp4")

    def test_game_band_of_a_short(self):
        # the finished short: facecam on top, the 'game band' below shows the page -> rejected in qa_short too
        tmp = Path(tempfile.mkdtemp())
        try:
            f = tmp / "src.mp4"
            make_talk_page(f, face_s=2.0, page_s=12.0)
            probs, _ = K.gameplay_check(f, crop="1920:720:0:360")
            self.assertTrue(probs)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_real_gameplay_passes(self):
        clips = [p for p in (HERE.parent.parent.parent / "out" / "_test_cam", HERE / "out" / "_test_cam") if p.exists()]
        if not clips:
            self.skipTest("no test clips (out/_test_cam)")
        for n in ("xqc", "garek", "kai_gta3", "kai_gta5", "neverossa", "syndicate"):
            f = clips[0] / f"{n}.mp4"
            if f.exists():
                probs, st = K.gameplay_check(f)
                self.assertEqual(probs, [], (n, st))


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class TruePeak(unittest.TestCase):                         # 2
    def test_gunshots_pass_after_the_aac_encode(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            f = tmp / "boom.mp4"
            # a game bed (noise + hum) with gunshot bursts (instant attack, fast decay), through the mix's
            # limiter (audio_mix) and AAC like encode(): -16 LUFS, the old single pass ended at +0.5 dBTP
            expr = "0.2*(2*random(0)-1)+0.15*sin(2*PI*180*t)+0.9*exp(-mod(t\\,0.7)*30)*(2*random(1)-1)"
            subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=64x64:r=30:d=12",
                            "-f", "lavfi", "-i", f"aevalsrc='{expr}':s=48000:d=12", "-shortest", "-c:v", "libx264",
                            "-af", "lowpass=f=9000,alimiter=limit=0.7:level=0:attack=2:release=40",
                            "-c:a", "aac", "-b:a", "128k", str(f)], check=True)
            old = tmp / "old.mp4"                            # the old fix: one loudnorm pass, TP -2
            K._loudnorm_pass(f, old, -2.0, False)
            self.assertGreater(K.loud_stats(old)[1], -1.0)   # = the bug: QA failed both looks
            r = K.loudness_fix(f)
            self.assertIsNotNone(r)
            lufs, tp = K.loud_stats(f)
            self.assertLessEqual(tp, K.TP_SAFE + 0.05, (r, lufs, tp))
            self.assertLessEqual(abs(lufs - K.LUFS_TARGET), K.LUFS_TOL, (r, lufs, tp))
            self.assertFalse(list(tmp.glob("*_ln*.mp4")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
