# Long-form: co ti přijde e-mailem a jak to nahrát za 2 minuty

Long-form (16:9, 8–15 min) se dělá sám na GitHubu (`.github/workflows/long.yml`) a na YouTube ho nahráváš ty ručně ve Studiu. YouTube API ani audit nepotřebujeme.

## Kdy a co (od 18. 11.: hned, jak je zásoba klipů)
Aleš 18. 11.: long-form se nahrává, **jakmile máme zásobu klipů**. Žádný den v týdnu, žádné video „na zítřek“.
- Běh je **každé 2 h**: cron v `long.yml` jako záloha a k tomu ho každé 2 h spouští 24/7 PC (`worker/beh.ps1`).
- Při každém běhu se pro každý kanál zvlášť udělá tohle:
  1. **Strop:** nejvýš 1 long-form za `long_min_gap_h` hodin (20 h, `brands.json`). Upload je ruční a YouTube nemá rád zahlcení. Po chybě (kontrola neprošla, pád) přijde další pokus nejdřív za `long_retry_h` (6 h), aby chybový e-mail nechodil každé 2 h.
  2. **Další série z rotace** (`brands.json` → `long_calendar.rotation`, pořadí bez vazby na dny). Rotace se posune, až když ta série opravdu vyjde. První díl každého kanálu je „First Reactions“ (momenty z prvních 36 h od odemčení 18. 11. 12:00). Dokud těch 36 h běží, první díl čeká jen na First Reactions. Potom se zkusí ještě jednou a když na něj materiál není, jede rotace.
  3. **Je v poolu silný díl?** Musí mít aspoň `long_target_min` (8 min) po střizích a silnou #1 (u fanpage silný vrchol příběhu). Pod 5 min se nedělá nikdy. Nejdřív proběhne levný odhad bez stahování: když kandidátů zjevně nestačí, běh skončí hned.
  4. Když série materiál nemá, zkusí se hlavní série kanálu (rotace pak zůstane stát, ta série přijde na řadu příště). Když nemá materiál ani hlavní série, běh **potichu skončí** (`*.skip.json`, žádný e-mail) a za 2 h to zkusí znovu.
  5. Když materiál je, video se postaví hned a hned přijde e-mail **„NAHRÁT HNED“**.
- Momenty každé série se berou **od jejího posledního dílu na tom kanálu** (nebo od odemčení hry), nejvýš 7 dní zpět. Nic se neztratí ani při zpožděném běhu.
- Stav je v `long_episodes.json`: `_last` (poslední díl kanálu: kdy, série, další pozice v rotaci), `_series_at` (začátek okna každé série) a `_fail` (poslední chyba). Odpovědi Llama Vision se ukládají do `long_vision.json` (14 dní), aby se běh každé 2 h neptal na stejný moment znovu.
- **gta6bananaclips** (rotace): „GTA 6 Funniest Moments #N“ (tier list) → „<streamer týdne> in GTA 6 #N“ (vede Banana League) → „Banana League Week #N“. V Banana League je každé místo ligy nejlepší moment toho streamera za týden (hra + jeho facecam) a přes něj jen malý odznak „#3 · 7.6 AVG“ ve stylu banánového skóre. Žádná samostatná tabulka.
- **gta6fanpageclip = „THE STORY“** (jiný koncept než bananas, vypínač `fanpage_story`): žádné pořadí, tier list ani #N v obraze. Na začátku je cold open ~6 s ze tří nejsilnějších momentů, pak jdou momenty chronologicky. Název kapitoly („HOUR 2“ = hodina streamu, „11 PM“ = čas klipu) je první vteřinu přes běžící obraz, černé karty už nejsou; slova naběhnou z rozostření a stejně odejdou. U střihů se 2.35:1 pruhy zasunou a vysunou, filmové barvy zůstávají. Na konci posledního momentu kapitoly vyjede zdola tichá reakce naší modelky (`assets/model_reakce/*.webm`, když tam jsou) a zase zajede; samostatná tmavá karta už není. Na konci outro s modelkou a Follow karta.
  - rotace: „<Streamer>'s Night in Vice City #N“ (jeden streamer) → „Same Mission, N Streamers #N“, když najdeme stejnou misi u ≥ 3 streamerů, jinak „Vice City After Dark #N“ (`same_mission_slot`) → „Vice City After Dark #N“ (noc napříč streamery, střihy do rytmu hudby).
