"""
outro_cap.py - burn fanpage captions into the fanpage outro (run again when the outro changes).

  .venv/Scripts/python tools/follow/outro_cap.py [--whisper small.en]
      assets/outro/fanpage_outro.mp4 -> assets/outro/fanpage_outro_cap.mp4

Whisper word timestamps -> captions_ass in the fanpage style of brands.json
(Anton, white + pink highlight, one word at a time, outline 3). No speech -> no file.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE))
import klipuj as k  # noqa: E402

SRC = HERE / "assets" / "outro" / "fanpage_outro.mp4"
DST = HERE / "assets" / "outro" / "fanpage_outro_cap.mp4"
CAP_Y = 1060          # middle of the picture: under the face, above the Follow card (y 1290-1510)


def main():
    k.WHISPER_MODEL = "small.en"                      # medium.en hung here on the 6 GB card; small is exact on this line
    if "--whisper" in sys.argv:
        k.WHISPER_MODEL = sys.argv[sys.argv.index("--whisper") + 1]
    # Whisper directly, not klipuj.transcribe: its lyrics filter (steady pitch = singing) drops
    # the outro's even voice, and here everything spoken is ours
    import whisper
    model = whisper.load_model(k.WHISPER_MODEL, download_root=str(k.WHISPER_DIR))
    res = model.transcribe(str(SRC), word_timestamps=True, condition_on_previous_text=False,
                           fp16=model.device.type == "cuda", verbose=None)
    words = [(w["start"], w["end"], w["word"].strip()) for seg in res["segments"] if seg.get("no_speech_prob", 0) <= 0.5
             for w in seg.get("words", []) if w["word"].strip()]
    print("speech:", " ".join(w for _, _, w in words) or "(none)")
    if not words:
        print("no speech in the outro -> no captions, nothing written")
        return
    cap = {**(k.brand_cfg(k.brands(), "fanpage").get("caption") or {}), "outline": 3, "shadow": 0}
    ass = DST.with_suffix(".ass")
    k.captions_ass(words, ass, y=CAP_Y, cap=cap, per=1, anim="bounce")
    try:
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(SRC), "-vf",
                        f"subtitles=filename='{k.ff_path(ass)}':fontsdir='{k.ff_path(k.CAP_FONTS)}'",
                        "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-pix_fmt", "yuv420p",
                        "-c:a", "copy", "-movflags", "+faststart", str(DST)], check=True)
    finally:
        ass.unlink(missing_ok=True)
    print(f"{DST.name}: {DST.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
