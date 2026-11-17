"""
posli_mail.py - e-mail to Aleš with everything for a manual YouTube upload of a long-form video.

Aleš 17. 11.: no YouTube API; he uploads long-form in YouTube Studio himself and makes the thumbnail
in Gemini / ChatGPT. Per video one e-mail:
  subject     [<channel>] <title> - nahrát <date time>   (the suggested publish slot, Prague)
  body        download link, what to set in Studio, the THUMBNAIL block (3 text options, the filled-in
              prompt, steps, a tip), title, full description (chapters + credits), tags
  attachments 1_tvar.jpg (streamer's face at the strongest emotion), 2_scena.jpg (payoff frame without
              chat/HUD), 3_tvar2.jpg (second streamer, optional), zaloha_nahled_ai.jpg (Cloudflare FLUX
              backup thumbnail, optional), popis.txt, prompt.txt
Gmail SMTP (smtp.gmail.com:465 SSL) with an app password. Secrets: MAIL_FROM, MAIL_APP_PASSWORD,
MAIL_TO. The addresses are never printed. Without them the e-mail goes to the job log instead.
--uloz DIR saves the e-mail (.eml + .html + attachments) instead of sending - for tests.

Usage:
  python posli_mail.py out/_long/bananas/2026-11-19_funny-day.meta.json --odkaz <artifact url>
  python posli_mail.py <meta.json> --uloz out/_ukazka/long_mail_bananas
"""

import argparse
import html
import json
import os
import smtplib
import ssl
import sys
from datetime import datetime
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path

HERE = Path(__file__).resolve().parent

def load_env():
    env = HERE / ".env"
    for line in env.read_text(encoding="utf-8").splitlines() if env.exists() else []:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            v = v.split(" #")[0].strip().strip('"')
            if v:
                os.environ.setdefault(k.strip(), v)

def popis(m):
    return f"{m['title']}\n\n{m['description']}\n\nTAGY:\n{', '.join(m.get('tags', []))}\n"

def attachments(m):
    """[(file name in the e-mail, path)] in the order the prompt refers to them."""
    out = []
    d = Path(m["nahled_vstup"]) if m.get("nahled_vstup") else None
    if d and d.exists():
        for src, name in (("face.jpg", "1_tvar.jpg"), ("frame.jpg", "2_scena.jpg"), ("face2.jpg", "3_tvar2.jpg")):
            if (d / src).exists():
                out.append((name, d / src))
    if m.get("thumbnail") and Path(m["thumbnail"]).exists():
        out.append(("zaloha_nahled_ai.jpg", Path(m["thumbnail"])))
    return out

def texts_for(m):
    when = datetime.fromisoformat(m["nahrat"])
    kanal = m.get("kanal") or m["znacka"]
    files = attachments(m)
    names = [n for n, _ in files]
    has = lambda n: n in names
    lines = [
        f"Kanál: {kanal}",
        f"Nahrát: {when:%d.%m.%Y %H:%M} (Praha)   Délka: {m.get('duration_s', 0) / 60:.1f} min",
        f"Video ke stažení (platí 3 dny, potřeba přihlášení na GitHub): {m.get('_odkaz') or '(chybí odkaz na artefakt)'}",
        "",
        f"Ve Studiu: nahraj video, vlož název, popis a tagy, miniaturu z Gemini/ChatGPT, naplánuj na "
        f"{when:%d.%m. %H:%M} (Praha), playlist \"{m.get('playlist', m['serie'])}\", kategorie Hry, pro děti NE.",
        "",
        "===== NÁHLED – Gemini/ChatGPT =====",
        "1) Nahraj přílohy " + " a ".join(n for n in ("1_tvar.jpg", "2_scena.jpg") if has(n))
        + (" (3_tvar2.jpg jen když chceš místo ženy druhého streamera)" if has("3_tvar2.jpg") else "") + ".",
        "2) Vlož prompt níže.",
        "3) Nech vygenerovat 3 varianty.",
        "4) Nejlepší dej do YouTube Studia jako miniaturu, další dvě do Test & Compare.",
        "Když obličej nevypadá jako streamer, napiš: \"keep the face from image 1 exactly, do not redraw it\".",
        "Pozor: náhled bez nahoty a sugestivních póz, jinak YouTube omezí reklamy nebo dá věkové omezení.",
    ]
    if not has("1_tvar.jpg"):
        lines.append("Pozor: obličej streamera se nenašel (bez facecamu). Vynech v promptu větu o Image 1, "
                     "nebo nahraj jeho fotku sám.")
    tx = m.get("nahled_texts") or []
    if tx:
        lines += ["", "Text na náhled (první je v promptu, jiný jen přepiš v uvozovkách):"]
        lines += [f"  {i}. {t}" for i, t in enumerate(tx, 1)]
    lines += ["", "--- PROMPT ---", m.get("nahled_prompt") or "(prompt se nevyrobil)", "--- KONEC PROMPTU ---"]
    if has("zaloha_nahled_ai.jpg"):
        lines += ["", "Záloha: zaloha_nahled_ai.jpg je automatický náhled (Cloudflare), kdyby nebyl čas."]
    lines += ["", "===== NÁZEV =====", m["title"], "", "===== POPIS =====", m["description"], "",
              "===== TAGY =====", ", ".join(m.get("tags", []))]
    subject = f"[{kanal}] {m['title']} – nahrát {when:%d.%m. %H:%M}"
    return subject, "\n".join(lines), files