- První díl na obou kanálech: „GTA 6: Streamers' First Reactions to Vice City“ (momenty z prvních 36 h od odemčení hry 18. 11. 12:00), když na něj je materiál.
- Každý název začíná „GTA 6“ (u noci jednoho streamera hned po jeho jménu), má do 100 znaků a jména streamerů v závorce.
- Číslo dílu (#N) má každá série zvlášť (`long_episodes.json`). Zvýší se, až když video opravdu vznikne. Když se e-mail s videem neodešle, číslo, použité momenty i rezerva se vrátí zpět (`kompilace.py --vratit`), takže žádný díl nechybí.
- Momenty berou jen čas od odemčení hry (18. 11. 12:00 Praha), nic dřívějšího.
- Téma bere jen momenty s důkazem ze 2 nezávislých zdrojů. Jeden z nich vždy musí být řeč nebo název klipu, druhý zvuk (siréna, náraz, křik) nebo pohyb obrazu. Pak to ještě potvrdí model Llama Vision otázkou ano/ne.
- Nudné momenty (ticho, chůze, menu) nejdou nikam.
- Konzervované scény (holič, obchod, oblečení, tetování, sedání do židle, cutscény, menu) jsou u všech hráčů stejné: když mají dva streameři skoro stejný herní obraz (facecam se maskuje), oba momenty jdou ven. Cutscéna = černé pruhy nahoře i dole. „Same Mission“ bere jen skupiny ≥ 3 streamerů se stejným začátkem a různým výsledkem (`podobnost.py`).
- Moment musí být hraní: obraz z větší části statický (prohlížeč, předávání cen, „SIGNAL LOST“) = ven. Pak to ještě jednou kontroluje Llama Vision na 4 snímcích, ve stejném volání jako téma. Vision sám prohlížeč přes hru nepoznal, chytil ho až test statického obrazu.
- Méně než 15 ověřených momentů → téma nevyjde (zkusí se hlavní série kanálu a v e-mailu je řádek „POZNÁMKA“).
- Nevyužité ověřené momenty tématu jdou na 7 dní do rezervy (`long_reserve.json`). Druhý kanál ani jiná série je nevezmou.
- Video kratší než 5 min se nedělá. Když se video začalo stavět a nevzniklo (neprošlo kontrolou, program spadl nebo vypršel čas), vždy přijde e-mail s důvodem. Čekání na materiál e-mail nemá (`*.skip.json`). Ruční běh s vybranou sérií bez materiálu e-mail má.
- Téma (např. first reactions) bez ověření modelem Llama Vision (došel rozpočet, výpadek) moment nevezme; hlavní série jede dál jen s testem statického obrazu.

## Stavba videa bananas (pevná)
Tier board je klasický TierMaker (`tools/long/tier.py`): černá plocha, pastelové štítky S–D. Závěrečná karta je z `tools/long/endcard.py`; hotové kopie jsou v `assets/long/end_*.mp4`. Fanpage si sama vezme `assets/outro/fanpage_outro_16x9.mp4`, až bude existovat.
1. Cold open ~6 s: tři nejlepší momenty, každý nejvýš 2 s. Je to rozjezd těsně před pointou s přiblížením, takže se ve videu neopakuje stejný střih.
2. Momenty #N…#1, od nejslabšího po nejlepší. Dva momenty stejného streamera ani stejné kategorie nejdou po sobě, když to jde poskládat jinak. Každý má úvodní kartu 0,6 s přes běžící obraz, pak moment (střih hluchých míst, střídavý zoom, titulky klíčových hlášek, zoom + záblesk na pointě, u bananas banán u top 5).
3. Po momentu 1,9 s zařazení do tier listu. Poslední snímek momentu se z celé obrazovky zmenší do karty uprostřed a za ním se objeví board, takže moment do tier listu přejde bez střihu. Zvýraznění proběhne po řádcích a zpomalí na jeho stupni, karta vletí do řádku. Karta se celou dobu jen zmenšuje, nic neposkakuje. Dopad má ránu se subbasem a záři řádku (u S navíc jiskření a krátký otřes, u D komické „splasknutí“). Kamera se pořád pomalu přibližuje. Pod tím hraje znělka z hudební databáze.
4. Plný tier board na 2,4 s (kamera pokračuje v přiblížení, přes náhledy přejede světlo), pak závěrečná karta.
- Prázdný board po cold openu je vypnutý (`long_flags.tier_intro`), znělka u zařazení jde vypnout přes `tier_sting`.
- Teaser „#1 is insane, stay till the end“ je jen na boardu po #6 a #3, nikdy přes moment. Slova naběhnou po jednom z rozostření.

## Tempo a opakování (`tools/long/zabava.py`)
- Stejný moment nesmí být ve videu dvakrát. Hlídá se stejné ID klipu, stejný VOD s překrývajícím se úsekem, stejný zvuk (dva klipy diváků nebo dva pohledy na jednu scénu) a stejný obraz u jednoho streamera. Zůstane ten s lepším skóre.
- Moment s malým pohybem, tichým zvukem a bez smíchu nebo křiku vypadne („low action“).
- Hluché úseky delší než 2 s (nikdo nemluví, nic není hlasité, obraz stojí) se vystřihnou. Když takový úsek leží přímo u pointy, moment vypadne celý.
- Střih je těsnější: začátek na větě asi 10 s před pointou, konec 2–3,5 s po ní. Průměrný moment vychází na 12–16 s.
- **Nikdy uprostřed věty (18. 11.):** začátek jen před začátkem věty (hledá se 7–12,5 s před pointou, pak až 15 s), konec jen po konci věty (2–3,6 s po pointě, pak až 8 s). Klip diváka, který začíná nebo končí, zatímco streamer mluví, se na tom místě nestřihne. Střih nesmí padnout ani do místa, kde je hlasitost řeči a Whisper tam nic nenapsal. Když se bezpečný střih najít nedá, moment vypadne. Střihy hluchých míst jen za dokončenou větou (tečka, nebo nová věta s velkým písmenem), nikdy dvě půlky jedné věty. Cold open začíná na začátku věty a končí mezi slovy. After Dark se kvůli beatu nezkracuje do věty, prodlouží se na další beat. Kontrola (QA) projde každý střih každého momentu; špatný moment vypadne a video se postaví znovu.
- Kapitoly v popisku jsou jen „#N streamer“ (u fanpage čas + streamer), žádné názvy klipů od diváků.

## Hudba (`tools/long/hudba.py`, databáze `assets/hudba/long/`)
- Hraje pod celým videem bez přestávky. Po každém bloku 4–5 momentů se skladba prolnutím vymění, pořadí jde od klidnější po nejsilnější, takže energie roste k #1.
- Pod řečí se ztiší, ale nikdy úplně. U boardu a karet je hlasitější. Pod závěrečnou kartou doznívá, karta má vlastní zvuk.
- Každý stream je stejně hlasitý: moment se nejdřív změří a pak dostane jedno zesílení na −16 LUFS (měří se pásmo řeči, aby hlasitý motor nebo hudba ve hře streamera neztišily). QA pak v hotovém videu změří každý moment zvlášť a nepustí rozdíl větší než ±1,5 LU.
- Databáze: `python tools/long/hudba_db.py hledej` stáhne kandidáty z Openverse (jen `license=cc0`, délka ≥ 90 s) a změří je (hlasitost, šum, výšky, tempo a zřetelnost rytmu, tichá místa). `vyber` uloží nejlepší do `assets/hudba/long/<značka>/` a k nim přidá naše starší skladby. Zdroj, autor a licence každé skladby jsou v `assets/hudba/long/sources.json`.

## Automatická kontrola (QA)
Než se video pošle, zkontroluje se:
- délka 5–15 min a sedící kapitoly,
- kredit u každého momentu,
- žádný černý nebo zamrzlý obraz v momentech,
- hlasitost −16 LUFS ±1,5 a true peak ≤ −1 dBTP, žádné ticho,
- počet položek na boardu a pořadí #N,
- žádný moment dvakrát a průměrná délka momentu do 21 s.

Kontroluje se i text: název s „GTA 6“ do 100 znaků, popis do 5 000 znaků s aspoň 3 kapitolami od 0:00, tagy do 500 znaků.

Když kontrola neprojde, zkusí se video bez tier listu (fanpage nemá tier list, tam se nestaví dvakrát totéž). Momenty se špatným střihem vypadnou a video se postaví ještě jednou. Když neprojde ani pak, přijde jen krátký e-mail „video na <datum> se nepovedlo: <důvod>“.

## Vypínače (`brands.json` → `long_flags`)
`tier_list`, `league`, `thumb_brand` (banán / naše modelka na náhledu), `multi_pov`, `banana_reaction`, `tier_sting`: `false` = vypnuto. `tier_intro`: `true` = zapnuto.

## Co přijde e-mailem (jedno video = jeden e-mail)
- **Předmět:** `[YouTube gta6bananaclips] NAHRÁT HNED – <název>`. Video je hotové teď, nahraj ho hned.
- **Čas zveřejnění** (v textu „ZVEŘEJNIT“): nejbližší dobrý čas, aspoň 45 min od e-mailu, nikdy v celou ani v půl (čas Praha):
  - e-mail mezi 9:40 a 18:40 → dnes večer v primetime USA/UK: bananas 18:40–19:40, fanpage 19:40–20:40,
  - večer a v noci (18:00–3:00) → hned (+45 min),
  - brzy ráno (3:00–8:10) → ranní slot pro Austrálii 8:10–9:40, mezi 8:10 a 9:40 hned.
- Chybový e-mail: `[YouTube <kanál>] long-form se nepovedl (<série>): <důvod>`.
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
6. Viditelnost → Naplánovat → datum a čas z řádku „ZVEŘEJNIT“ v e-mailu (časové pásmo Praha).

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
- Model: FLUX.2 [klein] 9B (`@cf/black-forest-labs/flux-2-klein-9b`). Jedna miniatura 1280×720 stojí zhruba 1 460 neuronů. Zdarma je 10 000 neuronů denně (reset 00:00 UTC), proto nejvýš 6 obrázků denně pro oba účty dohromady (`nahled_ai_budget.json` počítá den UTC jako Cloudflare, proměnná `CF_MAX_IMAGES`). Bananas jde první a nechá fanpage polovinu (každý účet max. 2 varianty, když jde ještě druhý). Kontroly Llama Vision: max. 120 denně (`CF_MAX_VISION`, ~10 neuronů za dotaz).
- Bezpečnostní filtr modelu některé herní snímky odmítne (pád z budovy, zbraně) a někdy i lidi. Skript to pak zkusí jednou bez herního snímku. Když ani to nevyjde, miniatura nebude.

## Ruční spuštění a test
Actions → **long-form** → Run workflow:
- `znacka` obe / bananas / fanpage, `serie` (prázdné = automatika: strop, rotace, cíl 8 min; nebo `glitches`, `chases`, `top-week`, `streamer:<login>` = hned, stačí 5 min, strop se nehlídá), `od`/`do` (YYYY-MM-DD),
- test před launchem: `hra` = `Grand Theft Auto V`, `delka` = `90` (krátké video asi 2 min).

Lokálně: `.venv\Scripts\python kompilace.py --znacka bananas --zkouska` vypíše, co by automatika teď udělala (bez renderu). `--kalendar N` nasimuluje N dalších dílů rotace.
