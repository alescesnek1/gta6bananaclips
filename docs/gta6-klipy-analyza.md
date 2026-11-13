# GTA 6: tvůrci a klipy – analýza pro launch

Stav k 12. 11. 2026. GTA 6 vychází **čtvrtek 19. 11. 2026** na PS5 a Xbox Series X|S. PC verze nemá datum. Take-Two na posledním earnings callu oznámil, že hra je „gone gold“ a další odklad nebude (Insider Gaming, Push Square). Přesný čas odemčení Rockstar nepotvrdil.

Čísla sledujících: YouTube z YouTube data API (TubeAlfred) 12. 11. 2026; Twitch z TwitchTracker (žebříček EN, listopad 2026) nebo Wikipedie / Social Blade (2. 8. 2026); Kick z článků 2026, jen přibližně. Co není ověřené, je tak označené.

---

## 1. Shrnutí

1. **„Odkaz v popisku = čisté“ neplatí.** Uvedení zdroje není licence. Česko nemá americký fair use. Citace podle §31 autorského zákona kryje jen přiměřené úryvky uvnitř vlastního díla nebo kritiky, ne přepost cizího klipu. YouTube i Meta navíc výslovně píšou, že ani svolení autora nebo kredit z přepostu „originální“ obsah nedělá. **Úplně čistá cesta neexistuje**, jen cesta s nejnižším dostupným rizikem: (a) oficiální clipping kampaně, kde tvůrce reuse sám povolil a platí za views, (b) písemné svolení, (c) vlastní přidaná hodnota v každém klipu (český komentář, titulky, kontext). Kampaň ani svolení streamera **nekryjí záběry Rockstaru a hudbu**: Rockstar povoluje jen nekomerční užití. V praxi toleruje monetizovaná videa streamerů, písemně to ale nepovoluje.
2. **Kdo je nejcennější obsahově:** IShowSpeed, Kai Cenat, Adin Ross, parta Lacy/Clix/JasonTheWeen, penguinz0 a Jynxzi. Speed a Kai jsou ale nejvíc přeplnění: klipy z nich dělají stovky kanálů. Méně obsazení a podobně vtipní: CaseOh, Jynxzi, Lacyho parta, Summit1g a RP veteráni na konzoli. **Pořadí nasazení ale určuje legální přístup** (sekce 2, první tabulka): ověřený placený program má dnes jen N3on; Adin Ross s ním síť ohlásil, podmínky neověřeny. Ostatní čekají na svolení.
3. **TikTok v Česku nejspíš přímo nevydělá.** Creator Rewards Program podle všech nalezených seznamů zemí ČR nezahrnuje (oficiální seznam TikToku neověřen; zkontrolovat v TikTok Studiu). Příjem z TikToku = jen z clipping kampaní.
4. **GTA RP (NoPixel apod.) na launchi nebude.** RP potřebuje PC a mody. RP streameři budou hrát příběh na konzoli. Twitch CEO čeká GTA 6 Online až v roce 2027.
5. **SupoClip na herní streamy nestačí.** Momenty vybírá hlavně z přepisu řeči. Herní virál je výkřik, akce nebo výbuch chatu. Živé streamy výslovně odmítá. Hodí se nanejvýš jako renderer pro VOD.
6. **Nejrychlejší cesta:** na Twitchi klipy, které dělají sami diváci (Twitch Helix API, řazeno podle zhlédnutí) + rychlost chatu + hlasitost zvuku → ffmpeg šablona 9:16 → kontrola člověkem. Cíl: klip venku 5–15 min po momentu. YouTube live (Speed) a Kick (Adin, N3on) divácké klipy přes API nedávají: tam vlastní záznam + chat + hlasitost, ~10–25 min.
7. **GTA-specifická rizika:** hudba z rádia ve hře (Content ID), spoilery a cutscény (Rockstar je stahuje), leaky (DMCA už v srpnu 2026). Platí i po launchi.

---

