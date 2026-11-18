"""
posli_mail.py - e-mail to Aleš with everything for a manual YouTube upload of a long-form video.

Aleš 17. 11.: no YouTube API; he uploads long-form in YouTube Studio himself and makes the thumbnail
in Gemini / ChatGPT. Per video one e-mail:
  subject     [YouTube <channel>] NAHRÁT HNED – <title>  (Aleš 18. 11.: built as soon as there is a stock of
              clips; the body has the next good publish slot >= 45 min ahead, Prague)
  body        download link, what to set in Studio, the THUMBNAIL block (3 text options, the filled-in
              prompt, steps, a tip), title, full description (chapters + credits), tags
  attachments 1_tvar.jpg (streamer's face at the strongest emotion), 2_scena.jpg (payoff frame without
              chat/HUD), 3_tvar2.jpg (second streamer, optional), zaloha_nahled_ai.jpg (Cloudflare FLUX
              backup thumbnail, optional), story_odkaz.jpg (Instagram story for the link sticker,
              stories.link_story), popis.txt, prompt.txt
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

def cenzura_meta(m):
    """18. 11.: swears / sexual words starred in everything pasted into YouTube (title, description, thumbnail
    text and prompt); a tag with such a word is dropped (a starred tag finds nothing)."""
    from cenzura import cenzura, tag_ok
    for k in ("title", "description", "nahled_prompt"):
        if isinstance(m.get(k), str):
            m[k] = cenzura(m[k])
    if isinstance(m.get("nahled_texts"), list):
        m["nahled_texts"] = [cenzura(x) if isinstance(x, str) else x for x in m["nahled_texts"]]
    if isinstance(m.get("tags"), list):
        m["tags"] = [t for t in m["tags"] if not isinstance(t, str) or tag_ok(t)]
    return m

def popis(m):
    return f"{m['title']}\n\n{m['description']}\n\nTAGY:\n{', '.join(m.get('tags', []))}\n"

def attachments(m):
    """[(file name in the e-mail, path)] numbered like the prompt's images: without the streamer's face the
    game frame is image 1 and the brand image 2 (nahled.build_prompt renumbers the same way)."""
    out = []
    d = Path(m["nahled_vstup"]) if m.get("nahled_vstup") else None
    if d and d.exists():
        k = 0
        for src, name in (("face.jpg", "tvar.jpg"), ("frame.jpg", "scena.jpg"), ("znacka.png", "znacka.png"),
                          ("face2.jpg", "tvar2.jpg")):
            if (d / src).exists():
                k += 1
                out.append((f"{k}_{name}", d / src))
    if m.get("thumbnail") and Path(m["thumbnail"]).exists():
        out.append(("zaloha_nahled_ai.jpg", Path(m["thumbnail"])))
    if m.get("_story") and Path(m["_story"][0]).exists():
        out.append(("story_odkaz.jpg", Path(m["_story"][0])))
    return out

def texts_for(m):
    when = datetime.fromisoformat(m["nahrat"])
    kanal = m.get("kanal") or m["znacka"]
    files = attachments(m)
    names = [n for n, _ in files]
    has = lambda n: n in names
    den = ["pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota", "neděle"][when.weekday()]
    first = lambda suffix: next((n for n in names if n.endswith("_" + suffix)), None)
    end_screen = ({"bananas": ["Video končí kartou s banánem. Rámečky pro prvky YouTube: vlevo a vpravo nahoře dvě "
                               "videa, uprostřed mezi nimi kruh pro odběr. Postup:",
                               "1) Přidej prvek Video → „Nejnovější nahrané“ a přetáhni ho do levého rámečku.",
                               f"2) Přidej prvek Playlist → \"{m.get('playlist', m['serie'])}\" (série) a přetáhni ho "
                               "do pravého rámečku.",
                               "3) Přidej prvek Odběr a přetáhni ho do kruhu uprostřed."],
                   "fanpage": ["Video končí kartou s modelkou. Rámečky pro prvky YouTube jsou v pravém sloupci nad "
                               "sebou: dvě videa a pod nimi kruh pro odběr. Postup:",
                               "1) Přidej prvek Video → „Nejnovější nahrané“ a přetáhni ho do horního rámečku.",
                               f"2) Přidej prvek Playlist → \"{m.get('playlist', m['serie'])}\" (série) a přetáhni ho "
                               "do druhého rámečku.",
                               "3) Přidej prvek Odběr a přetáhni ho do kruhu pod nimi."]})[
        "fanpage" if m.get("znacka") == "fanpage" else "bananas"]
    lines = [
        f"KANÁL: YouTube {kanal}",
        "NAHRÁT HNED (video je hotové teď, žádné čekání na den).",
        f"ZVEŘEJNIT: {den} {when:%d.%m.%Y} v {when:%H:%M} (čas Praha) – nejbližší dobrý čas, aspoň 45 min od e-mailu",
        *([f"POZNÁMKA: {m['poznamka']}"] if m.get("poznamka") else []),
        f"Délka: {m.get('duration_s', 0) / 60:.1f} min",
        f"Video ke stažení (platí 3 dny, potřeba přihlášení na GitHub): {m.get('_odkaz') or '(chybí odkaz na artefakt)'}",
        "",
        f"Ve Studiu: nahraj video, vlož název, popis a tagy, miniaturu z Gemini/ChatGPT, naplánuj na "
        f"{when:%d.%m. %H:%M} (Praha), playlist \"{m.get('playlist', m['serie'])}\", kategorie Hry, pro děti NE.",
        "",
        *(["===== INSTAGRAM STORY S ODKAZEM =====",
           f"Po nahrání videa: Instagram {kanal} → story → nahraj přílohu story_odkaz.jpg → nálepka Odkaz → "
           "vlož odkaz na video → sdílet (30 s).",
           f"Nálepku Odkaz dej {m['_story'][1]}, na volné místo mimo obličej. Odkaz na naplánované video "
           f"funguje až od zveřejnění, story proto sdílej až po {when:%H:%M}.",
           f"Story s „link in bio“ jde sama asi 2 h po {when:%H:%M}, proto video zveřejni v čase výše.",
           ""] if has("story_odkaz.jpg") else []),
        "===== KONCOVÁ OBRAZOVKA (Studio → Úpravy → Koncová obrazovka) =====",
        *end_screen,
        "4) Časování: všechny tři prvky nech běžet až do konce videa (celá závěrečná karta).",
        "Další díly: v koncové obrazovce dej „Importovat z videa“ a vyber předchozí díl, prvky sednou samy.",
        "",
        "===== NÁHLED – Gemini/ChatGPT =====",
        "1) Nahraj přílohy " + ", ".join(n for n in names if not n.endswith("_tvar2.jpg") and n[:1].isdigit())
        + (f" ({first('tvar2.jpg')} jen když chceš druhého streamera)" if first("tvar2.jpg") else "") + ".",
        "2) Vlož prompt níže.",
        "3) Nech vygenerovat 3 varianty.",
        "4) Nejlepší dej do YouTube Studia jako miniaturu, další dvě do Test & Compare.",
        "Když obličej nevypadá jako streamer, napiš: \"keep the face from image 1 exactly, do not redraw it\".",
        "Pozor: náhled bez nahoty a sugestivních póz, jinak YouTube omezí reklamy nebo dá věkové omezení.",
    ]
    if not has("1_tvar.jpg"):
        lines.append("Pozor: obličej streamera se nenašel (bez facecamu). Prompt je na to upravený: obrázky jsou "
                     "očíslované podle příloh a místo streamera nakreslí vymyšleného hráče se sluchátky.")
    tx = m.get("nahled_texts") or []
    if tx:
        lines += ["", "Text na náhled (první je v promptu, jiný jen přepiš v uvozovkách):"]
        lines += [f"  {i}. {t}" for i, t in enumerate(tx, 1)]
    lines += ["", "--- PROMPT ---", m.get("nahled_prompt") or "(prompt se nevyrobil)", "--- KONEC PROMPTU ---"]
    if has("zaloha_nahled_ai.jpg"):
        lines += ["", "Záloha: zaloha_nahled_ai.jpg je automatický náhled (Cloudflare), kdyby nebyl čas."]
    lines += ["", "===== NÁZEV =====", m["title"], "", "===== POPIS =====", m["description"], "",
              "===== TAGY =====", ", ".join(m.get("tags", []))]
    subject = f"[YouTube {kanal}] NAHRÁT HNED – {m['title']}"
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

def link_story(m):
    """stories.link_story -> (path, where the sticker goes) or None; never stops the e-mail."""
    if not m.get("title"):
        return None
    try:
        import stories
        return stories.link_story(m)
    except Exception as e:
        print(f"::warning::story_odkaz.jpg se nevyrobil ({type(e).__name__}: {str(e)[:160]})")
        return None

def build(m, link):
    m = {**m, "_odkaz": link}
    m["_story"] = link_story(m)
    subject, body, files = texts_for(m)
    msg = EmailMessage(policy=SMTP)
    msg["Subject"] = subject
    msg.set_content(body)
    msg.add_alternative(as_html(body), subtype="html")
    for name, path in list(files):
        sub = "png" if name.endswith(".png") else "jpeg"
        try:
            data = path.read_bytes()
        except OSError:
            if name != "story_odkaz.jpg":                # only the optional story may go missing
                raise
            files.remove((name, path))
            continue
        msg.add_attachment(data, maintype="image", subtype=sub, filename=name)
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
    if m.get("title"):                                   # a .failed.json note has no texts
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
    status = 0
    for mp in a.meta:
        m = cenzura_meta(json.loads(Path(mp).read_text(encoding="utf-8")))
        if mp.endswith(".failed.json"):                   # QA stopped the video: a short note, no link
            msg = EmailMessage(policy=SMTP)
            kanal = "gta6bananaclips" if m.get("znacka") == "bananas" else "gta6fanpageclip"
            why = " ".join(str(m.get("duvod", "")).split())       # a crash reason may hold newlines (ffmpeg)
            msg["Subject"] = f"[YouTube {kanal}] long-form se nepovedl ({m.get('serie')}): {why[:120]}"
            body = (f"Long-form {m.get('serie')} ({m.get('kdy') or m.get('publish_day')}) se nepovedl (automatická "
                    f"kontrola, pád nebo ruční běh bez materiálu), proto video nepřijde a nic se nenahrává.\n"
                    f"Důvod: {m.get('duvod')}\n"
                    "Stav (čísla dílů, použité momenty) se nezměnil. Automatický běh to zkusí znovu sám "
                    "(nejdřív za long_retry_h hodin, brands.json).\n")
            msg.set_content(body)
            files = []
        else:
            msg, body, files = build(m, a.odkaz)
        if a.uloz:
            save(msg, body, files, m, a.uloz)
            continue
        if not all(keys):
            print("::notice::MAIL_FROM / MAIL_APP_PASSWORD / MAIL_TO chybí - e-mail jen do logu:")
            print(f"Předmět: {msg['Subject']}")
            print(body)
            print("Přílohy: " + ", ".join(part.get_filename() for part in msg.iter_attachments()))
            if os.environ.get("GITHUB_ACTIONS") == "true":  # on GitHub nothing reached Aleš: long.yml rolls back
                print("::error::E-mail se neodeslal (chybí secrets MAIL_*).")
                status = 2
            continue
        msg["From"], msg["To"] = keys[0], keys[2]
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=60) as s:
                s.login(keys[0], keys[1])
                s.send_message(msg)
            print(f"e-mail odeslán: {msg['Subject']}")
        except Exception as e:                          # never print the addresses or the password
            print(f"::error::E-mail se neodeslal ({type(e).__name__}).")
            status = 1                                  # the other notes of this batch are still tried
    return status

if __name__ == "__main__":
    sys.exit(main())
