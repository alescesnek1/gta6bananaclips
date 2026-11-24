"""
long_pc.py - long-form built on the 24/7 PC, delivered by GitHub (owner 19. 11.: "ať tam klidně staví nonstop,
ale ať se to tam nikde neukládá").

The PC has a GPU and a home connection; a GitHub run spent 2-4 h per long-form. The PC token can only start
workflows (no push, no releases), so the hand-off is a Netlify draft deploy ("long-inbox"):

  PC:      python tools/long_pc.py pack --since <epoch>   # after kompilace: video (in 90 MB parts), meta,
                                                          # thumbnail inputs and the long-form state ->
                                                          # Netlify draft -> starts long-pc.yml -> deletes
                                                          # everything it built (nothing stays on the PC)
  GitHub:  python tools/long_pc.py unpack <base>          # long-pc.yml: files back into out/_long/<brand>/
                                                          # (meta paths rewritten), state into state_pc/
"""
import argparse
import json
import os
import shutil
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

PART = 90 * 1024 * 1024
STATE = ["long_used.json", "long_episodes.json", "long_reserve.json", "long_vision.json", "long_pool.json",
         "nahled_ai_budget.json"]
TITLE = "long-inbox"


def long_out():
    return Path(os.environ.get("KLIPY_OUT") or HERE / "out") / "_long"


def fresh_notes(since):
    """{brand: newest .meta.json / .failed.json written by this run}."""
    out = {}
    for z in ("bananas", "fanpage"):
        d = long_out() / z
        notes = [p for p in d.glob("*.json") if p.name.endswith((".meta.json", ".failed.json"))
                 and p.stat().st_mtime >= since] if d.exists() else []
        if notes:
            out[z] = max(notes, key=lambda p: p.stat().st_mtime)
    return out


def split(src, dst_dir, rel_base):
    """src -> rel_base.partNN files of PART bytes: {rel: path}."""
    files, i = {}, 0
    with open(src, "rb") as f:
        while True:
            chunk = f.read(PART)
            if not chunk:
                break
            p = dst_dir / f"{Path(rel_base).name}.part{i:02d}"
            p.write_bytes(chunk)
            files[f"{rel_base}.part{i:02d}"] = p
            i += 1
    return files


def dispatch(base):
    """Start long-pc.yml with the deploy URL (the PC's GITHUB_TOKEN may start workflows)."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        for line in (HERE / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("GITHUB_TOKEN="):
                token = line.split("=", 1)[1].strip()
    req = urllib.request.Request(
        "https://api.github.com/repos/alescesnek1/gta6bananaclips/actions/workflows/long-pc.yml/dispatches",
        data=json.dumps({"ref": "main", "inputs": {"base": base}}).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "gta6-pc"})
    urllib.request.urlopen(req, timeout=60).read()


def cmd_pack(a):
    import publikuj as P
    P.load_env()
    notes = fresh_notes(a.since)
    if not notes:
        print("long_pc: nothing new to deliver")
        return 0
    tmp = long_out() / "_pack"
    tmp.mkdir(parents=True, exist_ok=True)
    files, manifest = {}, {"brands": {}, "state": []}
    for z, note in notes.items():
        m = json.loads(note.read_text(encoding="utf-8"))
        entry = {"note": f"{z}/{note.name}", "files": {}}
        files[entry["note"]] = note
        if m.get("video") and Path(m["video"]).exists():
            parts = split(Path(m["video"]), tmp, f"{z}/{Path(m['video']).name}")
            files.update(parts)
            entry["video"] = {"name": Path(m["video"]).name, "parts": sorted(parts)}
        for key in ("thumbnail",):
            if m.get(key) and Path(m[key]).is_file():
                rel = f"{z}/extra/{Path(m[key]).name}"
                files[rel] = Path(m[key])
                entry["files"][key] = rel
        if m.get("nahled_vstup") and Path(m["nahled_vstup"]).is_dir():
            rels = []
            for f in Path(m["nahled_vstup"]).iterdir():
                if f.is_file() and f.stat().st_size < 20e6:
                    rel = f"{z}/nahled/{f.name}"
                    files[rel] = f
                    rels.append(rel)
            entry["files"]["nahled_vstup"] = rels
        manifest["brands"][z] = entry
    env = {"long_used.json": "LONG_USED", "long_episodes.json": "LONG_EPISODES", "long_reserve.json": "LONG_RESERVE",
           "long_vision.json": "LONG_VISION", "long_pool.json": "KLIPY_POOL"}
    for name in STATE:
        src = Path(os.environ.get(env.get(name, ""), "") or HERE / name)      # worker/long.ps1 keeps own copies
        if src.is_file():
            files[f"state/{name}"] = src
            manifest["state"].append(name)
    base = P.inbox_deploy(files, json.dumps(manifest).encode(), TITLE, essential=True)
    print(f"long_pc: delivered {', '.join(notes)} -> {base}")
    if a.no_dispatch:                                  # e.g. a notebook without GITHUB_TOKEN: gh workflow run
        print(f"long_pc: start it with: gh workflow run long-pc.yml -f base={base}")
        return 0
    dispatch(base)
    print("long_pc: long-pc.yml started")
    return 0


def fetch(url, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=600) as r, open(dst, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            return dst
        except OSError:
            if attempt == 3:
                raise
            time.sleep(10 * (attempt + 1))


def cmd_unpack(a):
    base = a.base.rstrip("/")
    with urllib.request.urlopen(f"{base}/inbox.json", timeout=60) as r:
        manifest = json.load(r)
    out = HERE / "out" / "_long"
    for z, e in manifest["brands"].items():
        d = out / z
        note = fetch(f"{base}/{e['note']}", d / Path(e["note"]).name)
        m = json.loads(note.read_text(encoding="utf-8"))
        if e.get("video"):
            video = d / e["video"]["name"]
            with open(video, "wb") as f:
                for rel in e["video"]["parts"]:
                    part = fetch(f"{base}/{rel}", d / "_parts" / Path(rel).name)
                    f.write(part.read_bytes())
                    part.unlink()
            m["video"] = str(video)
        if e["files"].get("thumbnail"):
            m["thumbnail"] = str(fetch(f"{base}/{e['files']['thumbnail']}", d / "extra" / Path(e["files"]["thumbnail"]).name))
        if e["files"].get("nahled_vstup"):
            nd = d / "nahled"
            for rel in e["files"]["nahled_vstup"]:
                fetch(f"{base}/{rel}", nd / Path(rel).name)
            m["nahled_vstup"] = str(nd)
        note.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"long_pc: {z} -> {note}")
    sd = HERE / "state_pc"
    for name in manifest.get("state", []):
        fetch(f"{base}/state/{name}", sd / name)
    print(f"long_pc: state {', '.join(manifest.get('state', []))}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pack")
    p.add_argument("--since", type=float, required=True, help="epoch: only notes written by this run")
    p.add_argument("--no-dispatch", action="store_true", help="only upload, print the URL")
    u = sub.add_parser("unpack")
    u.add_argument("base")
    a = ap.parse_args()
    return cmd_pack(a) if a.cmd == "pack" else cmd_unpack(a)


if __name__ == "__main__":
    sys.exit(main())