## 2. Tvůrci

### Legální přístup (rozhoduje o pořadí nasazení)

| Přístup | Tvůrci | Co dělat |
|---|---|---|
| **Ověřený placený program** | N3on (vlastní, 0,40–0,50 USD / 1 000 views) | přihlásit se hned, uložit zadání kampaně |
| **Ohlášeno, podmínky neověřeny** | Adin Ross (clipping síť s N3onem, sazba neuvedena) | zjistit podmínky; do té doby jako bez programu |
| **Jen sponzorská kampaň** | IShowSpeed: kampaň Expedia na Whop (1 USD / 1 000) kryje jen obsah té kampaně, ne libovolné GTA klipy; dál agentura Clipping (hlášeno) | ověřit, jestli Clipping bere externí klipaře; jinak svolení |
| **Bez programu → čeká na svolení** | Kai Cenat, Lacy/Clix/Jason, penguinz0, Jynxzi, CaseOh, xQc, Summit1g a ostatní | poslat žádost o svolení ještě před 19. 11.; do odpovědi nestříhat |
| **Před launchem prohledat** | Whop Content Rewards, Vyro, Kick Clipping Program na „GTA 6“ | nové kampaně se objeví kolem launche |

Tabulky níže řadí podle **obsahové hodnoty** (kolik virálního materiálu tvůrce vyrobí). Do výroby jde tvůrce teprve s přístupem z tabulky výše.

Obsahová priorita: **S** = nejvíc virálního materiálu, **A** = silný druhý sled, **B** = doplněk nebo pozdější fáze.
Plán GTA 6: **potvrzeno** = sám řekl nebo napsala média; **pravděpodobně** = silná historie GTA V; **možná** = bez signálu.

### Priorita S

| Tvůrce | Platforma | Sledující (zdroj) | Plán GTA 6 | Styl | Klipování | Proč |
|---|---|---|---|---|---|---|
| IShowSpeed | YouTube @IShowSpeed, Twitch | YT 62,3 M; Twitch 5,4 M (Wiki, srpen) | potvrzeno (win.gg 26. 10.). Start na Novém Zélandu kvůli dřívějšímu času je jen fámou z fan účtu | chaos, křik | vlastní program nenalezen; sponzorská kampaň (Expedia) na Whop za 1 USD / 1 000 views; podle Bloomberg/TheWrap spolupracuje s agenturou Clipping | 3. nejsledovanější co-stream GTA 6 showcase (177 887 diváků), top anglicky. Klipy s jeho jménem: 1,08 mld. views na YT za 30 dní. Zároveň největší konkurence |
| Kai Cenat | Twitch kaicenat, YT @KaiCenat | Twitch 21,98 M (TT); YT 8,26 M | potvrzeno: slíbil 100% maraton GTA 6; od 5. 11. hraje GTA III–V „před šestkou“ | hype, emoce | program nenalezen; klipy dělají fan kanály | nejsledovanější Twitch kanál; klipy 558 M views YT + 128 M TikTok za 30 dní |
| Adin Ross | Twitch adinross + Kick | Twitch 7,12 M, ⌀ 141 tis. diváků (TT); Kick ~2,1 M | potvrzeno (win.gg 26. 10.); jeho ohlášený „GTA 6 RP server“ je bez PC verze sporný | IRL chaos | s N3onem staví clipping síť | nejvyšší živá sledovanost ze seznamu |
| Lacy + Clix + JasonTheWeen + Mar3lg | Twitch | Lacy 3,52 M, Clix 8,72 M, Jason 2,86 M (TT) | potvrzeno: maraton v mansionu v Miami + IRL „mise“ (win.gg 9. 10.); datum neuvedeno | skupinový chaos | neznámé | skutečné „Vice City“ + hra = ideální krátké klipy |
| penguinz0 / MoistCr1TiKaL | YT @penguinz0, Twitch | YT 18,3 M; Twitch 6,0 M (Wiki) | pravděpodobně (Rockstar ho v srpnu začal sledovat, hraje klasické GTA) | suchý komentář | historicky tolerantní ke klipům, pro 2026 neověřeno | hlášky se dají citovat |
| Jynxzi | Twitch jynxzi, YT | Twitch 11,75 M (TT); YT 7,13 M | pravděpodobně (48h maraton GTA V v červenci) | rage, křik | neznámé | reakce stavěné na klip, méně klipaři obsazený |

