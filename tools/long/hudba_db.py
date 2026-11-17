"""
hudba_db.py - the long-form music database: energetic background tracks, CC0 only, from Openverse.

  python tools/long/hudba_db.py hledej  [--cache DIR]     # search + download candidates + measure -> cache/report.json
  python tools/long/hudba_db.py vyber   [--cache DIR] [--n 6]   # best per brand -> assets/hudba/long/<brand>/*.mp3 + sources.json
  python tools/long/hudba_db.py zmer <file...>            # the same measurements for our own tracks

Openverse API without a key (api.openverse.org/v1/audio/?q=...&license=cc0), duration >= 90 s. Nobody
listens here, so every candidate is measured ("listen proxy") and weak / lo-fi / noisy ones are out:
  lufs, lra      ffmpeg ebur128 (a track with a huge loudness range has long quiet parts)
  flatness       spectral flatness 0..1 (noise, field recordings, pads without notes are high)
  air            energy above 5 kHz against the whole (dB): hi-hats and presence; lo-fi / muffled = very low
  pulse, bpm     onset envelope autocorrelation: how clear the beat is and its tempo
  quiet          share of 1 s windows more than 18 dB under the track's loud level (breaks, intros)
  energy         0..1 for ordering inside a video (the music gets stronger towards #1)
Titles/tags that say vocals, speech, ambience, field recording are skipped before the download.
assets/hudba/long/sources.json keeps url, creator, license and the numbers for every kept track.
"""
import argparse
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parents[2]
DEST = HERE / "assets" / "hudba" / "long"
API = "https://api.openverse.org/v1/audio/"
UA = {"User-Agent": "music-finder/1.0"}
SR = 22050
AIR_MIN = -34.0                              # our own produced tracks sit at -20..-33 dB

QUERIES = {
    "bananas": ["phonk", "trap beat", "hip hop beat instrumental", "funk groove", "energetic beat", "upbeat electronic",
                "drum and bass", "breakbeat", "hype beat", "electro house", "boom bap", "funky beat", "dubstep",
                "bass music", "gaming music", "action music", "rock instrumental energetic", "chiptune"],
    "fanpage": ["synthwave", "retrowave", "outrun", "night drive", "deep house", "dark synth", "cyberpunk music",
                "electro pop instrumental", "house music", "techno", "80s synth", "neon", "future bass", "nu disco",
                "trance", "electronic dance"],
}
SKIP = re.compile(r"\b(vocals?|voice|speech|spoken|sing(s|ing|er)?|rap(per)?|acapella|lyrics|choir|ambien(t|ce)|atmo\w*|"
                  r"field|recording|drone|soundscape|noise|interview|podcast|talk|radio|live|improv\w*|jam|rehearsal|"
                  r"demo|test|sketch|piano|guitar solo|meditation|relax\w*|sleep|sad|slow|lullaby|christmas|rain|"
                  r"wind|birds?|crowd|street|city|traffic|engine|car)\b", re.I)


def get(url):
    for i in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                return r.read()
        except Exception as e:
            if "429" in str(e):
                time.sleep(20 * (i + 1))
            elif i == 3:
                raise
            else:
                time.sleep(3)
    return b""


def search(q, pages=2):
    out = []
    for page in range(1, pages + 1):
        url = API + "?" + urllib.parse.urlencode({"q": q, "license": "cc0", "page_size": 20, "page": page})
        try:
            d = json.loads(get(url) or b"{}")
        except Exception as e:
            print(f"  {q} p{page}: {str(e)[-80:]}")
            break
        for r in d.get("results") or []:
            dur = (r.get("duration") or 0) / 1000
            text = f"{r.get('title') or ''} {' '.join(t.get('name', '') for t in (r.get('tags') or [])[:12])}"
            if r.get("license") != "cc0" or not 90 <= dur <= 480 or SKIP.search(text):
                continue
            out.append({"id": r["id"], "title": (r.get("title") or "").strip(), "creator": r.get("creator") or "",
                        "creator_url": r.get("creator_url") or "", "license": "CC0 " + (r.get("license_version") or "1.0"),
                        "license_url": r.get("license_url") or "https://creativecommons.org/publicdomain/zero/1.0/",
                        "url": r["url"], "page": r.get("foreign_landing_url") or "", "source": r.get("source") or "",
                        "duration": round(dur, 1), "query": q})
        if page >= (d.get("page_count") or 1):
            break
        time.sleep(1.5)
    return out


