# Hudba pro účet gta6fanpageclip – prompty pro Gemini

Hotové soubory ulož do `assets/hudba/fanpage/` jako `fanpage_<nazev>.mp3` (render bere `assets/hudba/fanpage/*.mp3`, viz `brands.json` → `fanpage.music`). Když složka zůstane prázdná, klipy fanpage půjdou bez hudby. Hudbu bananas render nepřebírá.

Fanpage musí znít jinak než bananas. Bananas má temný, zpomalený „villain“ phonk kolem 75 BPM (`docs/phonk-prompt.md`). Fanpage je rychlá, světlá a čistá: drift/Brazilian funk a hype trap beat pro edity.

Pravidla pro oba prompty:
- jen instrumentál, Gemini jinak přidává hlas (proto výčet „no vocals, no voice, no humming, no chants, no vocal samples“ přímo v promptu),
- čistý mix bez praskání desky, šumu pásky a klipování, špičky kolem −3 dB,
- smyčka: bez intra a bez fade-outu,
- žádná jména interpretů ani skladeb (riziko Content ID).

Když výsledek přesto obsahuje hlas nebo výkřiky, vygeneruj znovu, nestříhej to ručně.

## 1) Drift funk (hlavní)

```
Energetic drift funk instrumental for fast car and action edits, 130 BPM, bright and driving. Punchy Brazilian funk kick pattern with a bouncy syncopated groove, a tight clean 808 bass, crisp claps and snappy shakers, and a short catchy synth-bell hook that repeats every bar. Fast, playful and confident, like a high-speed chase in the sun. Strictly instrumental: no vocals, no voice, no humming, no chants, no vocal samples, no spoken words. Clean modern mix: no vinyl crackle, no tape hiss, no distortion on the master, no clipping, peaks around -3 dB. Leave the midrange a little open so speech on top stays clear. Starts at full energy on the first beat, no intro, loops seamlessly, no fade-out.
```

## 2) Hype trap edit beat

```
Hype trap instrumental for a sports-style highlight edit, 145 BPM with a half-time feel. Big clean 808 with short slides, hard crisp snares, fast rolling hi-hats with triplet fills, and a bold bright brass-and-synth stab melody that builds tension and hits on the downbeat. Triumphant, loud and confident, made for a big moment and a replay. Strictly instrumental: no vocals, no voice, no humming, no chants, no vocal samples, no ad-libs, no spoken words. Clean polished mix: no vinyl crackle, no tape hiss, no lo-fi effects, no clipping, peaks around -3 dB. Keep space in the midrange for speech. Starts immediately at full energy, no intro, loops seamlessly, no fade-out.
```

Poznámky:
- Ověř si v podmínkách Gemini, jestli smíš vygenerovanou hudbu použít na sítích a monetizovat.
- Stejnou skladbu nedávej do obou účtů; fanpage bere jen soubory ze své složky.
