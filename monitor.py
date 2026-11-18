"""
monitor.py - hourly health / publishing / performance check of the whole pipeline (GitHub Actions, monitor.yml).

  1. health      workflow runs of the last 3 h (denne-klipy, live-klipy, long-form) from the GitHub API:
                 success / failure / cancelled, the last failure's failed step + a short (sanitised) log tail,
                 age of the last successful denne run (alert > 30 min on launch days 18.-21. 11., else > 90 min),
                 whether the live watcher runs.
  2. publishing  publikovano*.json of both brands (both writers): posts in the current publishing window
                 (12:00-11:59 Prague) vs the day's minimum / max (brands.json), last / next post, hot / star / live;
                 alert when a brand posted nothing for > 4 h of the active hours (14:00-05:00) once gameplay exists.
  3. performance Buffer Post.metrics (read-only) of the posts of the last 48 h: top 5 by views, averages per
                 brand and network, views per hour of the newest posts.
  4. Netlify     deploys in the last 24 h, the month's transfer estimate (publikuj.month_gb) vs the budget.

Writes monitor/stav.json (machine-readable) and monitor/stav.md (short Czech report). The repo is public:
no secrets, tokens, e-mail addresses or Buffer ids go into them. E-mail (Gmail SMTP, MAIL_FROM /
MAIL_APP_PASSWORD / MAIL_TO, addresses never printed) only on GitHub Actions: at once for a NEW alert
(the same alert again at most every 6 h while it lasts) and a daily summary at ~09:00 Prague.

Stdlib only (imports publikuj for the record files, the window math and the Buffer GraphQL call).

Usage:
  python monitor.py              # GitHub Actions: write monitor/stav.*, mail when needed
  python monitor.py --zkouska    # local dry run: read-only, prints the report, never writes or mails
  python monitor.py --zkouska --denni   # ... and prints the daily summary mail too
"""

import argparse
import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import publikuj as P

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "monitor"
TZ = P.TZ
NOW = None                                         # tests: fixed "now" (aware datetime)

REPO_DEFAULT = "aleshrani/gta6bananaclips"
WORKFLOWS = {"denne-klipy": "denne.yml", "live-klipy": "live.yml", "long-form": "long.yml"}
LAUNCH_DAYS = (date(2026, 11, 18), date(2026, 11, 21))    # denne must succeed every 30 min (else 90)
STALE_MIN_LAUNCH, STALE_MIN = 30, 90
GAMEPLAY_FROM = datetime(2026, 11, 18, 12, 0, tzinfo=TZ)   # from here a silent brand is a problem
SILENCE_H = 4
LIVE_GAP_MIN = 40                                  # live watcher: runs back to back, a gap > 40 min = down
REPEAT_H = 6                                       # a lasting alert is mailed again at most every 6 h
DAILY_HOUR = 9                                     # daily summary from 09:00 Prague (cron may be late: until 12)
DEPLOY_WARN = P.DEPLOY_SOFT
RECORD_NAMES = ("publikovano.json", "publikovano_live.json", "publikovano_fanpage.json",
                "publikovano_fanpage_live.json")

def now_tz():
    return NOW or datetime.now(TZ)

def log(msg):
    print(msg, flush=True)

# -----------------------------------------------------------------------------
# HTTP: GitHub REST, Netlify REST (Buffer goes through publikuj.gql)

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None

def gh_token():
    t = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if t:
        return t
    try:                                           # local dry run: the logged-in gh CLI (read-only use)
        return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""

def gh(path, raw=False):
    """GET api.github.com<path>. raw=True: plain text, following a redirect WITHOUT the token
    (job logs live on a signed blob URL that must not get our Authorization header)."""
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
         "User-Agent": "gta6-monitor"}
    tok = gh_token()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request("https://api.github.com" + path, headers=h)
    if not raw:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308) or not e.headers.get("Location"):
            raise
        loc = e.headers["Location"]
    with urllib.request.urlopen(urllib.request.Request(loc, headers={"User-Agent": "gta6-monitor"}), timeout=60) as r:
        return r.read().decode("utf-8", "replace")