### Priorita A

| Tvůrce | Platforma | Sledující | Plán | Poznámka |
|---|---|---|---|---|
| CaseOh | Twitch caseoh_, YT | Twitch 9,22 M; YT 11,3 M | možná | vtipné reakce, málo klipařů (21,8 M views za 30 dní) |
| xQc | Twitch xqc + Kick | Twitch 12,65 M; Kick ~1,1 M | pravděpodobně (NoPixel) | Twitch mu kvůli Netflix záznamu kázal stáhnout VOD showcase; „xQc povoluje reuploady“ je **neověřené**, nespoléhat |
| StableRonaldo | Twitch | 5,35 M | pravděpodobně | patří k Lacyho partě |
| N3on | Kick | ~578 tis. (neověřeno, starší) | možná (postava v GTA 6 mu podobná) | **má placený clipping program**: ~1,4 M USD pro 303 klipařů za 5 týdnů, 0,40–0,50 USD / 1 000 views |
| Hasan | Twitch hasanabi | Twitch 3,13 M; YT 2,04 M | pravděpodobně (RP) | komentář |
| Typical Gamer | YT | 16,1 M | pravděpodobně | GTA V stálice |
| Sidemen / MoreSidemen | YT | 23,4 M / 9,75 M | pravděpodobně, plán nenalezen | UK; editovaná videa |
| Summit1g | Twitch | 6,42 M | pravděpodobně (RP legenda) | do příchodu PC hraje příběh |
| Asmongold | Twitch zackrawrr | 2,53 M, ⌀ 40 tis. | potvrzeno, na konzoli | rant komentář |

### Priorita B (doplněk)
Sodapoppin (9,02 M Twitch), shroud (11,3 M Twitch), LIRIK (3,0 M), TimTheTatman / Valkyrae / Fuslie / BrookeAB (Rockstar je v srpnu začal sledovat), Ludwig (7,1 M YT), AMP (Agent00, Fanum, Duke Dennis), Vanoss crew (VanossGaming 26,0 M, Nogla 7,41 M, Terroriser 3,93 M), DarkViperAU (2,34 M YT; glitche a speedrun od launche), GTA Series Videos (4,14 M) / MrBossFTW (3,19 M) / TGG (neověřeno) pro „věděli jste“ klipy, Ali-A, SSundee, Kwebbelkop, Sykkuno, RP veteráni Buddha, Ramee, Mr_K, Penta (jejich hlavní GTA 6 obsah přijde až s PC a RP).

### Co z toho plyne
- **Benchmark:** GTA 6 showcase na Netflixu (konec srpna) měl ve špičce ~3,97 M souběžných diváků přes 9 038 co-streamů; Twitch ~44 % sledovanosti, Kick ~20 % (Streams Charts). Prognóza rekordu na launch zveřejněná není.
- **Konkurence:** za 30 dní do 26. 8. udělalo ~1 820 klipařských účtů ~2 mld. views jen ze šesti největších streamerů (CreatorDB). Top kanály: Nexxo Recaps, deagzzzshorts, Stream Bellz, DaPlug, Stream Vortex, Goofy Recaps, Snickerly, Kai Cenat Live.
- **Mezera:** méně obsazení streameři (CaseOh, Jynxzi, Lacyho parta, Summit1g) a **český úhel** (titulky, komentář, kontext pro CZ/SK publikum). Na čistém anglickém přepostu Speeda a Kaie nevyhrajete rychlostí ani objemem.
- **Kick vs Twitch:** Kick v GTA V předběhl Twitch (červenec 2026: ⌀ 112 tis. vs 69 tis.), ale táhnou to arabští tvůrci a MENA RP servery. Pro anglické klipy je hlavní Twitch a YouTube.

