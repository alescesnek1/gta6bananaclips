"""
Regression test of the facecam detector (klipuj.detect_cam / get_cam).

  .venv/Scripts/python tools/test_facecam.py            # downloads the clips once into out/_test_cam/
  .venv/Scripts/python tools/test_facecam.py --only garek

Cases:
  garek     RP conversation, NO webcam on stream; the in-game character's face sat still for most of
            the clip and went into the top band as "facecam" (17. 11.) -> must be None
  kai_gta5  Kai Cenat, small webcam left + in-game close-up of Wade in the middle -> webcam (left)
  kai_gta3  Kai Cenat, webcam top-left over GTA 3 -> webcam
  xqc       xQc RP, webcam top-left -> webcam
  syndicate webcam top-right -> webcam
  neverossa webcam bottom-left -> webcam
  carmen    no webcam -> None
  posters   no webcam, faces on posters in a static room (twinkles) -> None
  avatar    no webcam, a still profile picture in an in-game browser (local file only) -> None
Also checks that a WRONG cached auto box (the garek face) does not survive get_cam.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("KLIPY_STATE", str(Path(tempfile.gettempdir()) / "klipy_test_state.json"))
import klipuj as K   # noqa: E402

CACHE = K.OUT / "_test_cam"
# name, clip url, expected: None = no facecam, else (x0, x1, y0, y1) range in % the box centre must fall into
CASES = [
    ("garek", "https://www.twitch.tv/garek/clip/SaltyPlainSandwichDeIlluminati-vjQJfypl2wSMXfbY", None),
    ("kai_gta5", "https://www.twitch.tv/kaicenat/clip/PuzzledLongAntelopeDxAbomb-G8QCjK1905yV3xMQ", (0, 25, 25, 50)),
    ("kai_gta3", "https://www.twitch.tv/kaicenat/clip/SmoothAstuteBearWow-B2HPApzPMQLrZHGr", (0, 30, 0, 30)),
    ("xqc", "https://www.twitch.tv/xqc/clip/SquareGlamorousOryxCorgiDerp-0zJf6dItCef8GAbu", (0, 30, 0, 45)),
    ("syndicate", "https://www.twitch.tv/syndicate/clip/HelplessUnusualClintmullinsGivePLZ-qGjd8YbDceVdtvc9", (70, 100, 10, 45)),
    ("neverossa", "https://www.twitch.tv/neverossa/clip/ThankfulWanderingNostrilSeemsGood-Re1kRlh8Wal_SNmP", (0, 25, 60, 100)),
    ("carmen", "https://www.twitch.tv/carmen/clip/AltruisticObedientCrocodileThunBeast-La3saX4PyFrJ6oiT", None),
    # no webcam; posters with faces on the wall of a static RP room (faces never move)
    ("posters", "https://www.twitch.tv/twinkles/clip/FineReliableLocustPicoMause-IcRskCSFvsSow1sY", None),
    # no webcam; a browser tab in the game shows a co-stream with a still profile picture bottom-left
    ("avatar", None, None),
]

def fetch(name, url):
    CACHE.mkdir(parents=True, exist_ok=True)
    dst = CACHE / f"{name}.mp4"
    if not dst.exists() and not url:
        return None                                       # local-only sample, not downloadable
    if not dst.exists():
        subprocess.run(["yt-dlp", "-q", "--no-warnings", "-f", "best[height<=1080]/best", "-o", str(dst), url],
                       check=True, stdin=subprocess.DEVNULL, timeout=600)
    return dst

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    a = ap.parse_args()
    tmp_db = Path(tempfile.mkdtemp()) / "streamers.json"
    K.STREAMERS = tmp_db                                  # never touch the real streamers.json
    fails = 0
    for name, url, want in CASES:
        if a.only and a.only != name:
            continue
        src = fetch(name, url)
        if src is None:
            print(f"SKIP {name:10s} (put the sample at {CACHE / (name + '.mp4')})")
            continue
        cam = K.detect_cam(src)
        if want is None:
            ok = cam is None
        else:
            cx, cy = (cam[0] + cam[2] / 2, cam[1] + cam[3] / 2) if cam else (-1, -1)
            ok = bool(cam) and want[0] <= cx <= want[1] and want[2] <= cy <= want[3]
        fails += not ok
        print(f"{'OK  ' if ok else 'FAIL'} {name:10s} cam={cam} expected={'none' if want is None else want}", flush=True)
    if not a.only or a.only == "garek":
        # the wrong box from 17. 11. cached as auto: re-validation must drop it
        tmp_db.write_text(json.dumps({"garek": {"cam": [21.98, 21.87, 25.0, 26.34], "auto": True}}), encoding="utf-8")
        cam = K.get_cam("garek", fetch("garek", CASES[0][1]))
        left = json.loads(tmp_db.read_text(encoding="utf-8"))
        ok = cam is None and "garek" not in left
        fails += not ok
        print(f"{'OK  ' if ok else 'FAIL'} cached wrong garek box -> get_cam={cam}, streamers.json keeps garek: {'garek' in left}")
    print("ALL OK" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)

if __name__ == "__main__":
    main()
