# Dva účty – společné zadání (kontrakt mezi render / publikací / infrastrukturou)

Aleš 17. 11.: druhý účet **gta6fanpageclip** na všech 6 sítích (vlastní 2 Buffer účty → `BUFFER_API_KEY_3`, `BUFFER_API_KEY_4`). Bez banánů, jiné titulky, podobný typ účtu. Účty dostávají **různé klipy**; **velmi virální klip jde na oba**, ale každý ve viditelně jiné verzi, aby žádná síť nevyhodnotila kopii. Netlify ≤ 1000 kreditů měsíčně (nejvýš 1500). Rychlost od virálního momentu k postu je priorita.

## Konfigurace: `brands.json` (kořen repa)
- `bananas` = stávající účet (chování se nesmí změnit), `fanpage` = nový.
- Pole: `name`, `buffer_keys`, `channels` (soubor kanálů Bufferu), `published` (záznam publikovaného), `short` (název výstupního souboru ve složce klipu), `banana`, `score_card`, `music` (cesty, glob povolen; neexistující → záloha na hudbu druhé značky NE, raději bez hudby), `caption` (font, velikost, barvy v RGB hex, obrys, stín, velká písmena), `grade` (ffmpeg filtr barev herního pásu), `hook` (`flash` = hook z pointy + bílý záblesk se zvukem; `cut` = hook z pointy + tvrdý střih s krátkým „whip“ zoomem, bez záblesku), `punch` (síla střídavého zoomu), `crop_shift` (posun výřezu hry v podílu šířky výřezu, aby se snímky lišily), `watermark`, `ramp`, `max_per_day`, `top3`.
- `both_score`: klip s viralitou (`virality()` / `score`) ≥ této hodnoty jde na oba účty.
- `netlify_gb_soft` / `netlify_gb_hard`: měsíční přenos Netlify (součet obou účtů, odhad MB × 7). Nad soft: oba účty bez extra videí nad povinnou kvótu a bez Top 3. Nad hard: `fanpage` přestane publikovat úplně, `bananas` jen povinná kvóta.

## Rozdělení klipů (render, `klipuj.py`)
- Každý vyrenderovaný klip dostane v `meta.json` pole `"brands": [...]` a `"shorts": {"bananas": "<cesta>", "fanpage": "<cesta>"}` (jen pro značky, kterým patří).
- Pravidlo: `virality(clip) >= both_score` → obě značky. Jinak střídavě (round robin) – stav `state.json` → `"rr"` (poslední značka). Live klipy (`live: True`) se spike_z ≥ 10 → obě.
- Pro každou značku samostatný finální encode se stylem značky (titulky, barvy, hook, punch, crop_shift, hudba, vodoznak, banán ano/ne, karta skóre ano/ne). Přepis (Whisper), tracking a facecam se počítají jednou.
- Výstup značky `bananas` zůstává `short_9x16.mp4` (zpětná kompatibilita), `fanpage` → `short_fanpage.mp4`.
- YouTube schránka (worker PC, `publikuj.py schranka`) a live-inbox přenesou `meta` včetně `brands` a obou souborů (`file` pro bananas, `file_fanpage` pro fanpage).

## Publikace (`publikuj.py`)
- Všechny příkazy dostanou `--znacka bananas|fanpage` (výchozí `bananas`). `kanaly --znacka fanpage` uloží `buffer_fanpage.json` s klíči 3/4.
- `den --znacka X`: kandidáti jen klipy, které mají `shorts[X]`; záznam do `published` souboru značky; kvóta z `ramp`/`max_per_day` značky; horká cesta per značka; Top 3 jen kde `top3: true`.
- Popisky značky `fanpage`: vlastní háčky a výzvy (bez banánů a banánového skóre), hashtagy `#gta6 #gta6clips` + rotace; SlopMonster lint 5/5. Žádné `@` zmínky.
- Netlify rozpočet: `month_gb()` sečte oba `published` soubory.

## Workflow (`.github/workflows/denne.yml`, `live.yml`) a worker
- `denne.yml`: secrets `BUFFER_API_KEY_3`, `BUFFER_API_KEY_4`; po renderu `publikuj.py den --znacka bananas` a `publikuj.py den --znacka fanpage`; commit stavu včetně `publikovano_fanpage.json`, `buffer_fanpage.json`; první běh `kanaly --znacka fanpage`, když chybí `buffer_fanpage.json`.
- `live.yml` beze změny publikace (dispatch denne), jen render obou verzí podle pravidla.
- Hudba fanpage: `assets/hudba/fanpage/` (Aleš vygeneruje podle `docs/fanpage-hudba-prompt.md`).