### Kalendář launch týdne
- Teď: Kai hraje GTA III–V (sériový maraton).
- 18. 11. kolem 11:00 UTC: **pokud** platí fáma o Speedovi na Novém Zélandu, první virální klipy padnou tady.
- 19. 11.: launch, Kai 100% maraton, Adin, Asmongold, Lacyho parta (datum neuvedeno).
- Před launchem: na Whop hledat kampaně „GTA 6“; Twitch a Kick slibují dropy a akce bez detailů.

---

## 3. Nejrychlejší cesta ke klipu

### Proč ne jen přepis řeči
Herní virál je výkřik, náhlá akce, glitch nebo exploze chatu. AI clippery stavěné na podcasty (SupoClip, Klap, Vizard) čtou hlavně slova. U herního obsahu je spolehlivější brát signály, které už virálnost změřily.

### Signály virálnosti

| Signál | Co dává | Přístup | Poznámka |
|---|---|---|---|
| **Twitch Helix „Get Clips“** | klipy, které udělali diváci, s počtem zhlédnutí, `vod_offset`, délkou | zdarma, app token (client credentials) | nejlepší signál, jen Twitch; `vod_offset` je u živého vysílání `null` po neurčitou dobu a u smazaného VOD natrvalo → záloha: `created_at` klipu − začátek VOD; řadit podle **růstu** zhlédnutí, ne celkového čísla |
| **Rychlost chatu** | špičky zpráv a emotů (KEKW, OMEGALUL, LUL, W, LMAO, ?????) | živě anonymní IRC (`justinfan`), u VOD TwitchDownloaderCLI | z-skóre proti klouzavé 5min bázi po 10 s |
| **Hlasitost** | výkřiky, výbuchy | ffmpeg `ebur128` | levné, lokální, jen audio |
| **YouTube „Most replayed“** | `heatmap` přes `yt-dlp -J` (ověřeno lokálně) | bez klíče | jen 100 úseků na video (3h video ≈ 108 s), jen u videí s dost zhlédnutími |

### A. Živě během launch streamu (cíl 5–15 min od momentu k postu)
1. Každých 60 s se zeptat Helix API na nové klipy sledovaných streamerů od začátku streamu; souběžně hlídat rychlost chatu.
2. Když klip rychle roste, stáhnout ho (yt-dlp). Divácké klipy mají ≤ 60 s a už je ořízl člověk. ~10–20 s.
3. Přepis jen toho klipu lokálně: faster-whisper (`large-v3-turbo` int8) na RTX 3050. ~5–15 s, paměť 6 GB pravděpodobně stačí (neověřeno).
4. Render jedním průchodem ffmpeg s NVENC: facecam nahoře, hra dole, titulky vypálené. Souřadnice facecamu se nastaví jednou pro každého streamera (ukládat v procentech). Vstup se nejdřív srovná na 1920×1080, protože Twitch klipy chodí i v 720p, 936p nebo 1440p. Filtr (render otestován na tomto stroji, úvodní `scale` doplněn po oponentuře):
   ```
   ffmpeg -i clip.mp4 -filter_complex "[0:v]scale=1920:1080,split[a][b];[a]crop=480:270:1440:0,scale=1080:608[cam];[b]crop=888:1080:516:0,scale=1080:1312[game];[cam][game]vstack=inputs=2,subtitles=clip.ass[v]" -map "[v]" -map 0:a -c:v h264_nvenc -preset p5 -cq 21 -c:a aac -b:a 160k out.mp4
   ```
   ~10–20 s.
5. **Člověk zkontroluje (povinně):** háček v první vteřině, titulek, spoiler, hudba z rádia, právo k použití. 1–3 min.
6. Post ručně nebo naplánovat.

