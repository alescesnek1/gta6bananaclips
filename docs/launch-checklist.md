# Launch checklist (GTA 6, 19. 11. 2026)

Stav k noci 17./18. 11. Testy proběhly na klipech z GTA V. Na skutečných záběrech GTA 6 to ověří až první dny po launchi.

## Hotovo a ověřené
| Oblast | Stav | Jak ověřeno |
|---|---|---|
| Shorty bananas (hook v2, banán, karta skóre, zlatá 10/10, čip ligy) | ✅ | ostré rendery, QA, kontaktní archy |
| Shorty fanpage (reakce modelky jako videa, Heat 1–5, outro + Follow) | ✅ | ostré rendery (xqc, kaicenat, neverossa) |
| Facecam jen skutečný (postava ze hry se nebere) | ✅ | regresní test 10/10 (`tools/test_facecam.py`) |
| Stejná hlasitost shortů (−14 LUFS) a long-formu (±1,5 LU mezi momenty) | ✅ | měření na renderech |
| Nudné klipy nevyjdou (mrtvé místo > 2,5 s, slabá pointa) | ✅ | brána nudy na poolu |
| Plánování just in time (horizont 3 h, dohánění minima, horká a live cesta kdykoli) | ✅ | 50 testů, simulace celého dne |
| Rychlá live cesta se zámkem publikace | ✅ | ostrý běh na GitHubu 18. 11. 00:34 (zámek vzat i uvolněn) |
| Horká cesta skutečně publikuje (bez příznaku konceptu) | ✅ | opraveno v auditu, test |
| X/Threads max 3 posty denně (napříč běhy) | ✅ | test |
| Žádný klip na obou účtech | ✅ | test |
| Kvóty: 19.–25. 11. min 5 (nad 5 jen silné, max 10 / fanpage 8), od 26. 11. min 8, bananas od 3. 12. 10 | ✅ | `den --zkouska` |
| Od 19. 11. bez reakcí na trailery, jen gameplay | ✅ | test |
| Long-form: 3× týdně na kanál, e-mail den předem s kanálem a časem | ✅ | ukázky, QA |
| Long-form bananas: tier list, liga ze záběrů, odpočet, žlutá závěrečná karta | ✅ | ukázky |
| Long-form fanpage: příběh, After Dark, outro s modelkou na šířku, profilovka vlevo | ✅ | ukázky |
| Hudba: 15 skladeb CC0 + 4 vlastní, fanpage shorty už bez nepotvrzené licence | ✅ | `assets/hudba/long/sources.json` |
| Pokusné koncepty (multi-POV, příběh, kdo první), zapnou se podle materiálu, měří zhlédnutí z Bufferu | ✅ | 22 testů, dry-run |
| Tier list ve shortech vypnutý (jen long-form) | ✅ | `brands.json` `exp_tier3: false` |
| Hlídač live chatu se po výpadku znovu připojí | ✅ | test |
| Momenty z YouTube z PC do long-formu | ✅ | suchý běh schránky |

## Ráno 18. 11. zkontrolovat (2 min)
1. GitHub → Actions: běhy `denne` zelené (od 18. 11. 00:30 běží s novým plánováním).
2. Buffer: na obou účtech jsou naplánované posty jen na pár hodin dopředu (to je správně, sloty se plní průběžně).
3. E-mail: nic nepřišlo, první long-form e-mail přijde v noci z 19. na 20. 11.

## 19. 11. (launch)
1. Kolem poledne: první GTA 6 klipy na obou účtech, různé klipy, hook z pointy.
2. Večer: horké klipy vycházejí hned (ne až druhý den).
3. První live klip z výbuchu chatu: má vyjít do ~10 min.
4. Kdyby se cokoli pokazilo: vypínače v `brands.json` (`hook_v2`, `golden10`, `heat`, `model_sticker`, `league`, `league_chip`, `qa`, `exp_*`, `long_flags`) jdou vypnout bez změny kódu.

## Co ještě nikdo neviděl (ověří až GTA 6)
- Prahy nudy, kategorií a podobnosti jsou nastavené na GTA V; čísla každého klipu jsou v `meta.json` (`bore`, `qa`).
- Hudbu v databázi vybralo měření, nikdo ji neposlouchal: co zní špatně, vyřadit `python tools/long/hudba_db.py vyber --bez <id>`.
- Multi-POV potřebuje stejné repliky postav ze hry u víc streamerů.
- Live klipy můžou vyčerpat denní kvótu brzy; ladí se `hot_room` nebo práh `spike_z` v `sources.json`.
- Ruční `publikuj.py den` z PC v launch dny nepouštět (bere jen horizont a nebere zámek).

## Úklid (volitelné)
`out/_test_cam` (124 MB, cache testu facecamu), `out/_hudba_cache` (237 MB, kandidáti hudby), složky v `out/_pool` ze simulací. Nic z toho není v gitu.
