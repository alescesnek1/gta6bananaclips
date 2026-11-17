# Long-form na YouTube (návrh 17. 11. 2026, postaveno týž den)

**Stav (17. 11. večer):** hotovo, upload je ruční (Aleš nechtěl YouTube API ani audit). Jak to běží a jak nahrávat: `docs/long-form-navod.md`.
- Sběr: `klipuj.py` píše každý short do `long_pool.json` (16:9 zdroj, pointa, HPC střih, štítky `funny/glitch/chase/crash/rage/first-reaction/wow`). Kandidáti, kteří se nevešli do kvóty, jdou do poolu jen jako řádek ze seznamu (bez stahování). Režim `klipuj.py sber` jen sbírá a štítkuje bez renderu shortu.
- `kompilace.py` (výběr, střih, cold open, odpočet, banánové skóre / neonový přechod, kapitoly, kredity) → `nahled.py` (vstupy miniatury) → `nahled_ai.py` (Cloudflare FLUX.2 klein-9b + náš nadpis) → `posli_mail.py` (Gmail) → `.github/workflows/long.yml`.
- Sdílený seznam použitých momentů: `long_used.json`. Rozpočet AI obrázků: `nahled_ai_budget.json`.


Cíl: automaticky publikovat delší videa (8–15 min) na YouTube obou účtů, každý účet jiná videa a jiné série. Shorty běží dál beze změny.

## Proč a v čem je jiný
- Long-form na YouTube přináší reklamy uprostřed videa (od 8 min), vyhledávání a doporučení a diváky, kteří se vracejí. Shorty přivedou lidi, long-form je udrží.
- Formát: **16:9 na šířku** (1920×1080), původní rozložení streamu (hra přes celý obraz a facecam streamera, jak ho má ve streamu). Nic se neořezává do 9:16. Zvuk je originál streamu, titulky jen u důležitých hlášek.
- Zdroj: momenty, které už sbíráme. Twitch klipy diváků, špičky živého chatu, YouTube okna a hlavně momenty, které se **nedostaly do shortů**, kterých bude po launchi stovky denně. Ukládá se `source.mp4` (16:9) a meta.

## Série (každý účet jiné, žádný překryv klipů)
| | gta6bananaclips (vtipné, banán) | gta6fanpageclip (sleek, „vrrr“) |
|---|---|---|
| denně v launch týdnu | **„GTA 6 Funniest Moments – Day N“** | **„GTA 6 Wildest Streamer Moments – Day N“** |
| týdně | **Top 25 nejvtipnějších týdne** s banánovým skóre u každého | **Best Car Chases & Crashes / Craziest Glitches** |
| tematicky | Rage moments, Streamers vs GTA 6 physics, Fails | Streamers react to Vice City first time, Clutch escapes, Glitches & bugs |
| podle streamera | „Kai Cenat GTA 6 funniest“ | „xQc GTA 6 best moments“ |

## Jak to bude fungovat (pipeline)
1. **Sběr a značkování** (běží pořád): každý moment dostane štítky podle přepisu, chatu a obrazu:
   - `funny`: smích, „lol/lmao“, smajlíci v chatu,
   - `glitch`: slova „bug, glitch, broken, clipping, how“ a divná fyzika,
   - `chase`/`crash`: hodně pohybu, sirény, „cops“,
   - `rage`: nadávky, křik,
   - `first-reaction`: první den hraní, „Vice City“, „wow“.
   K tomu skóre virality a streamer.
2. **Výběr** (jednou denně ráno, týdenní v neděli): pro sérii vybere 25–40 nejlepších momentů z posledních 24 h nebo 7 dní, nejvýš 4 od jednoho streamera. Momenty, které už použil druhý účet, se nesmí opakovat (sdílený seznam použitých klipů).
3. **Střih**: každý moment ořízne podle stejného HPC jako shorty (začít v akci, konec po pointě). Mezi momenty krátký střih se zvukem. Na začátku 15 s **cold open** z nejlepších 3 momentů, pak odpočet od nejslabšího k nejlepšímu. U banánů po každém momentu malé banánové skóre, u fanpage vlastní přechod. Vodoznak účtu a kredit streamera v rohu u každého momentu (`@kanál`). Délka 8–15 min.
4. **Balení**:
   - název ze šablon („GTA 6 Funniest Moments Day 2 (Kai Cenat, xQc, Speed)“),
   - **kapitoly** v popisku s časy a jménem streamera,
   - kredit a odkaz na každého streamera,
   - hashtagy,
   - **miniatura** automaticky: snímek pointy nejlepšího momentu + obličej streamera + značka účtu.
5. **Upload a plán**: rovnou na YouTube kanál účtu v pevném denním okně s posunem (jako shorty), u denní série do 2 h po uzávěrce dne.

## Upload: na čem to stojí (rozhodnout)
- **Buffer** umí u YouTube spolehlivě jen Shorts; dlouhá videa přes API zatím neověřeno → nejspíš ne.
- **YouTube Data API v3** (doporučeno): upload přímo z GitHubu, žádný Netlify. 10 min videa ~450 MB přes Netlify by rozbilo rozpočet kreditů. Potřeba:
  - Google Cloud projekt + OAuth pro každý kanál (refresh token do GitHub Secrets; zakládá Aleš),
  - kvóta 10 000 jednotek denně, upload stojí 1 600 → ~6 uploadů denně na projekt (stačí),
  - **pozor:** videa nahraná z neověřeného API projektu YouTube zamyká jako soukromá, dokud projekt neprojde auditem (formulář „YouTube API Services Audit“, schválení trvá dny až týdny) → zažádat hned.
- Render 10–15 min videa na CPU runneru GitHubu trvá zhruba 20–40 min. U veřejného repa zdarma.

## Rizika
- **Monetizace (YPP):** kompilace cizího obsahu bez přidané hodnoty YouTube označuje jako „reused content“ a nemonetizuje. Pomáhá vlastní struktura (odpočet, hodnocení, kapitoly, banán), ale bez vlastního komentáře je schválení nejisté. AI hlas nedáváme (Alešovo pravidlo).
- **Content ID:** hudba z rádia ve hře → nárokovky (výnos jde vlastníkovi, video zůstane). Při výběru momentů lze úseky s písní (náš detektor zpěvu) přeskočit.
- **Streameři:** kredit u každého momentu, blocklist dál platí.

## Co je potřeba na implementaci
1. Ukládat 16:9 zdroj a štítky ke každému momentu (rozšířit `klipuj.py` meta). Zhruba 2 h.
2. `kompilace.py`: výběr, střih, cold open, odpočet, kapitoly, miniatura. Zhruba 1 den.
3. `youtube_upload.py` (Data API, OAuth refresh token) + workflow `long.yml` (denně a týdně). Zhruba půl dne.
4. Od Aleše: Google Cloud projekt, OAuth klienti pro oba kanály, žádost o audit API.