def measure(path):
    """The listen proxy: numbers that tell a punchy, clean, rhythmic track from a weak / noisy / lo-fi one."""
    p = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(path), "-af", "ebur128=peak=true", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    summ = p.stderr[p.stderr.rfind("Summary:"):]
    num = lambda rx: float(m.group(1)) if (m := re.search(rx, summ)) and m.group(1) != "-inf" else None
    lufs, lra, tp = num(r"I:\s+(-?[\d.]+) LUFS"), num(r"LRA:\s+(-?[\d.]+) LU"), num(r"Peak:\s+(-?[\d.]+|-inf) dBFS")
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                         capture_output=True, timeout=300).stdout
    x = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
    if x.size < SR * 30 or lufs is None:
        return None
    n, hop = 1024, 512
    k = (x.size - n) // hop
    idx = np.arange(n)[None, :] + hop * np.arange(k)[:, None]
    spec = np.abs(np.fft.rfft(x[idx] * np.hanning(n), axis=1)) ** 2 + 1e-12
    freqs = np.fft.rfftfreq(n, 1 / SR)
    en = spec.sum(axis=1)
    live = en > np.percentile(en, 30)
    flat = float(np.mean(np.exp(np.log(spec[live]).mean(axis=1)) / spec[live].mean(axis=1)))
    cum = np.cumsum(spec[live].mean(axis=0))
    rolloff = float(freqs[int(np.searchsorted(cum, 0.95 * cum[-1]))])
    bass = float(spec[live][:, (freqs >= 40) & (freqs < 160)].sum() / spec[live].sum())
    air = float(10 * np.log10(spec[live][:, freqs >= 5000].sum() / spec[live].sum() + 1e-12))   # hats / presence
    # onset envelope (spectral flux) -> tempo + how clear the pulse is
    flux = np.maximum(0, np.diff(np.log1p(1000 * np.sqrt(spec)), axis=0)).sum(axis=1)
    flux = flux - np.convolve(flux, np.ones(43) / 43, mode="same")
    flux = np.maximum(0, flux)
    fr = SR / hop                                              # 43 frames / s
    ac = np.correlate(flux[: int(fr * 90)], flux[: int(fr * 90)], mode="full")[min(flux.size, int(fr * 90)) - 1:]
    lags = np.arange(int(fr * 60 / 185), int(fr * 60 / 70))    # 70-185 BPM
    best = lags[int(np.argmax(ac[lags]))]
    pulse = float(ac[best] / (ac[0] + 1e-9))
    bpm = 60 * fr / best
    # quiet parts: 1 s windows vs the loud level of the track
    w = int(SR)
    sq = (x[: x.size // w * w].astype(np.float64) ** 2).reshape(-1, w)
    rms = np.sqrt(sq.mean(axis=1) + 1e-12)
    db = 20 * np.log10(rms)
    quiet = float((db < np.percentile(db, 90) - 18).mean())
    onset_rate = float((flux > flux.mean() + 1.5 * flux.std()).sum() / (flux.size / fr))
    energy = (0.35 * min(1, pulse / 0.5) + 0.25 * min(1, onset_rate / 6) + 0.2 * min(1, max(0, (lufs + 24) / 14))
              + 0.2 * min(1, max(0, (bpm - 80) / 80)))
    return {"lufs": lufs, "lra": lra, "tp": tp, "flatness": round(flat, 4), "rolloff": round(rolloff), "bass": round(bass, 3), "air": round(air, 1),
            "pulse": round(pulse, 3), "bpm": round(bpm, 1), "quiet": round(quiet, 3), "onsets": round(onset_rate, 2),
            "energy": round(energy, 3), "dur": round(x.size / SR, 1)}


def verdict(m):
    """[] = good enough for the database, else the reasons it is out."""
    if m is None:
        return ["unreadable / too short"]
    bad = []
    if m["pulse"] < 0.16:
        bad.append(f"no clear beat (pulse {m['pulse']})")
    if m["flatness"] > 0.15:
        bad.append(f"noisy (flatness {m['flatness']})")
    if m["air"] < AIR_MIN:
        bad.append(f"muffled / lo-fi (energy above 5 kHz {m['air']} dB)")
    if m["quiet"] > 0.12:
        bad.append(f"quiet parts {m['quiet']:.0%}")
    if (m["lra"] or 0) > 11:
        bad.append(f"loudness range {m['lra']} LU")
    if m["lufs"] < -26:
        bad.append(f"weak ({m['lufs']} LUFS)")
    if m["onsets"] < 1.5:
        bad.append(f"sparse ({m['onsets']} onsets/s)")
    if m["bass"] < 0.03:
        bad.append("no low end")
    return bad


def cmd_hledej(a):
    cache = Path(a.cache)
    cache.mkdir(parents=True, exist_ok=True)
    rep = cache / "report.json"
    rows = json.loads(rep.read_text(encoding="utf-8")) if rep.exists() else {}
    for brand, qs in QUERIES.items():
        for q in qs:
            found = search(q)
            print(f"{brand} '{q}': {len(found)} candidates >= 90 s")
            for r in found:
                if r["id"] in rows:
                    continue
                f = cache / f"{r['id']}.mp3"
                try:
                    if not f.exists():
                        f.write_bytes(get(r["url"]))
                    m = measure(f)
                except Exception as e:
                    m = None
                    print(f"    {r['title'][:40]}: {str(e)[-80:]}")
                rows[r["id"]] = {**r, "brand": brand, "m": m, "bad": verdict(m), "file": f.name}
                print(f"    {'OK ' if not rows[r['id']]['bad'] else 'out'} {r['title'][:44]:<44} {m and m['bpm']} bpm "
                      f"pulse {m and m['pulse']} {'; '.join(rows[r['id']]['bad'])}")
                rep.write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
            time.sleep(1.5)
    ok = [r for r in rows.values() if not r["bad"]]
    print(f"\n{len(rows)} measured, {len(ok)} pass: " + ", ".join(f"{b} {sum(1 for r in ok if r['brand'] == b)}" for b in QUERIES))


OWN = {                                                        # (file, licence, creator): our own tracks, >= 90 s
    "bananas": [("assets/hudba/The_Heavy_Toll.mp3", "own (generated in Gemini)", "gta6bananaclips"),
                ("assets/hudba/Under_Heavy_Boots.mp3", "own (generated in Gemini)", "gta6bananaclips")],
    "fanpage": [("assets/hudba/fanpage/Asphalt_Swagger.mp3", "own (generated in Gemini)", "gta6fanpageclip"),
                ("assets/hudba/fanpage/The_Champion_s_View.mp3", "own (generated in Gemini)", "gta6fanpageclip")],
}


def slug(s):
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:48] or "track"


def cmd_vyber(a):
    cache = Path(a.cache)
    rows = json.loads((cache / "report.json").read_text(encoding="utf-8"))
    only = set(a.jen or [])
    drop = set(a.bez or [])
    src_file = DEST / "sources.json"
    db = {"_napoveda": "Long-form background music. Openverse, CC0 only (no attribution needed; kept anyway). "
                       "tools/long/hudba_db.py. energy 0..1 orders the tracks inside a video.", "tracks": []}
    for brand in QUERIES:
        pre = lambda ids, i: any(i.startswith(x) for x in ids)   # ids may be given as prefixes
        good = [r for r in rows.values() if r["brand"] == brand and not r["bad"] and not pre(drop, r["id"])
                and (not only or pre(only, r["id"]))]
        good.sort(key=lambda r: -(r["m"]["energy"] + 0.5 * r["m"]["pulse"]))
        seen, kept = set(), []
        for r in good:                                        # one creator at most twice: variety
            if (not only and sum(1 for k in kept if k["creator"] == r["creator"]) >= 2) or slug(r["title"]) in seen:
                continue
            seen.add(slug(r["title"]))
            kept.append(r)
            if len(kept) >= a.n:
                break
        d = DEST / brand
        d.mkdir(parents=True, exist_ok=True)
        for old in d.glob("*.mp3"):
            old.unlink()
        for r in kept:
            out = d / f"{slug(r['title'])}.mp3"
            # one loudness for the whole database (-16 LUFS), so tracks can be crossfaded without level jumps
            subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(cache / r["file"]), "-vn", "-af",
                            f"loudnorm=I=-16:TP=-1.5:LRA=11:measured_I={r['m']['lufs']}:measured_TP={r['m']['tp'] or -1}:"
                            f"measured_LRA={r['m']['lra'] or 5}:measured_thresh={r['m']['lufs'] - 10}:linear=true,aresample=48000",
                            "-c:a", "libmp3lame", "-b:a", "192k", str(out)], check=True)
            db["tracks"].append({"file": str(out.relative_to(HERE)).replace("\\", "/"), "brand": brand, "title": r["title"],
                                 "creator": r["creator"], "creator_url": r["creator_url"], "license": r["license"],
                                 "license_url": r["license_url"], "url": r["page"] or r["url"], "audio_url": r["url"],
                                 "source": f"Openverse ({r['source']})", "openverse_id": r["id"], **r["m"],
                                 "lufs": -16.0, "lufs_src": r["m"]["lufs"]})
            print(f"{brand}: {out.name}  {r['m']['bpm']} bpm energy {r['m']['energy']} pulse {r['m']['pulse']}  ({r['creator']})")
    for brand, files in OWN.items():                           # our existing tracks join the database in place
        for f, lic, who in files:
            if not (HERE / f).exists():
                continue
            m = measure(HERE / f)
            if m is None or m["dur"] < 90 or (verdict(m) and not a.vse):
                print(f"{brand}: own {Path(f).name} out: {'; '.join(verdict(m)) or 'shorter than 90 s'}")
                continue
            db["tracks"].append({"file": f, "brand": brand, "title": Path(f).stem.replace("_", " "), "creator": who,
                                 "license": lic, "url": "", "source": "own library", **m})
            print(f"{brand}: own {Path(f).name}  {m['bpm']} bpm energy {m['energy']}")
    src_file.write_text(json.dumps(db, indent=1, ensure_ascii=False), encoding="utf-8")
    print(src_file)


def cmd_zmer(a):
    for f in a.files:
        m = measure(f)
        print(f"{Path(f).name}: {json.dumps(m)}  -> {'OK' if not verdict(m) else '; '.join(verdict(m))}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("hledej")
    s.add_argument("--cache", default=str(HERE / "out" / "_hudba_cache"))
    s = sub.add_parser("vyber")
    s.add_argument("--cache", default=str(HERE / "out" / "_hudba_cache"))
    s.add_argument("--n", type=int, default=6)
    s.add_argument("--jen", nargs="*", help="only these Openverse ids")
    s.add_argument("--bez", nargs="*", help="never these Openverse ids")
    s.add_argument("--vse", action="store_true", help="keep our own tracks even when they fail the measurements")
    s = sub.add_parser("zmer")
    s.add_argument("files", nargs="+")
    a = p.parse_args()
    {"hledej": cmd_hledej, "vyber": cmd_vyber, "zmer": cmd_zmer}[a.cmd](a)