Záloha, když API vypadne: vlastní nahrávání živého HLS (streamlink / yt-dlp) do klouzavého bufferu a střih podle špičky chatu nebo hlasitosti.

### A2. Živě na YouTube (IShowSpeed) a Kicku (Adin Ross, N3on)
Divácké klipy přes API tu nejsou, signál si musíte vyrobit sami.
1. Nahrávat stream do klouzavého bufferu: `yt-dlp --live-from-start` (YouTube) nebo streamlink (YouTube i Kick), segmenty po 10 min.
2. Chat: YouTube živý chat přes chat-downloader nebo yt-dlp `--sub-langs live_chat` (neotestováno); Kick chat přes jejich websocket (neověřeno).
3. Špička chatu nebo hlasitosti → vyříznout okno ±40 s z bufferu → krok 3–6 jako výše.
4. Časový odhad 10–25 min od momentu (člověk musí sám najít začátek a konec, protože střih neudělal divák).

U YouTube navíc sledovat klipy, které YouTube „Clip“ funkcí udělali diváci a sdílejí je na X/Redditu; API pro ně není.

### B. VOD a YouTube dávka (4h VOD → 10–20 kandidátů za ~20–30 min, odhad)
1. Stáhnout jen audio + JSON chatu. 2–5 min.
2. Skóre špičky (výchozí váhy na ladění): 0,4 × chat + 0,3 × diváčtí klipy (podle `vod_offset`) + 0,2 × hlasitost + 0,1 × heatmapa. 1–3 min.
3. Top 30 oken ±45 s, přepsat jen ta (GPU). 2–5 min. Volitelně LLM vybere střih a háček z krátkého přepisu.
4. Stáhnout video jen pro ta okna (`yt-dlp --download-sections`), render jako v A4. 2–5 min + ~15 s / klip.
5. Člověk projde kandidáty. 10–20 min.

VOD stahovat hned: Twitch je drží 7 dní (bez statusu), 14 (Affiliate), 60 (Partner), a streamer je může kdykoli smazat. VODy jen pro odběratele bez přihlášení nestáhnete.

### Co poběží lokálně (RTX 3050 6 GB)
NVENC kódování (ověřeno), faster-whisper/WhisperX medium či turbo (pravděpodobně), detekce obličeje YuNet na CPU. Lokální LLM 7–8B ve 4 bitech se vejde, ale virálnost hodnotí hůř než levný cloudový model.

### Nástroje

| Nástroj | Hodí se na hry | Twitch | Facecam nahoře / hra dole | Cena (ověřit) |
|---|---|---|---|---|
| Vlastní skript (výše) | ano, bere signály | ano, i živě | ano, šablona na streamera | ~0 |
| SupoClip (self-host) | slabý výběr momentů | VOD ano, živě ne | jen facecam v horním rohu | AssemblyAI ~0,21 USD/h + LLM |
| OpusClip | střední (recenze: ~60 % použitelných na hrách vs 80–85 % na podcastech) | VOD do 10 h, živě ne | ano | Pro ~29 USD/měs. |
| StreamLadder | stavěný pro streamery | ano | ano, šablony | ~9–27 USD/měs. |
| Eklipse | nejlepší na gameplay (zvuk + herní události) | ano | ano | ~20–25 USD/měs.; podpora GTA 6 na launchi neznámá |
| zhouxiaoka/autoclip | na mluvené slovo | yt-dlp | ořez řečníka | zdarma + LLM klíč; `cookiesfrombrowser` nepoužívat |

Doporučení na týden: vlastní skript na živý den, Eklipse nebo StreamLadder na zkoušku na VOD jako srovnání. SupoClip až pro podcastový obsah.

---

## 4. SupoClip – verdikt

