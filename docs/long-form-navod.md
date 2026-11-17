# Long-form: co ti přijde e-mailem a jak to nahrát za 2 minuty

Long-form (16:9, 8–15 min) se dělá sám na GitHubu (`.github/workflows/long.yml`) a na YouTube ho nahráváš ty ručně ve Studiu. YouTube API ani audit nepotřebujeme.

## Kdy
- **Denně kolem 00:25** (Praha) vznikne za včerejší den jedno video pro každý účet:
  - gta6bananaclips: „GTA 6 Funniest Moments – Day N“ (série `funny-day`),
  - gta6fanpageclip: „GTA 6 Wildest Streamer Moments – Day N“ (série `wild-day`).
- **V neděli kolem 16:10** navíc „Top N … This Week“ pro oba účty (`top-week`).
- GitHub u veřejného repa spouští plán někdy o hodiny později. Video je pak za správný den, jen přijde později.
- Když je za den málo momentů (méně než 8 min), video nevznikne a v běhu je žluté varování.

## Co přijde e-mailem (jedno video = jeden e-mail)
- **Předmět:** `[gta6bananaclips] <název> – nahrát 20.11. 01:53`. Čas je navržený slot pro zveřejnění, každý den trochu jiný a nikdy v celou ani v půl.
- **Text:**
  - odkaz na stažení videa,
  - blok **NÁHLED – Gemini/ChatGPT**: 3 varianty textu, hotový prompt a postup,
  - název, celý popis s kapitolami a kredity streamerů, tagy oddělené čárkou.
- **Přílohy:**
  - `1_tvar.jpg`: obličej streamera ve chvíli největší emoce (z facecamu, kolem nejhlasitějšího místa pointy),
  - `2_scena.jpg`: snímek pointy bez chatu a HUD,
  - `3_tvar2.jpg`: druhý streamer z top 3 (jen když se jeho obličej našel; použij místo ženy, chceš-li),
  - `zaloha_nahled_ai.jpg`: automatický záložní náhled z Cloudflare (jen když vyšel),
  - `popis.txt` a `prompt.txt`.
- Odkaz na video vede na GitHub Actions artefakt. Je potřeba být přihlášený na GitHubu a stáhnout ho do 3 dnů. Stáhne se jako zip s mp4 uvnitř.
- Popis scény v promptu píše model Llama 3.2 Vision na Cloudflare (stojí pár neuronů). Bez něj se scéna odvodí ze štítků momentu.

## Náhled v Gemini / ChatGPT (1 minuta)
1. Nahraj přílohy 1 a 2 (3 jen když chceš místo ženy druhého streamera).
2. Vlož prompt z e-mailu. Jiný text náhledu stačí přepsat v uvozovkách.
3. Nech vygenerovat 3 varianty.
4. Nejlepší dej ve Studiu jako miniaturu, další dvě do „Test & Compare“.
- Když obličej nevypadá jako streamer, napiš: „keep the face from image 1 exactly, do not redraw it“.
- Na každém náhledu je druhá postava: vymyšlená žena ve stylu Vice City (oblečení, účes a póza se mezi videi střídají ze 6 kombinací). Banán ani modelka fanpage na náhledu nejsou.
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
