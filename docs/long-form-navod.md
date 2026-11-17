# Long-form: co ti přijde e-mailem a jak to nahrát za 2 minuty

Long-form (16:9, 8–15 min) se dělá sám na GitHubu (`.github/workflows/long.yml`) a na YouTube ho nahráváš ty ručně ve Studiu. YouTube API ani audit nepotřebujeme.

## Kdy a co (kalendář, `brands.json` → `long_calendar`)
Běh je každý den kolem 00:25 a dělá video na **zítřek**. E-mail tedy přijde den předem. Long-form vychází jen **3× týdně na kanál**:
- **gta6bananaclips:**
  - úterý: „GTA 6 Funniest Moments #N“ (tier list),
  - čtvrtek: „<streamer týdne> in GTA 6 #N“ (vede Banana League),
  - neděle: „Banana League Week #N“: každé místo ligy = nejlepší moment toho streamera za týden (hra + jeho facecam), přes něj jen malý odznak „#3 · 7.6 AVG“ ve stylu banánového skóre. Žádná samostatná tabulka.
- **gta6fanpageclip = „THE STORY“** (jiný koncept než bananas, vypínač `fanpage_story`): žádné pořadí, tier list ani #N v obraze. Momenty jdou chronologicky. Kapitoly jsou černé karty s časem („HOUR 2“ = hodina streamu, „11 PM“ = čas klipu), u střihů 2.35:1 pruhy a filmové barvy. Mezi kapitolami je tichá reakce naší modelky (`assets/model_reakce/*.png`, když tam jsou). Na konci outro s modelkou a Follow karta.
  - pondělí: „<Streamer>'s Night in Vice City #N“ (jeden streamer, jeden den),
  - středa: „Same Mission, N Streamers #N“, když najdeme stejnou misi u ≥ 3 streamerů, jinak „Vice City After Dark #N“,
  - sobota: „Vice City After Dark #N“ (noc napříč streamery, střihy do rytmu hudby).