- Repo FujiwaraChoki/supoclip, AGPL-3.0, ~2 900 hvězd, poslední commit 12. 11. 2026. Kód čten lokálně, nic neinstalováno ani nespuštěno.
- Stack: Docker Compose (Next.js, FastAPI, ARQ/Redis workery, Postgres), REST API a MCP server.
- Přepis: výchozí AssemblyAI; jde přepnout na Whisper (openai-whisper) nebo YouTube titulky.
- LLM: libovolný přes pydantic-ai (Gemini, OpenAI, Anthropic, OpenRouter, Ollama).
- **Výběr momentů:** přepis se dělí na 12min okna, LLM vybírá 3–5 úseků z textu. Jediné nejazykové vodítko je 8 nejsilnějších zvukových špiček za celé video a jen pokud padnou do řeči. Žádný chat, žádné divácké klipy, žádná heatmapa. Na 4h streamu je to málo.
- 9:16: detekce obličeje YuNet; rozložení facecam nad hrou jen při malém stabilním obličeji v horním rohu.
- Délka: výchozí max 90 min (jde zvýšit). Živé streamy odmítá. Sub-only VODy odmítá.
- Telemetrie při self-hostu vypnutá, dokud nevyplníte klíče.
- AGPL: interní použití OK. Pokud upravenou verzi nabídnete jiným přes síť, musíte zveřejnit zdroják. Na samotné klipy se AGPL nevztahuje.
- **Verdikt:** dobrý renderer, špatný „oko na virál“ pro hry, těžký na rozjetí za týden. Převzít z něj nápady (YuNet, stahování jen vybraných úseků, časování titulků), ne celý stack.

---

## 5. Práva a pravidla

### Proč odkaz nestačí
- **Tři až čtyři vrstvy práv:** streamer vlastní facecam, hlas a komentář; Take-Two/Rockstar herní záběry; vydavatelství hudbu v rádiu. Navíc osobnostní práva: šířit podobu a hlas člověka komerčně lze jen s jeho svolením (§84–90 občanského zákoníku); zákonné výjimky na komerční přepost nedopadají. Žádost o svolení proto musí výslovně zahrnout i užití podoby a hlasu.
- **Svolení streamera ani kampaň nekryjí Rockstar a hudbu.** Rockstar povoluje videa jen „non-commercial“. Placená kampaň je komerční užití. Rockstar monetizovaná videa streamerů v praxi toleruje, ale písemně to nedovoluje a může je kdykoli stáhnout.
- **Česko (zák. 121/2000 Sb.):** uzavřený výčet výjimek, žádný fair use. §31 citace = přiměřené úryvky ve vlastním díle nebo kritice s uvedením zdroje; uvedení zdroje je podmínka, ne povolení. §29 třístupňový test: klipy konkurují streamerovým vlastním Shorts. §38g parodie/pastiš je úzká.
- **USA fair use:** 30–60s virál jen s popiskem je nejslabší případ.
- **Platformy nad rámec zákona:**
  - YouTube „reused content“: klipy bez příběhu či komentáře se nemonetizují; pravidlo platí „i když máte svolení původního tvůrce“; posuzuje se celý kanál. 3 copyright strikes za 90 dní = zrušení kanálu.
  - TikTok Creator Rewards: video ≥ 1 min a originální; kompilace cizích klipů a cizí vodoznak nic nevydělají. **Pro český účet program nejspíš vůbec není dostupný**: žádný nalezený seznam způsobilých zemí ČR neuvádí (nejčastěji USA, UK, Německo, Francie, Japonsko, Jižní Korea, Brazílie, Mexiko). Ověřit v TikTok Studiu.
  - Instagram/Meta: 10+ neoriginálních Reels za 30 dní = účet vypadne z doporučování; od 30. 4. 2026 i fotky a karusely; kredit ani vodoznak nestačí. Pomáhá vlastní voiceover, vlastní text, Collab post s tvůrcem.
  - Twitch: diváci mají licenci jen pro osobní použití.
