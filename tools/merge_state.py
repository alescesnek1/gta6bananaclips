"""
merge_state.py - merge this run's JSON state files into the newest ones on origin (no git rebase conflicts).

  python tools/merge_state.py OURS_DIR FILE...    # merges OURS_DIR/FILE into ./FILE (origin's copy) in place

Two workflows (denne + the fast live path, long-form) commit state at the same time; a git rebase of two
edits of one JSON file conflicts and the state was lost (18. 11.). Instead each run keeps its own copy,
resets to origin and merges key by key:
  dict  -> union of keys, recursively; a key both changed: ours wins, except numbers where the larger wins
           (failure counters, timestamps)
  list  -> origin's items + ours that origin doesn't have (order kept)
  other -> ours
"""
import json
import sys
from pathlib import Path


def merge(theirs, ours):
    if isinstance(theirs, dict) and isinstance(ours, dict):
        out = dict(theirs)
        for k, v in ours.items():
            out[k] = merge(theirs[k], v) if k in theirs else v
        return out
    if isinstance(theirs, list) and isinstance(ours, list):
        seen = [json.dumps(x, sort_keys=True) for x in theirs]
        return theirs + [x for x in ours if json.dumps(x, sort_keys=True) not in seen]
    if isinstance(theirs, (int, float)) and isinstance(ours, (int, float)) and not isinstance(ours, bool):
        return max(theirs, ours)
    return ours


def main():
    ours_dir, files = Path(sys.argv[1]), sys.argv[2:]
    for f in files:
        mine = ours_dir / f
        if not mine.exists():
            continue
        ours = json.loads(mine.read_text(encoding="utf-8"))
        cur = Path(f)
        theirs = json.loads(cur.read_text(encoding="utf-8")) if cur.exists() else None
        out = ours if theirs is None else merge(theirs, ours)
        cur.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"merged {f}")


if __name__ == "__main__":
    main()