def netlify_get(path):
    h = {"Authorization": f"Bearer {os.environ['NETLIFY_AUTH_TOKEN']}", "User-Agent": "gta6-monitor"}
    with urllib.request.urlopen(urllib.request.Request(P.NETLIFY + path, headers=h), timeout=60) as r:
        return json.loads(r.read() or b"null")

def buffer_gql(query, key):
    """Buffer GraphQL (read-only) -> (data, errors). Unlike publikuj.gql it keeps partial data: one deleted
    post ("Post not found") must not hide the numbers of the other 19 in the same request."""
    body = json.dumps({"query": query}).encode()
    h = {"Authorization": f"Bearer {os.environ[key]}", "Content-Type": "application/json", "User-Agent": "gta6-monitor"}
    res = json.loads(P.http(P.API, body, h, "POST", 60)[1])
    return res.get("data") or {}, res.get("errors") or []

# -----------------------------------------------------------------------------
# helpers

def ts(s):
    """ISO time (GitHub 'Z', ours with offset) -> aware datetime, None when empty / broken."""
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None

def mins(a, b):
    return round((a - b).total_seconds() / 60)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|[A-Za-z0-9_\-]{32,})\b")

def clean(text, limit=220):
    """A log line safe for the public repo: no ANSI, no e-mail addresses, no token-like strings."""
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", str(text))
    text = re.sub(r"(?i)\bbearer\s+\S+", "Bearer [skryto]", text)
    text = re.sub(r"(?i)\b(token|password|secret)([=: ]+)(?!\[skryto\])\S+", r"\1\2[skryto]", text)
    text = _EMAIL.sub("[e-mail]", text)
    text = _TOKEN.sub("[skryto]", text)
    return text[:limit]