- **Rockstar (policy z ledna 2025, čteno jen přes výňatky):** videa OK nekomerčně; konec hry, kompilace cutscén a samostatné cinematiky stahují; leaky stahují (DMCA na leaky GTA 6 v srpnu 2026). Licence: copyright@take2games.com. Že by GTA 6 klip kanály tolerovali, nikde nepsali.

### Matice rizik

| Co | Riziko | Opatření |
|---|---|---|
| Přepost klipu + kredit, monetizovaný | vysoké (strike, bez peněz, horší dosah) | jen přes kampaň nebo písemné svolení |
| Totéž bez monetizace | středně vysoké | totéž |
| Hraje rádio ve hře | vysoké (Content ID) | střihnout nebo ztlumit hudbu |
| Cutscény, konec, odhalení misí | vysoké | žádné spoilery jako samostatný klip |
| Leak / předčasné záběry | velmi vysoké | nikdy |
| Jméno nebo tvář tvůrce jako název či avatar účtu | vysoké (impersonace) | vlastní značka, tvůrce jen v popisku |
| Zavádějící titulek („Kai DIED“) | střední | titulek musí sedět |
| Placená kampaň bez označení | střední (zák. 40/1995 Sb.) | přepínač „placené partnerství“ + „reklama“ |
| Kampaň sponzorovaná kasinem (Stake) | vysoké v CZ (zák. 186/2016 Sb.) | nebrat, nebo právník |
| Skutečný komentář nebo recenze s krátkými úryvky | nižší, ne nulové | komentář pomáhá s monetizací na platformách; autorskoprávně ho kryje jen §31 v přiměřeném rozsahu; licence kde to jde |
| Kampaň nebo svolení streamera, monetizováno | nižší vůči streamerovi, **nekryté vůči Rockstaru** | bez spoilerů a rádia; pro větší objem licence od Take-Two |

### Doporučený provozní model
1. Firma (s.r.o. nebo IČO), ne soukromá osoba: smlouvy, daně, odpovědnost.
2. **Nejdřív oficiální kampaně:** Whop Content Rewards (tvůrce nastaví sazbu za 1 000 views, typicky ~0,5–1,5 USD, rozpětí 0,2–6), Vyro (MrBeast, hlášeno ~1,5–3 USD / 1 000, strop 1 000 USD/post), Kick Clipping Program, N3onův program, agentura Clipping (hlášeno 300–1 500 USD / 1 M views). Ke každé kampani uložit screenshot zadání jako doklad svolení. Kampaň, která chce poplatek za vstup, je podvod.
3. **Kde kampaň není, písemné svolení** (e-mail nebo DM tvůrci či managementu, uschovat odpověď). Obsah žádosti: název účtu a platformy, délka klipů 15–60 s, kredit + odkaz, zda monetizujete, souhlas s užitím podoby a hlasu, stažení na žádost do 24 h, žádné vydávání se za tvůrce, případně podíl z výnosu, výslovné „ano“.
4. **Vlastní přidaná hodnota v každém klipu:** český voiceover nebo komentář, kontext, vlastní titulky. Bez toho TikTok/YouTube/Meta nemonetizují a omezují dosah.
5. **GTA checklist u každého klipu:** bez rádia, bez konce a velkých cutscén, bez leaků, bez grafického násilí v prvních ~7 s Shorts (YouTube monetizace, hlášeno Dexertem).
6. Vlastní značka účtu; Collab nebo tag jen se souhlasem tvůrce.
7. Placený obsah označit (přepínač platformy + „reklama“ / „placená spolupráce“).
8. Hlídat claimy; po prvním strike zastavit a přezkoumat. Nikdy se nepřiblížit ke 3.

Právník (IP/média, CZ) je potřeba na: rozsah §31/§38g u komentovaných střihů, licenční smlouvy s tvůrci, komerční užití záběrů Rockstaru, kampaně s hazardním sponzorem, daně a DPH z výplat v USD.