- 20. 11. na obou: „Streamers' First Reactions to Vice City“.
- Číslo dílu (#N) má každá série zvlášť (`long_episodes.json`). Zvýší se, až když video opravdu vznikne.
- Téma bere jen momenty s důkazem ze 2 nezávislých zdrojů. Jeden z nich vždy musí být řeč nebo název klipu, druhý zvuk (siréna, náraz, křik) nebo pohyb obrazu. Pak to ještě potvrdí model Llama Vision otázkou ano/ne.
- Nudné momenty (ticho, chůze, menu) nejdou nikam.
- Konzervované scény (holič, obchod, oblečení, tetování, sedání do židle, cutscény, menu) jsou u všech hráčů stejné: když mají dva streameři skoro stejný herní obraz (facecam se maskuje), oba momenty jdou ven. Cutscéna = černé pruhy nahoře i dole. „Same Mission“ bere jen skupiny ≥ 3 streamerů se stejným začátkem a různým výsledkem (`podobnost.py`).
- Moment musí být hraní: obraz z větší části statický (prohlížeč, předávání cen, „SIGNAL LOST“) = ven. Pak to ještě jednou kontroluje Llama Vision na 4 snímcích, ve stejném volání jako téma. Vision sám prohlížeč přes hru nepoznal, chytil ho až test statického obrazu.
- Méně než 15 ověřených momentů → vyjde hlavní série kanálu a v e-mailu je řádek „POZNÁMKA“.
- Nevyužité ověřené momenty tématu jdou na 7 dní do rezervy (`long_reserve.json`). Druhý kanál ani jiná série je nevezmou.
- Video kratší než 5 min se nedělá.

## Stavba videa bananas (pevná)
Tier board je klasický TierMaker (`tools/long/tier.py`): černá plocha, pastelové štítky S–D, čtvercové náhledy momentů, které do řádku přiletí s pružinou a vlastním zvukem. Závěrečná karta je z `tools/long/endcard.py`; hotové kopie jsou v `assets/long/end_*.mp4`. Fanpage si sama vezme `assets/outro/fanpage_outro_16x9.mp4`, až bude existovat. Naše hudba se pod závěrečnou kartou ztiší, karta má vlastní.
1. Cold open 10–12 s ze tří nejlepších pointe.
2. Prázdný tier board na 2 s.
3. Momenty #N…#1. Každý má úvodní kartu 0,6 s, pak moment (střih hluchých míst, střídavý zoom, titulky klíčových hlášek, zoom + záblesk na pointě, u bananas banán u top 5). Pak 1,5 s „let“ do tier listu (S = top 10 %, A, B, C, D).
4. Plný tier board na 3 s, pak závěrečná karta.
- Teaser „#1 is insane, stay till the end“ je jen na boardu po #6 a #3, nikdy přes moment.
- Hudba hraje potichu pod celým videem, pod řečí se ztiší.

## Automatická kontrola (QA)
Než se video pošle, zkontroluje se:
- délka 5–15 min a sedící kapitoly,
- kredit u každého momentu,
- žádný černý nebo zamrzlý obraz v momentech,
- hlasitost −16 LUFS ±1,5 a true peak ≤ −1 dBTP, žádné ticho,
- počet položek na boardu a pořadí #N.

Když kontrola neprojde, zkusí se video bez tier listu. Když neprojde ani pak, přijde jen krátký e-mail „video na <datum> se nepovedlo: <důvod>“.

## Vypínače (`brands.json` → `long_flags`)
`tier_list`, `league`, `thumb_brand` (banán / naše modelka na náhledu), `multi_pov`, `banana_reaction`: `false` = vypnuto.

## Co přijde e-mailem (jedno video = jeden e-mail)
- **Předmět:** `[gta6bananaclips] <název> – nahrát 20.11. 01:53`. Čas je navržený slot pro zveřejnění, každý den trochu jiný a nikdy v celou ani v půl.
- **Text:**
  - odkaz na stažení videa,
  - blok **NÁHLED – Gemini/ChatGPT**: 3 varianty textu, hotový prompt a postup,
  - název, celý popis s kapitolami a kredity streamerů, tagy oddělené čárkou.
- **Přílohy:**
  - `1_tvar.jpg`: obličej streamera ve chvíli největší emoce (z facecamu, kolem nejhlasitějšího místa pointy),
  - `2_scena.jpg`: snímek pointy bez chatu a HUD,
  - `3_znacka.png`: bananas = náš banán bez pozadí, fanpage = naše modelka (prompt ji pokaždé oblékne do jiného outfitu z Vice City),
  - `4_tvar2.jpg`: druhý streamer (jen když se jeho obličej našel),
  - `zaloha_nahled_ai.jpg`: automatický záložní náhled z Cloudflare (jen když vyšel),
  - `popis.txt` a `prompt.txt`.
- Odkaz na video vede na GitHub Actions artefakt. Je potřeba být přihlášený na GitHubu a stáhnout ho do 3 dnů. Stáhne se jako zip s mp4 uvnitř.
- Popis scény v promptu píše model Llama 3.2 Vision na Cloudflare (stojí pár neuronů). Bez něj se scéna odvodí ze štítků momentu.

## Náhled v Gemini / ChatGPT (1 minuta)
1. Nahraj přílohy 1–3 (4 jen když chceš na náhledu druhého streamera).
2. Vlož prompt z e-mailu. Jiný text náhledu stačí přepsat v uvozovkách.
3. Nech vygenerovat 3 varianty.
4. Nejlepší dej ve Studiu jako miniaturu, další dvě do „Test & Compare“.
- Když obličej nevypadá jako streamer, napiš: „keep the face from image 1 exactly, do not redraw it“.
- Na náhledu je vždy náš „obličej“ kanálu: u bananas banán se stejnou emocí jako streamer a tier odznak (např. „S“), u fanpage naše modelka a „HEAT 5/5“. K tomu odznak GTA 6 a #N. Červená šipka jen když pomůže.
- Náhled musí být bez nahoty a sugestivních póz, jinak YouTube omezí reklamy nebo video věkově omezí.

## Nahrání ve Studiu (2 minuty)
1. studio.youtube.com, přepni se na správný kanál, Vytvořit → Nahrát videa → vyber mp4 ze zipu.
2. Do pole Název vlož název z e-mailu, do Popisu celý blok popisu (kapitoly se načtou samy).
3. Miniatura: ta z Gemini/ChatGPT. Další dvě varianty přidej přes „Test & Compare“.
4. Playlist: playlist série (`funny-day`, `wild-day`, `top-week`…). Napoprvé ho založ.
5. Publikum: „Ne, není určeno dětem“. Další možnosti: vlož tagy, kategorie Hry, jazyk angličtina.
6. Viditelnost → Naplánovat → datum a čas z předmětu (časové pásmo Praha).

## Jednorázové nastavení

### Krok 1: ověřený telefon kanálu (bez něj nejdou vlastní miniatury)
Pro každý kanál: youtube.com/verify → ověř telefonním číslem. Bez toho Studio nedovolí nahrát vlastní miniaturu.

### Krok 2: Gmail heslo aplikace (pro odesílání e-mailů z GitHubu)
1. Účet Google, ze kterého se má posílat (může to být ten tvůj): myaccount.google.com → Zabezpečení → zapni **Dvoufázové ověření** (bez něj hesla aplikací nejdou).
2. myaccount.google.com/apppasswords → název třeba „gta6 long-form“ → Vytvořit → zobrazí se 16 znaků. Zkopíruj je bez mezer.
3. GitHub → repo → Settings → Secrets and variables → Actions → New repository secret:
   - `MAIL_FROM` = adresa Gmailu, ze které se posílá,
   - `MAIL_APP_PASSWORD` = těch 16 znaků,
   - `MAIL_TO` = kam to má chodit.
   Adresy nikde v repu ani v logu nejsou, jsou jen v secrets.
4. Bez těchto secrets se e-mail jen vypíše do logu běhu a běh nespadne.

### Krok 3: Cloudflare token (popis scény + záložní AI náhled, zdarma)
1. dash.cloudflare.com → vpravo nahoře profil → **My Profile → API Tokens → Create Token**.
2. Šablona **Workers AI** (Use template). Oprávnění Workers AI: Read + Edit. Účet: tvůj. Create Token a token zkopíruj (ukáže se jen jednou).
3. Account ID: dash.cloudflare.com → Workers & Pages → vpravo „Account ID“.
4. GitHub secrets: `CF_API_TOKEN`, `CF_ACCOUNT_ID`.
- Model: FLUX.2 [klein] 9B (`@cf/black-forest-labs/flux-2-klein-9b`). Jedna miniatura 1280×720 stojí zhruba 1 460 neuronů. Zdarma je 10 000 neuronů denně (reset 00:00 UTC), proto nejvýš 6 obrázků denně pro oba účty dohromady (`nahled_ai_budget.json`, proměnná `CF_MAX_IMAGES`).
- Bezpečnostní filtr modelu některé herní snímky odmítne (pád z budovy, zbraně) a někdy i lidi. Skript to pak zkusí jednou bez herního snímku. Když ani to nevyjde, miniatura nebude.

## Ruční spuštění a test
Actions → **long-form** → Run workflow:
- `znacka` obe / bananas / fanpage, `serie` (prázdné = denní série; nebo `glitches`, `chases`, `top-week`, `streamer:<login>`), `od`/`do` (YYYY-MM-DD),
- test před launchem: `hra` = `Grand Theft Auto V`, `delka` = `90` (krátké video asi 2 min).

Lokálně: `.venv\Scripts\python kompilace.py --znacka bananas --serie funny-day --zkouska` vypíše výběr bez renderu.