def log_tail(text, n=12):
    """The lines before (and including) the first ##[error] of a job log, else the last n lines."""
    lines = []
    for ln in text.splitlines():
        ln = re.sub(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ?", "", ln.rstrip())
        if ln and not ln.startswith(("##[group]", "##[endgroup]")):
            lines.append(ln)
    err = next((i for i, ln in enumerate(lines) if ln.startswith("##[error]")), None)
    part = lines[max(0, err - n + 1): err + 1] if err is not None else lines[-n:]
    # "##[error]" / "::error::" printed again by the monitor would become annotations of ITS run
    return [clean(re.sub(r"^##\[(\w+)\]", r"[\1] ", x)).replace("::", ": :") for x in part]

def fmt_t(t):
    return t.astimezone(TZ).strftime("%d.%m. %H:%M") if t else "–"

# -----------------------------------------------------------------------------
# 1. health (GitHub Actions)

def health(now, repo):
    since = (now - timedelta(hours=3)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = {"workflows": {}, "posledni_chyba": None}
    failures = []
    for name, wf in WORKFLOWS.items():
        q = urllib.parse.urlencode({"per_page": 100, "created": f">={since}"})
        runs = gh(f"/repos/{repo}/actions/workflows/{wf}/runs?{q}").get("workflow_runs") or []
        c = {"success": 0, "failure": 0, "cancelled": 0, "bezi": 0, "jine": 0}
        for r in runs:
            if r.get("status") != "completed":
                c["bezi"] += 1
            elif r.get("conclusion") in ("success", "failure", "cancelled"):
                c[r["conclusion"]] += 1
            else:
                c["jine"] += 1
            if r.get("conclusion") == "failure":
                failures.append((ts(r.get("updated_at")) or now, name, r))
        out["workflows"][name] = c
    last_ok = gh(f"/repos/{repo}/actions/workflows/denne.yml/runs?per_page=1&status=success").get("workflow_runs") or []
    t_ok = ts(last_ok[0].get("updated_at")) if last_ok else None
    out["denne_posledni_uspech"] = t_ok.isoformat() if t_ok else None
    out["denne_uspech_pred_min"] = mins(now, t_ok) if t_ok else None
    live = gh(f"/repos/{repo}/actions/workflows/live.yml/runs?per_page=5").get("workflow_runs") or []
    out["live_bezi"] = any(r.get("status") in ("in_progress", "queued", "waiting", "pending", "requested")
                           for r in live)
    ended = [ts(r.get("updated_at")) for r in live if r.get("status") == "completed"]
    ended = [t for t in ended if t]
    out["live_konec_pred_min"] = mins(now, max(ended)) if ended else None
    if failures:
        t, name, r = max(failures, key=lambda x: x[0])
        info = {"workflow": name, "cas": t.isoformat(), "krok": None, "log": []}
        try:
            jobs = gh(f"/repos/{repo}/actions/runs/{r['id']}/jobs").get("jobs") or []
            job = next((j for j in jobs if j.get("conclusion") == "failure"), None)
            if job:
                step = next((s for s in job.get("steps") or [] if s.get("conclusion") == "failure"), None)
                info["krok"] = clean(step["name"], 120) if step else clean(job.get("name"), 120)
                info["log"] = log_tail(gh(f"/repos/{repo}/actions/jobs/{job['id']}/logs", raw=True))
        except Exception as e:                    # the counts still stand without the details
            info["log"] = [f"(log nedostupný: {type(e).__name__})"]
        out["posledni_chyba"] = info
    return out

def is_launch(now):
    return LAUNCH_DAYS[0] <= now.astimezone(TZ).date() <= LAUNCH_DAYS[1]

def health_alerts(h, now, live_from):
    al = []
    limit = STALE_MIN_LAUNCH if is_launch(now) else STALE_MIN
    age = h.get("denne_uspech_pred_min")
    if age is None or age > limit:
        al.append(("denne_stary", f"denne-klipy: poslední úspěšný běh před {age if age is not None else '?'} min "
                                  f"(limit {limit} min)"))
    for name, c in h["workflows"].items():
        if c["failure"]:
            al.append((f"selhani:{name}", f"{name}: {c['failure']}× selhal za 3 h"))
    gap = h.get("live_konec_pred_min")
    if now >= live_from and not h.get("live_bezi") and (gap is None or gap > LIVE_GAP_MIN):
        al.append(("live_nebezi", f"live-klipy neběží (poslední skončil před {gap if gap is not None else '?'} min)"))
    return al

# -----------------------------------------------------------------------------
# 2. publishing (record files)

def brand_names():
    return [k for k, v in P.brands_cfg().items() if isinstance(v, dict)]

def publishing(now):
    P.NOW = now
    day = P.pub_day(now)
    stars = set(P.load_json(HERE / "sources.json", {}).get("star_streamers") or [])
    out = {"okno": f"{P.window_start(day):%d.%m. %H:%M} – {P.window_start(day) + timedelta(days=1):%d.%m. %H:%M}",
           "den": day.isoformat(), "znacky": {}}
    for b in brand_names():
        P.set_brand(b)
        recs = P.records()
        today = {k: r for k, r in recs.items() if P.in_day(r, day)}
        quota = {k: r for k, r in today.items() if not k.startswith(("top3_", "exp_"))}
        dues = [(ts(r.get("due")), r) for r in recs.values() if not r.get("draft") and not r.get("deleted")]
        past = [t for t, _ in dues if t and t <= now]
        future = [t for t, _ in dues if t and t > now]
        out["znacky"][b] = {
            "postu": len(quota), "navic": len(today) - len(quota),
            "minimum": P.daily_quota(day), "maximum": P.max_per_day(day),
            "hot": sum(bool(r.get("hot")) for r in quota.values()),
            "star": sum((r.get("channel") or "") in stars for r in quota.values()),
            "live": sum(bool(r.get("live")) for r in quota.values()),
            "drafty": sum(bool(r.get("draft")) for r in today.values()),
            "posledni": max(past).isoformat() if past else None,
            "dalsi": min(future).isoformat() if future else None,
        }
    return out

def active_start(now):
    """Start of the current active stretch (14:00 of the publishing day) or None outside 14:00-05:00."""
    loc = now.astimezone(TZ)
    if 5 <= loc.hour < 14:
        return None
    return P.window_start(P.pub_day(now)) + timedelta(hours=2)

def publishing_alerts(pub, now):
    al = []
    start = active_start(now)
    if start is None or now < GAMEPLAY_FROM:
        return al
    for b, s in pub["znacky"].items():
        if s["minimum"] <= 0:
            continue
        last = ts(s["posledni"])
        since = max(last, start) if last else start
        if now - since > timedelta(hours=SILENCE_H):
            al.append((f"ticho:{b}", f"{b}: nic nepublikováno {SILENCE_H}+ h (poslední post {fmt_t(last)})"))
    return al

# -----------------------------------------------------------------------------
# 3. performance (Buffer Post.metrics, read-only)

def norm_metrics(ms):
    """[{type, value}] -> views / likes / comments / shares / saves (whatever the network reports)."""
    d = {m.get("type"): m.get("value") or 0 for m in ms or []}
    return {"views": d.get("views") or d.get("impressions") or d.get("reach") or 0,
            "likes": d.get("likes") or d.get("reactions") or 0,
            "comments": d.get("comments") or 0,
            "shares": d.get("shares") or d.get("reposts") or 0,
            "saves": d.get("saves") or 0}

def performance(now, hours=48):
    items = []                                     # (brand, buffer key, clip, record, service, post id)
    for b in brand_names():
        P.set_brand(b)
        chans = {c["id"]: c for c in P.load_json(P.CHANNELS, [])}
        for clip, rec in P.records().items():
            t = ts(rec.get("due"))
            if rec.get("draft") or rec.get("deleted") or not t or not (now - timedelta(hours=hours) <= t <= now):
                continue
            for cid, pid in (rec.get("posts") or {}).items():
                ch = chans.get(cid) or {}
                items.append((b, ch.get("key") or "BUFFER_API_KEY_1", clip, rec, (ch.get("service") or "?").lower(), pid))
    posts, missing, errors, gone = [], set(), [], 0
    by_key = {}
    for it in items:
        by_key.setdefault(it[1], []).append(it)
    for key, its in by_key.items():
        if not os.environ.get(key):
            missing.add(key)
            continue
        for i in range(0, len(its), 20):
            chunk = its[i:i + 20]
            q = "query { " + " ".join(f"p{j}: post(input: {{id: {json.dumps(it[5])}}}) {{ status metrics {{ type value }} }}"
                                      for j, it in enumerate(chunk)) + " }"
            try:
                data, errs = buffer_gql(q, key)
            except Exception as e:
                errors.append(clean(f"{key}: {type(e).__name__} {str(e)[-120:]}"))
                continue
            for e in errs:
                if "not found" in str(e.get("message", "")).lower():
                    gone += 1                      # deleted in Buffer: counted, not an error of the monitor
                elif len(errors) < 5:
                    errors.append(clean(f"{key}: {str(e.get('message'))[:120]}"))
            for j, (b, _, clip, rec, service, _) in enumerate(chunk):
                p = data.get(f"p{j}") or {}
                if p.get("status") != "sent":
                    continue
                t = ts(rec.get("due"))
                posts.append({"znacka": b, "klip": str(clip)[:80], "streamer": str(rec.get("channel") or "?")[:40],
                              "sit": service, "hot": bool(rec.get("hot")), "live": bool(rec.get("live")),
                              "due": t.isoformat(), "stari_h": round((now - t).total_seconds() / 3600, 1),
                              **norm_metrics(p.get("metrics"))})
    return summarize(posts, now) | {"chybi_klice": len(missing), "chyby": errors[:5], "postu_celkem": len(items),
                                      "nenalezeno": gone}

def summarize(posts, now):
    top = sorted(posts, key=lambda p: -p["views"])[:5]
    avg = {}
    for p in posts:
        a = avg.setdefault(p["znacka"], {}).setdefault(p["sit"], {"n": 0, "views": 0, "likes": 0})
        a["n"] += 1
        a["views"] += p["views"]
        a["likes"] += p["likes"]
    for nets in avg.values():
        for a in nets.values():
            a["views"] = round(a["views"] / a["n"])
            a["likes"] = round(a["likes"] / a["n"])
    clips = {}
    for p in posts:
        c = clips.setdefault((p["znacka"], p["klip"]), {"znacka": p["znacka"], "klip": p["klip"], "streamer": p["streamer"],
                                                          "stari_h": p["stari_h"], "views": 0})
        c["views"] += p["views"]
    newest = sorted(clips.values(), key=lambda c: c["stari_h"])[:5]
    for c in newest:
        c["views_za_h"] = round(c["views"] / max(c["stari_h"], 0.5))
    return {"zmereno": len(posts), "top5": top, "prumer": avg, "nejnovejsi": newest, "posty": posts}

# -----------------------------------------------------------------------------
# 4. Netlify

def netlify_state(now):
    out = {"gb_mesic": round(P.month_gb(), 1)}
    cfg = P.brands_cfg()
    out["gb_soft"] = cfg.get("netlify_gb_soft")
    out["gb_hard"] = cfg.get("netlify_gb_hard")
    out["gb_rozpocet"] = P.load_json(HERE / "sources.json", {}).get("netlify_gb_month")
    site = os.environ.get("NETLIFY_SITE_ID")
    if os.environ.get("NETLIFY_AUTH_TOKEN") and site:
        ds = netlify_get(f"/sites/{site}/deploys?per_page=100") or []
        cut = now - timedelta(hours=24)
        out["deploye_24h"] = sum(1 for d in ds if (ts(d.get("created_at")) or cut - timedelta(1)) >= cut)
    else:
        out["deploye_24h"] = None
    return out

def netlify_alerts(n):
    al = []
    if n.get("deploye_24h") is not None and n["deploye_24h"] >= DEPLOY_WARN:
        al.append(("netlify_deploye", f"Netlify: {n['deploye_24h']} deployů za 24 h (měkký strop {DEPLOY_WARN})"))
    if n.get("gb_soft") and n["gb_mesic"] >= n["gb_soft"]:
        al.append(("netlify_gb", f"Netlify: odhad {n['gb_mesic']} GB tento měsíc (soft {n['gb_soft']}, hard {n['gb_hard']})"))
    return al

# -----------------------------------------------------------------------------
# alert dedupe + mail decisions

def plan_mail(alerts, prev, now):
    """alerts [(key, text)], prev = last alert state {key: {od, poslano}} -> (new state, keys to mail now).
    A key is mailed when it is new or its last mail is >= REPEAT_H old; keys that vanished are dropped."""
    state, due = {}, []
    for key, _ in alerts:
        p = dict(prev.get(key) or {})
        p.setdefault("od", now.isoformat())
        sent = ts(p.get("poslano"))
        if not sent or now - sent >= timedelta(hours=REPEAT_H):
            due.append(key)
        state[key] = p
    return state, due

def daily_due(prev_day, now):
    loc = now.astimezone(TZ)
    return DAILY_HOUR <= loc.hour < 12 and prev_day != loc.date().isoformat()

def yesterday(perf, now):
    d = (now.astimezone(TZ) - timedelta(days=1)).date()
    ps = [p for p in perf.get("posty") or [] if ts(p["due"]).astimezone(TZ).date() == d]
    out = {"den": d.isoformat(), "postu": len(ps), "znacky": {}}
    for p in ps:
        z = out["znacky"].setdefault(p["znacka"], {"postu": 0, "views": 0, "likes": 0, "comments": 0, "shares": 0})
        for k in ("views", "likes", "comments", "shares"):
            z[k] += p[k]
        z["postu"] += 1
    out["top5"] = sorted(ps, key=lambda p: -p["views"])[:5]
    return out

# -----------------------------------------------------------------------------
# report

def n(x):
    return f"{x:,.0f}".replace(",", " ") if isinstance(x, (int, float)) else "–"

def report_md(st):
    h, pub, perf, net = st.get("zdravi"), st.get("publikace"), st.get("vykon"), st.get("netlify")
    L = [f"# Monitor GTA6 klipy – {fmt_t(ts(st['cas']))} (Praha)", ""]
    al = st.get("alerty") or []
    L += ["## Problémy", *([f"- {a['text']}" for a in al] or ["- žádné"]), ""]
    L.append("## Běhy (3 h)")
    if h:
        for name, c in h["workflows"].items():
            L.append(f"- {name}: ✔ {c['success']}  ✖ {c['failure']}  ⊘ {c['cancelled']}  běží {c['bezi']}")
        L.append(f"- denne: poslední úspěch před {h.get('denne_uspech_pred_min', '–')} min; "
                 f"live hlídač {'běží' if h.get('live_bezi') else 'NEBĚŽÍ'}")
        e = h.get("posledni_chyba")
        if e:
            L.append(f"- poslední chyba: {e['workflow']} {fmt_t(ts(e['cas']))}, krok „{e.get('krok') or '?'}“")
            if e.get("log"):
                L += ["", "```", *e["log"][-6:], "```"]
    else:
        L.append("- nedostupné")
    L += ["", f"## Publikace (okno {pub['okno']})" if pub else "## Publikace"]
    for b, s in (pub or {}).get("znacky", {}).items():
        L.append(f"- {b}: {s['postu']}/{s['minimum']}–{s['maximum']} (+{s['navic']} navíc), hot {s['hot']}, "
                 f"hvězdy {s['star']}, live {s['live']}; poslední {fmt_t(ts(s['posledni']))}, další {fmt_t(ts(s['dalsi']))}"
                 + (f", drafty {s['drafty']}" if s["drafty"] else ""))
    L += ["", "## Výkon (Buffer, 48 h)"]
    if perf and perf.get("zmereno"):
        L.append(f"- změřeno {perf['zmereno']} z {perf['postu_celkem']} postů"
                 + (f", v Bufferu nenalezeno {perf['nenalezeno']}" if perf.get("nenalezeno") else ""))
        L.append("- top 5:")
        for p in perf["top5"]:
            L.append(f"  - {n(p['views'])} zhl. · {p['streamer']} · {p['sit']} · {p['znacka']} · {p['stari_h']} h · {p['klip']}")
        for b, nets in perf["prumer"].items():
            L.append(f"- průměr {b}: " + ", ".join(f"{s} {n(a['views'])} ({a['n']})" for s, a in sorted(nets.items())))
        L.append("- nejnovější (zhl./h): " + ", ".join(f"{c['streamer']} {n(c['views_za_h'])}/h ({c['stari_h']} h)"
                                                    for c in perf["nejnovejsi"]))
    else:
        L.append("- zatím žádná čísla" + (f" ({perf['postu_celkem']} postů, v Bufferu nenalezeno {perf['nenalezeno']})"
                                          if perf and perf.get("nenalezeno") else "")
                 + (f" ({'; '.join(perf['chyby'])})" if perf and perf.get("chyby") else ""))
    if net:
        L += ["", "## Netlify",
              f"- deploye 24 h: {net['deploye_24h'] if net['deploye_24h'] is not None else '–'}; "
              f"měsíc {net['gb_mesic']} GB (soft {net['gb_soft']}, hard {net['gb_hard']}, rozpočet {net['gb_rozpocet']})"]
    if st.get("chyby"):
        L += ["", "## Monitor nedostal data", *[f"- {e}" for e in st["chyby"]]]
    return "\n".join(L) + "\n"

def daily_text(y, md):
    L = [f"Denní přehled za {y['den']}: {y['postu']} postů (součet přes sítě).", ""]
    for b, z in y["znacky"].items():
        L.append(f"{b}: {z['postu']} postů, {n(z['views'])} zhlédnutí, {n(z['likes'])} lajků, "
                 f"{n(z['comments'])} komentářů, {n(z['shares'])} sdílení")
    if y["top5"]:
        L += ["", "Top posty:"]
        L += [f"- {n(p['views'])} zhl. · {p['streamer']} · {p['sit']} · {p['znacka']} · {p['klip']}" for p in y["top5"]]
    return "\n".join(L) + "\n\n---\n\n" + md

# -----------------------------------------------------------------------------
# mail (never locally, never print the addresses)

def send_mail(subject, body):
    keys = [os.environ.get(k) for k in ("MAIL_FROM", "MAIL_APP_PASSWORD", "MAIL_TO")]
    if os.environ.get("GITHUB_ACTIONS") != "true":
        log(f"(lokálně se e-mail neposílá) {subject}")
        return False
    if not all(keys):
        log(f"::warning::MAIL_* chybí, e-mail se neodeslal: {subject}")
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, keys[0], keys[2]
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=60) as s:
            s.login(keys[0], keys[1])
            s.send_message(msg)
        log(f"e-mail odeslán: {subject}")
        return True
    except Exception as e:
        log(f"::warning::E-mail se neodeslal ({type(e).__name__}).")
        return False

# -----------------------------------------------------------------------------

def collect(now):
    st = {"cas": now.isoformat(timespec="seconds"), "chyby": []}
    repo = os.environ.get("GITHUB_REPOSITORY") or REPO_DEFAULT
    live_from = ts(P.load_json(HERE / "sources.json", {}).get("live_from")) or GAMEPLAY_FROM
    alerts = []
    steps = [("zdravi", lambda: health(now, repo), lambda v: health_alerts(v, now, live_from)),
             ("publikace", lambda: publishing(now), lambda v: publishing_alerts(v, now)),
             ("vykon", lambda: performance(now), lambda v: []),
             ("netlify", lambda: netlify_state(now), netlify_alerts)]
    for name, get, judge in steps:
        try:
            st[name] = get()
            alerts += judge(st[name])
        except Exception as e:                    # one source down must not hide the others
            st[name] = None
            st["chyby"].append(clean(f"{name}: {type(e).__name__} {str(e)[-150:]}"))
    st["alerty"] = [{"klic": k, "text": t} for k, t in alerts]
    return st

def run(zkouska=False, denni=False, now=None):
    now = now or now_tz()
    prev = P.load_json(OUT_DIR / "stav.json", {})
    st = collect(now)
    alerts = [(a["klic"], a["text"]) for a in st["alerty"]]
    state, due = plan_mail(alerts, prev.get("alerty_stav") or {}, now)
    st["denni_odeslano"] = prev.get("denni_odeslano")
    md = report_md(st)
    sent_alert = sent_daily = False
    if due:
        first = next(t for k, t in alerts if k in due)
        subj = f"[GTA6 klipy] PROBLÉM: {first[:110]}" + (f" (+{len(alerts) - 1})" if len(alerts) > 1 else "")
        body = "Nové nebo trvající problémy:\n" + "\n".join(f"- {t}" for _, t in alerts) + "\n\n---\n\n" + md
        if zkouska:
            log(f"(zkouška) e-mail by šel: {subj}")
        elif send_mail(subj, body):
            sent_alert = True
            for k in due:
                state[k]["poslano"] = now.isoformat(timespec="seconds")
    if denni or daily_due(st["denni_odeslano"], now):
        y = yesterday(st.get("vykon") or {}, now)
        subj = f"[GTA6 klipy] denní přehled {date.fromisoformat(y['den']):%d.%m.}"
        body = daily_text(y, md)
        if zkouska:
            log(f"(zkouška) e-mail by šel: {subj}\n{body}")
        elif send_mail(subj, body):
            sent_daily = True
            st["denni_odeslano"] = now.astimezone(TZ).date().isoformat()
    st["alerty_stav"] = state
    st["podpis"] = "|".join(sorted(state))
    if st.get("vykon"):
        st["vykon"] = {k: v for k, v in st["vykon"].items() if k != "posty"}   # the list only feeds the daily mail
    if zkouska:
        log(md)
        return st, md
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "stav.json").write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT_DIR / "stav.md").write_text(md, encoding="utf-8")
    log(md)
    log(f"alerty {len(alerts)}, e-mail problém {'ano' if sent_alert else 'ne'}, denní {'ano' if sent_daily else 'ne'}")
    return st, md

def main():
    p = argparse.ArgumentParser(description="hourly monitor: runs, publishing, performance, Netlify")
    p.add_argument("--zkouska", action="store_true", help="read-only: print the report, no files, no e-mail")
    p.add_argument("--denni", action="store_true", help="build the daily summary now (with --zkouska: print it)")
    a = p.parse_args()
    if a.zkouska:
        P.load_env()                              # local: Buffer / Netlify keys from .env (read-only calls)
    run(zkouska=a.zkouska, denni=a.denni)
    return 0

if __name__ == "__main__":
    sys.exit(main())