Ekonomika pro představu: při ~1 USD / 1 000 views dělá klip s 500 tis. views ~500 USD, pokud kampaň má rozpočet a klip schválí. Klipy lze odmítnout i po dosažení views. TikTok sám českému účtu nejspíš neplatí (viz výše, ověřit), YouTube Shorts a Meta platí jen za originální obsah, takže hlavní příjem jsou kampaně.

---

## 6. Neověřeno / otevřené
- Fáma o Speedově startu na Novém Zélandu; přesné časy streamů 19. 11.
- Čísla YT pro xQc, StableRonaldo, Lacy, TGG, Duke Dennis; Kick čísla N3on, Ramee, Penta.
- Rockstar nepotvrdil režim pro streamery ani přepínač licencované hudby v GTA 6.
- Rychlost a paměť faster-whisper na RTX 3050; přesné přepínače TwitchDownloaderCLI.
- Ceny OpusClip, StreamLadder, Eklipse; podpora GTA 6 v Eklipse.
- Zda Stake má českou licenci.
- Oficiální seznam zemí TikTok Creator Rewards (ČR nejspíš ne).
- Zda agentura Clipping (IShowSpeed) bere externí klipaře.
- „Gone gold“ stojí na jednom earnings callu a rozhovoru s IGN (Push Square); oficiální oznámení Rockstaru nenalezeno.
- Živý chat YouTube a Kick: nástroje neotestovány.

## Zdroje
- Datum: insider-gaming.com/gta-6-delayed-again-release/, pushsquare.com/news/2026/11/gta-6-wont-have-any-last-minute-delays-as-its-officially-gone-gold
- Tvůrci: twitchtracker.com/channels/ranking/english, en.wikipedia.org/wiki/List_of_most-followed_Twitch_channels, win.gg/lacy-plans-gta-6-marathon/, win.gg/eddie-craven-claims-kick-is-the-home-of-gta-streaming/, streamer.guide/blog/kai-cenat-gta-marathon-2026, sportskeeda.com/us/streamers/news-kai-cenat-hold-gta-6-100-playthrough-marathon-upon-game-s-release, gamingamigos.com/post/ishowspeed-plans-new-zealand-trip-gta-6-stream, gtaboom.com/rockstar-games-quietly-followed-a-small-army-of-streamers-888d, rockstarintel.com/gta-6-gameplay-reveal-had-nearly-4-million-live-viewers-as-it-broke-netflix/, creatordb.app/creator-news/the-rise-of-the-clip-economy/, dexerto.com/gaming/xqc-furious-after-being-forced-to-take-down-gta-6-vod-by-twitch-3403752/, dexerto.com/gta/twitch-ceo-says-gta-6-online-is-launching-next-year-after-speaking-to-rockstar-3409995/, win.gg/gta-livestreaming-viewership-trends-kick-twitch/
- Pipeline: github.com/FujiwaraChoki/supoclip, dev.twitch.tv/docs/api/reference/clips#get-clips, github.com/lay295/TwitchDownloader, opus.pro/tools/gaming-video-editor
- Práva: support.rockstargames.com/articles/200153756, support.google.com/youtube/answer/1311392, support.google.com/youtube/answer/2814000, tiktok.com/legal/page/global/tiktok-creator-rewards-program-eea/en, tubefilter.com/2026/04/30/instagram-removes-algorithm-recommendations-repost-content-aggregator/, about.fb.com/news/2026/03/rewarding-original-creators-on-facebook/, zakonyprolidi.cz/cs/2000-121, epravo.cz/top/clanky/spoluprace-s-influencery-na-co-si-dat-pozor-120427.html, docs.whop.com/manage-your-business/growth-marketing/content-rewards, tubefilter.com/2025/10/14/mrbeast-vyro-clipping-platform-viewstats-expansion/, thewrap.com/media-platforms/tv/creators-rise-of-clipping-explained/, dexerto.com/gta/youtube-updates-gaming-violence-monetization-rules-ahead-of-gta-6-launch-3405945/
