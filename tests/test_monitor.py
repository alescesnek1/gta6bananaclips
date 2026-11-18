"""
monitor.py: alert logic, dedupe, publishing window math, report without secrets. GitHub / Buffer / Netlify
are mocked, nothing is sent, the real record files are never touched (temp dir).
  .venv/Scripts/python -m unittest tests.test_monitor
"""
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import monitor as M                                        # noqa: E402
import publikuj as P                                       # noqa: E402

TZ = P.TZ
SECRETS = {"BUFFER_API_KEY_1": "bufsecret111111111111111111111111111111", "BUFFER_API_KEY_3": "bufsecret3333",
           "NETLIFY_AUTH_TOKEN": "nfp_netlifysecret9999999999999999999999", "NETLIFY_SITE_ID": "site-abc",
           "MAIL_FROM": "robot@example.com", "MAIL_APP_PASSWORD": "mailpass123", "MAIL_TO": "ales@example.com",
           "GITHUB_TOKEN": "ghs_githubsecret1234567890abcdef"}


def at(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TZ)


def iso_z(t):
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Env(unittest.TestCase):
    """Temp project dir: brands.json, sources.json, record files, Buffer channels."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        brands = {"_napoveda": "x", "netlify_gb_soft": 85, "netlify_gb_hard": 100,
                  "bananas": {"buffer_keys": ["BUFFER_API_KEY_1"], "channels": "buffer.json",
                              "published": "publikovano.json", "ramp": {"2026-11-18": 8}, "max_per_day": 10},
                  "fanpage": {"buffer_keys": ["BUFFER_API_KEY_3"], "channels": "buffer_fanpage.json",
                              "published": "publikovano_fanpage.json", "ramp": {"2026-11-18": 8}, "max_per_day": 10}}
        self.w("brands.json", brands)
        self.w("sources.json", {"netlify_gb_month": 100, "live_from": "2026-11-17T23:00:00Z",
                                "star_streamers": ["kaicenat"]})
        self.w("buffer.json", [{"id": "chIG", "service": "instagram", "key": "BUFFER_API_KEY_1"},
                               {"id": "chYT", "service": "youtube", "key": "BUFFER_API_KEY_1"}])
        self.w("buffer_fanpage.json", [{"id": "chTT", "service": "tiktok", "key": "BUFFER_API_KEY_3"}])
        self.patches = [mock.patch.object(P, "HERE", self.d), mock.patch.object(P, "BRANDS", self.d / "brands.json"),
                        mock.patch.object(P, "EXTRA_MB", self.d / "netlify_extra.json"),
                        mock.patch.object(M, "HERE", self.d), mock.patch.object(M, "OUT_DIR", self.d / "monitor"),
                        mock.patch.dict(os.environ, SECRETS)]
        for p in self.patches:
            p.start()
        os.environ.pop("GITHUB_ACTIONS", None)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        P.NOW = None
        P.set_brand("bananas")
        self.tmp.cleanup()

    def w(self, name, data):
        (self.d / name).write_text(json.dumps(data), encoding="utf-8")

    def rec(self, due, posts=None, **kw):
        return {"due": due.isoformat(), "draft": False, "posts": posts or {"chIG": "p1"}, "channel": "kaicenat", **kw}


class Window(Env):
    def test_window_and_counts(self):
        # 01:30 on 19. 11. still belongs to the 18. 11. window (12:00 -> 12:00)
        now = at(2026, 11, 19, 1, 30)
        self.w("publikovano.json", {"a": self.rec(at(2026, 11, 18, 14, 3), hot=True),
                                    "b": self.rec(at(2026, 11, 18, 11, 50)),          # previous window
                                    "top3_x": self.rec(at(2026, 11, 18, 23, 0)),       # extra, not quota
                                    "c": self.rec(at(2026, 11, 19, 3, 0)),             # future
                                    "del": self.rec(at(2026, 11, 19, 0, 0), deleted=True)})  # deleted by hand
        self.w("publikovano_live.json", {"l": self.rec(at(2026, 11, 18, 22, 10), live=True)})
        pub = M.publishing(now)
        b = pub["znacky"]["bananas"]
        self.assertEqual(pub["den"], "2026-11-18")
        self.assertEqual((b["postu"], b["navic"], b["minimum"], b["maximum"]), (3, 1, 8, 10))
        self.assertEqual((b["hot"], b["live"], b["star"]), (1, 1, 3))
        self.assertEqual(M.ts(b["posledni"]), at(2026, 11, 18, 23, 0))
        self.assertEqual(M.ts(b["dalsi"]), at(2026, 11, 19, 3, 0))
        self.assertEqual(pub["znacky"]["fanpage"]["postu"], 0)

    def test_active_start(self):
        self.assertIsNone(M.active_start(at(2026, 11, 19, 9, 0)))
        self.assertEqual(M.active_start(at(2026, 11, 19, 2, 0)), at(2026, 11, 18, 14, 0))
        self.assertEqual(M.active_start(at(2026, 11, 18, 20, 0)), at(2026, 11, 18, 14, 0))


class PublishingAlerts(Env):
    def pub(self, now, last):
        self.w("publikovano.json", {"a": self.rec(last)} if last else {})
        self.w("publikovano_fanpage.json", {"f": self.rec(now - timedelta(minutes=10), {"chTT": "x"})})
        return M.publishing(now)

    def test_silence_counts_only_active_hours(self):
        now = at(2026, 11, 18, 17, 0)                 # 3 h after 14:00, last post in the morning
        self.assertEqual(M.publishing_alerts(self.pub(now, at(2026, 11, 18, 9, 0)), now), [])
        now = at(2026, 11, 18, 18, 30)                # 4.5 h of the active stretch without a post
        al = M.publishing_alerts(self.pub(now, at(2026, 11, 18, 9, 0)), now)
        self.assertEqual([k for k, _ in al], ["ticho:bananas"])

    def test_silence_overnight_and_quiet_morning(self):
        now = at(2026, 11, 19, 3, 0)
        al = M.publishing_alerts(self.pub(now, at(2026, 11, 18, 22, 30)), now)
        self.assertEqual([k for k, _ in al], ["ticho:bananas"])
        now = at(2026, 11, 19, 9, 0)                  # 05:00-14:00 is never an alert
        self.assertEqual(M.publishing_alerts(self.pub(now, at(2026, 11, 18, 22, 30)), now), [])

    def test_no_alert_before_gameplay(self):
        now = at(2026, 11, 18, 3, 0)                  # before 18. 11. 12:00
        self.assertEqual(M.publishing_alerts(self.pub(now, None), now), [])


class HealthAlerts(Env):
    def h(self, age, fails=0, live=True, gap=5):
        return {"workflows": {"denne-klipy": {"success": 3, "failure": fails, "cancelled": 0, "bezi": 0, "jine": 0}},
                "denne_uspech_pred_min": age, "live_bezi": live, "live_konec_pred_min": gap}

    def test_stale_limits(self):
        lf = at(2026, 11, 17, 0, 0)
        launch, normal = at(2026, 11, 19, 20, 0), at(2026, 11, 23, 20, 0)
        keys = lambda h, now: [k for k, _ in M.health_alerts(h, now, lf)]
        self.assertEqual(keys(self.h(25), launch), [])
        self.assertEqual(keys(self.h(35), launch), ["denne_stary"])
        self.assertEqual(keys(self.h(35), normal), [])
        self.assertEqual(keys(self.h(95), normal), ["denne_stary"])
        self.assertEqual(keys(self.h(None), normal), ["denne_stary"])

    def test_failures_and_live(self):
        lf, now = at(2026, 11, 17, 0, 0), at(2026, 11, 19, 20, 0)
        self.assertEqual([k for k, _ in M.health_alerts(self.h(5, fails=2), now, lf)], ["selhani:denne-klipy"])
        self.assertEqual([k for k, _ in M.health_alerts(self.h(5, live=False, gap=15), now, lf)], [])
        self.assertEqual([k for k, _ in M.health_alerts(self.h(5, live=False, gap=60), now, lf)], ["live_nebezi"])
        self.assertEqual(M.health_alerts(self.h(5, live=False, gap=60), now, at(2026, 11, 20, 0, 0)), [])

    def test_health_from_github(self):
        now = at(2026, 11, 19, 20, 0)
        runs = {"denne.yml": [{"id": 1, "status": "completed", "conclusion": "success", "updated_at": iso_z(now)},
                              {"id": 2, "status": "completed", "conclusion": "failure",
                               "updated_at": iso_z(now - timedelta(minutes=20))},
                              {"id": 3, "status": "completed", "conclusion": "cancelled", "updated_at": iso_z(now)},
                              {"id": 4, "status": "in_progress", "conclusion": None, "updated_at": iso_z(now)}],
                "live.yml": [{"id": 9, "status": "in_progress", "updated_at": iso_z(now)}], "long.yml": []}
        log = ("2026-11-19T19:39:00.1Z ##[group]Run git push\n2026-11-19T19:39:01.1Z pushing with token "
               "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123 for robot@example.com\n"
               "2026-11-19T19:39:02.1Z ##[error]stav se nepodarilo pushnout\n2026-11-19T19:39:03.1Z Post job cleanup.\n")

        def fake_gh(path, raw=False):
            if raw:
                return log
            if "/jobs" in path:
                return {"jobs": [{"id": 77, "name": "run", "conclusion": "failure",
                                  "steps": [{"name": "render", "conclusion": "success"},
                                            {"name": "save state", "conclusion": "failure"}]}]}
            if "status=success" in path:
                return {"workflow_runs": [{"updated_at": iso_z(now - timedelta(minutes=12))}]}
            wf = path.split("/workflows/")[1].split("/")[0]
            return {"workflow_runs": runs[wf]}
        with mock.patch.object(M, "gh", fake_gh):
            h = M.health(now, "o/r")
        self.assertEqual(h["workflows"]["denne-klipy"], {"success": 1, "failure": 1, "cancelled": 1, "bezi": 1, "jine": 0})
        self.assertEqual(h["denne_uspech_pred_min"], 12)
        self.assertTrue(h["live_bezi"])
        e = h["posledni_chyba"]
        self.assertEqual(e["krok"], "save state")
        self.assertEqual(e["log"][-1], "[error] stav se nepodarilo pushnout")   # no annotation in our log
        joined = "\n".join(e["log"])
        self.assertNotIn("ghp_", joined)
        self.assertNotIn("robot@example.com", joined)
        self.assertNotIn("Post job cleanup", joined)


class Dedupe(unittest.TestCase):
    def test_new_repeat_and_clear(self):
        t0 = at(2026, 11, 19, 20, 7)
        state, due = M.plan_mail([("a", "A")], {}, t0)
        self.assertEqual(due, ["a"])
        state["a"]["poslano"] = t0.isoformat()
        state, due = M.plan_mail([("a", "A")], state, t0 + timedelta(hours=1))
        self.assertEqual(due, [])                                     # same alert next hour: no mail
        state, due = M.plan_mail([("a", "A"), ("b", "B")], state, t0 + timedelta(hours=2))
        self.assertEqual(due, ["b"])                                  # a new one: mail
        state["b"]["poslano"] = (t0 + timedelta(hours=2)).isoformat()
        state, due = M.plan_mail([("a", "A"), ("b", "B")], state, t0 + timedelta(hours=6))
        self.assertEqual(due, ["a"])                                  # a repeats after 6 h
        self.assertEqual(state["a"]["od"], t0.isoformat())
        state, due = M.plan_mail([], state, t0 + timedelta(hours=7))
        self.assertEqual((state, due), ({}, []))                      # resolved: forgotten
        _, due = M.plan_mail([("a", "A")], state, t0 + timedelta(hours=8))
        self.assertEqual(due, ["a"])                                  # comes back: new again

    def test_daily_due(self):
        self.assertTrue(M.daily_due(None, at(2026, 11, 19, 9, 7)))
        self.assertFalse(M.daily_due("2026-11-19", at(2026, 11, 19, 10, 7)))
        self.assertTrue(M.daily_due("2026-11-18", at(2026, 11, 19, 11, 7)))
        self.assertFalse(M.daily_due(None, at(2026, 11, 19, 8, 7)))
        self.assertFalse(M.daily_due(None, at(2026, 11, 19, 12, 7)))


class Performance(Env):
    def test_metrics_partial_and_top(self):
        now = at(2026, 11, 19, 20, 0)
        self.w("publikovano.json", {"a": self.rec(now - timedelta(hours=5), {"chIG": "p1", "chYT": "p2"}),
                                    "b": self.rec(now - timedelta(hours=1), {"chIG": "p3"}),
                                    "old": self.rec(now - timedelta(hours=60), {"chIG": "p9"}),
                                    "d": self.rec(now - timedelta(hours=2), {"chIG": "p4"}, draft=True)})
        self.w("publikovano_fanpage.json", {"f": self.rec(now - timedelta(hours=3), {"chTT": "p5"})})
        calls = []

        def fake(q, key):
            calls.append((key, q))
            if key == "BUFFER_API_KEY_3":
                return {"p0": None}, [{"message": "Post not found for id: p5", "path": ["p0"]}]
            vals = {"p1": 1000, "p2": 50, "p3": 400}
            data = {}
            for alias, pid in re.findall(r'(p\d+): post\(input: \{id: "(\w+)"\}\)', q):
                if pid in vals:
                    data[alias] = {"status": "sent", "metrics": [{"type": "views", "value": vals[pid]},
                                                                 {"type": "reactions", "value": 7}]}
            return data, []
        with mock.patch.object(M, "buffer_gql", fake):
            r = M.performance(now)
        self.assertNotIn("p9", "".join(q for _, q in calls))           # older than 48 h
        self.assertNotIn("p4", "".join(q for _, q in calls))           # draft
        self.assertEqual(r["zmereno"], 3)
        self.assertEqual(r["nenalezeno"], 1)
        self.assertEqual([p["views"] for p in r["top5"]], [1000, 400, 50])
        self.assertEqual(r["top5"][0]["likes"], 7)
        self.assertEqual(r["prumer"]["bananas"]["instagram"], {"n": 2, "views": 700, "likes": 7})
        self.assertEqual(r["nejnovejsi"][0]["klip"], "b")
        self.assertEqual(r["nejnovejsi"][0]["views_za_h"], 400)
        y = M.yesterday({"posty": r["posty"]}, at(2026, 11, 20, 9, 7))
        self.assertEqual((y["den"], y["postu"]), ("2026-11-19", 3))
        self.assertEqual(y["znacky"]["bananas"]["views"], 1450)


class RunEndToEnd(Env):
    def fakes(self, now, failures=0):
        def fake_gh(path, raw=False):
            if raw:
                return "##[error]boom with bufsecret111111111111111111111111111111\n"
            if "/jobs" in path:
                return {"jobs": [{"id": 1, "conclusion": "failure", "steps": [{"name": "publish", "conclusion": "failure"}]}]}
            if "status=success" in path:
                return {"workflow_runs": [{"updated_at": iso_z(now - timedelta(minutes=5))}]}
            if "live.yml" in path:
                return {"workflow_runs": [{"status": "in_progress", "updated_at": iso_z(now)}]}
            return {"workflow_runs": [{"id": 5, "status": "completed", "conclusion": "failure",
                                       "updated_at": iso_z(now)}] * failures}
        return [mock.patch.object(M, "gh", fake_gh),
                mock.patch.object(M, "buffer_gql", lambda q, k: ({"p0": {"status": "sent", "metrics": [
                    {"type": "views", "value": 10}]}}, [])),
                mock.patch.object(M, "netlify_get", lambda path: [{"created_at": iso_z(now)}] * 3)]

    def go(self, now, failures=0, **kw):
        with contextlib.ExitStack() as st:
            for p in self.fakes(now, failures):
                st.enter_context(p)
            sent = st.enter_context(mock.patch.object(M, "send_mail", return_value=True))
            with contextlib.redirect_stdout(io.StringIO()):
                M.run(now=now, **kw)
        return sent

    def test_mail_once_then_repeat_and_no_secrets(self):
        now = at(2026, 11, 19, 20, 7)
        self.w("publikovano.json", {"a": self.rec(now - timedelta(hours=1))})
        sent = self.go(now, failures=1)
        subjects = [c.args[0] for c in sent.call_args_list]
        self.assertEqual(len(subjects), 1)
        self.assertTrue(subjects[0].startswith("[GTA6 klipy] PROBLÉM: denne-klipy"))
        st = json.loads((self.d / "monitor" / "stav.json").read_text(encoding="utf-8"))
        self.assertIn("selhani:denne-klipy", st["podpis"])
        self.assertEqual(self.go(now + timedelta(hours=1), failures=1).call_count, 0)      # dedupe
        self.assertEqual(self.go(now + timedelta(hours=6), failures=1).call_count, 1)      # repeat
        out = (self.d / "monitor" / "stav.json").read_text(encoding="utf-8") + \
            (self.d / "monitor" / "stav.md").read_text(encoding="utf-8")
        for v in SECRETS.values():
            if len(v) > 8:
                self.assertNotIn(v, out)
        self.assertNotIn("@example.com", out)
        self.assertNotIn('"p0"', out)
        self.assertNotIn("chIG", out)                                                       # no Buffer ids

    def test_daily_once(self):
        now = at(2026, 11, 20, 9, 7)
        self.w("publikovano.json", {"a": self.rec(at(2026, 11, 19, 21, 0))})
        sent = self.go(now)
        self.assertEqual([c.args[0] for c in sent.call_args_list], ["[GTA6 klipy] denní přehled 19.11."])
        self.assertIn("bananas: 1 postů", sent.call_args_list[0].args[1])
        self.assertEqual(self.go(now + timedelta(hours=1)).call_count, 0)

    def test_dry_run_writes_and_mails_nothing(self):
        now = at(2026, 11, 19, 20, 7)
        with mock.patch.object(M.smtplib, "SMTP_SSL") as smtp:
            with contextlib.ExitStack() as st:
                for p in self.fakes(now, failures=1):
                    st.enter_context(p)
                with contextlib.redirect_stdout(io.StringIO()):
                    _, md = M.run(zkouska=True, denni=True, now=now)
        smtp.assert_not_called()
        self.assertFalse((self.d / "monitor").exists())
        self.assertIn("## Problémy", md)

    def test_send_mail_never_local(self):
        with mock.patch.object(M.smtplib, "SMTP_SSL") as smtp, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertFalse(M.send_mail("s", "b"))
        smtp.assert_not_called()
        self.assertNotIn("example.com", out.getvalue())

    def test_one_source_down_keeps_the_rest(self):
        now = at(2026, 11, 19, 20, 7)
        with contextlib.ExitStack() as st:
            for p in self.fakes(now):
                st.enter_context(p)
            st.enter_context(mock.patch.object(M, "netlify_get", side_effect=OSError("down")))
            st.enter_context(mock.patch.object(M, "send_mail", return_value=True))
            with contextlib.redirect_stdout(io.StringIO()):
                s, md = M.run(now=now)
        self.assertIsNone(s["netlify"])
        self.assertIsNotNone(s["publikace"])
        self.assertIn("Monitor nedostal data", md)


class Clean(unittest.TestCase):
    def test_clean(self):
        s = M.clean("\x1b[31mAuthorization: Bearer abc.def ghp_0123456789abcdefABCDEF x@y.cz\x1b[0m")
        self.assertNotIn("abc.def", s)
        self.assertNotIn("ghp_", s)
        self.assertNotIn("x@y.cz", s)
        self.assertNotIn("\x1b", s)


if __name__ == "__main__":
    unittest.main()
