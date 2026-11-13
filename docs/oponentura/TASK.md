# Zadání (doslova od uživatele)

"chci aby jsi udelal analyzu vsech anglickych influenceru, kteri budou hrat a natacet a streamovat gta 6 zaroven udelal analyzu toho jak z toho udelat co nejrychleji klipy na socialni site, nasel jsem na githubu supoclip, ale potrebuju aby jsi udelal analyzu vsech nejvetsich streameru nebo influenceru kteri budou nejak natacet gta 6, my pak vezeme jejich obsah a udelame z toho klipy, skrze tebe, jejich nejviralnejsi momenty, samozrejme v popisku budeme odkazovat na ne, aby to bylo cisty, jdi na to"

# Kontext
- Dnes 2026-11-12. GTA 6 vychází 2026-11-19 (PS5, Xbox Series; PC bez data). Take-Two hlásí "gone gold".
- Uživatel: malý český tým (CZ, EU). Chce brát obsah anglických streamerů/YouTuberů, stříhat virální momenty do 9:16 klipů (TikTok/Shorts/Reels), v popisku uvést zdroj. Věří, že odkaz = čisté.
- Stroj: Windows 11, RTX 3050 6 GB, ffmpeg, yt-dlp.
- SupoClip = github.com/FujiwaraChoki/supoclip (AGPL-3.0), naklonováno read-only do C:\Ales\Projekty\cizi\FujiwaraChoki\supoclip (SHA 5c99c18).
- Podklady: tři rešerše (tvůrci, SupoClip+pipeline, práva). Shrnuty v analýze.

# Výstup
Soubor `gta6-klipy-analyza.md` (česky) – analýza pro tým.

# Akceptační kritéria
1. Seznam největších anglických tvůrců, kteří budou GTA 6 streamovat/natáčet, s platformou, čísly (datovanými, se zdrojem), stavem plánu (potvrzeno/pravděpodobně/možná) a prioritou pro klipy.
2. Analýza nejrychlejší cesty od streamu ke klipu: konkrétní pipeline s časy, nástroje, co běží lokálně.
3. Verdikt na SupoClip: co umí, co neumí pro herní streamy, náklady, licence.
4. Pravdivé posouzení tvrzení "odkaz v popisku = čisté" a legální provozní model (CZ/EU právo, pravidla platforem, Rockstar).
5. Žádná vymyšlená čísla; neověřené věci označené.
6. Prakticky použitelné do launch dne (19. 11.).