def as_html(body):
    """Plain body -> simple HTML: the prompt / description / tags as copyable <pre> blocks."""
    out, pre = [], False
    for line in body.split("\n"):
        start = line.startswith("--- PROMPT") or line in ("===== POPIS =====", "===== TAGY =====", "===== NÁZEV =====")
        end = line.startswith("--- KONEC") or (pre and line.startswith("====="))
        if pre and end:
            out.append("</pre>")
            pre = False
            if line.startswith("--- KONEC"):
                continue
        if line.startswith("====="):
            out.append(f"<h3>{html.escape(line.strip('= '))}</h3>")
        elif line.startswith("--- PROMPT"):
            out.append("<h4>Prompt</h4>")
        else:
            out.append(html.escape(line) if pre else (html.escape(line) + "<br>"))
        if start:
            out.append("<pre style=\"white-space:pre-wrap;background:#f4f4f4;padding:10px;border-radius:6px\">")
            pre = True
    if pre:
        out.append("</pre>")
    return "<html><body style=\"font-family:sans-serif;font-size:14px\">" + "\n".join(out) + "</body></html>"

def build(m, link):
    m = {**m, "_odkaz": link}
    subject, body, files = texts_for(m)
    msg = EmailMessage(policy=SMTP)
    msg["Subject"] = subject
    msg.set_content(body)
    msg.add_alternative(as_html(body), subtype="html")
    for name, path in files:
        sub = "png" if name.endswith(".png") else "jpeg"
        msg.add_attachment(path.read_bytes(), maintype="image", subtype=sub, filename=name)
    msg.add_attachment(popis(m).encode("utf-8"), maintype="text", subtype="plain", filename="popis.txt")
    if m.get("nahled_prompt"):
        msg.add_attachment(m["nahled_prompt"].encode("utf-8"), maintype="text", subtype="plain", filename="prompt.txt")
    return msg, body, files

def save(msg, body, files, m, target):
    """Test: out/_ukazka/long_mail_x.eml + .html + folder with the attachments; nothing is sent."""
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    target.with_suffix(".eml").write_bytes(bytes(msg))
    target.with_suffix(".html").write_text(as_html(body), encoding="utf-8")
    for name, path in files:
        (target / name).write_bytes(path.read_bytes())
    (target / "popis.txt").write_text(popis(m), encoding="utf-8")
    if m.get("nahled_prompt"):
        (target / "prompt.txt").write_text(m["nahled_prompt"], encoding="utf-8")
    print(f"e-mail uložen: {target.with_suffix('.eml')} (+ .html, přílohy v {target})")

def main():
    p = argparse.ArgumentParser(description="e-mail with a long-form video ready for manual YouTube upload")
    p.add_argument("meta", nargs="+")
    p.add_argument("--odkaz", default="", help="download link of the video (GitHub artifact URL)")
    p.add_argument("--uloz", help="save the e-mail here (.eml/.html + folder) instead of sending (one meta only)")
    a = p.parse_args()
    load_env()
    keys = [os.environ.get(k) for k in ("MAIL_FROM", "MAIL_APP_PASSWORD", "MAIL_TO")]
    for mp in a.meta:
        m = json.loads(Path(mp).read_text(encoding="utf-8"))
        msg, body, files = build(m, a.odkaz)
        if a.uloz:
            save(msg, body, files, m, a.uloz)
            continue
        if not all(keys):
            print("::notice::MAIL_FROM / MAIL_APP_PASSWORD / MAIL_TO chybí - e-mail jen do logu:")
            print(f"Předmět: {msg['Subject']}")
            print(body)
            print("Přílohy: " + ", ".join(part.get_filename() for part in msg.iter_attachments()))
            continue
        msg["From"], msg["To"] = keys[0], keys[2]
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=60) as s:
                s.login(keys[0], keys[1])
                s.send_message(msg)
            print(f"e-mail odeslán: {msg['Subject']}")
        except Exception as e:                          # never print the addresses or the password
            print(f"::error::E-mail se neodeslal ({type(e).__name__}).")
            return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
