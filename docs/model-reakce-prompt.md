# Modelka fanpage – prompty (jednorázově)

Všechno generuješ s referencí profilovky `assets/brand/pfp_fanpage.webp` (nahraj ji ke každému promptu). Modelka je v bikinách (Aleš 17. 11.). Pozor: u long-formu na YouTube to může omezit reklamy, u shortů je to v pořádku. Žádná nahota ani explicitní pózy, jinak sítě omezí dosah.

## 1) Reakce – jeden prompt na všech 6 (mřížka)
Vygeneruje jeden obrázek s 6 reakcemi v mřížce 3 × 2. Ulož ho jako `assets/model_reakce/mrizka.png` a spusť:
```
python tools/model/klicuj.py --mrizka assets/model_reakce/mrizka.png
```
Skript mřížku rozřeže na `gasp, laugh, wink, shocked, smirk, eyeroll`, odstraní zelené pozadí a uloží průhledná PNG.

```
Use the attached photo as the exact same woman in every panel: same face, same hair, same skin tone, same makeup, same body. Create ONE image: a clean 3x2 grid of six equal panels (3 columns, 2 rows), thin white gaps between panels, landscape 3:2. In every panel she is shown waist-up, facing the camera, in a stylish Vice City bikini (hot pink with gold details), bright even studio light, sharp focus, photorealistic, on a solid pure green background (#00FF00) with no shadows and nothing else in the panel. Same framing and size in every panel. Expressions, left to right, top row first:
1) shocked gasp, mouth wide open, eyes wide, one hand on her cheek
2) laughing hard, head tilted back a little, eyes squeezed, hand near her mouth
3) playful wink with a confident half smile
4) jaw dropped in disbelief, both hands on her head
5) teasing smirk, one eyebrow raised, chin slightly down
6) dramatic eye roll, unimpressed, arms crossed
No text, no numbers, no logos.
```

## 2) Úvodní snímek outra (start frame pro video generátor)
Nejdřív vygeneruj obrázek, pak z něj video (krok 3). Tak bude obličej v obou verzích stejný.

**Na výšku (9:16):**
```
Use the attached photo as the exact same woman: same face, same hair, same skin tone, same makeup, same body. Vertical 9:16 photo, waist-up, centred, looking straight into the camera with a confident smile. She wears a stylish Vice City bikini (hot pink with gold details). Golden-hour Vice City beach boulevard behind her: palm trees, pastel art-deco buildings, pink neon signs, soft warm backlight. Photorealistic, sharp, natural skin, shallow depth of field. No text, no logos.
```

**Na šířku (16:9):**
```
Use the attached photo as the exact same woman: same face, same hair, same skin tone, same makeup, same body. Cinematic 16:9 landscape photo. She stands on the left third of the frame, waist-up, looking straight into the camera with a confident smile, wearing a stylish Vice City bikini (hot pink with gold details). Golden-hour Vice City beach boulevard behind her: palm trees, pastel art-deco buildings, pink neon signs, soft warm backlight. Keep the right half of the frame calm and uncluttered (space for end-screen elements). Photorealistic, sharp, natural skin, shallow depth of field. No text, no logos.
```

## 3) Video outra z úvodního snímku (image-to-video)
Nahraj úvodní snímek z kroku 2 jako první frame. 6 s, se zvukem/hlasem.

**Na šířku (16:9), pro long-form** → ulož jako `assets/outro/fanpage_outro_16x9.mp4`:
```
Start from the attached image, keep her exact face and look. 6 seconds, slow cinematic camera push-in. She looks into the camera, smiles, and says clearly in a warm, playful American voice: "If you want to see more, follow us." Then she winks and gives a small wave. Gentle breeze in her hair, palm leaves moving, neon signs flickering softly. Natural lip sync, photorealistic, no text on screen, no music.
```

**Na výšku (9:16), volitelně nové outro pro shorty** → ulož jako `assets/outro/fanpage_outro.mp4` (pak spustit `python tools/follow/outro_cap.py`, vypálí titulky):
```
Start from the attached image, keep her exact face and look. 5 seconds, slow push-in. She looks into the camera, smiles and says in a warm, playful American voice: "Want more? Follow us." Then she winks. Gentle breeze in her hair, palm leaves moving. Natural lip sync, photorealistic, no text on screen, no music.
```

Program na outro na šířku sám přidá jméno účtu a místo pro koncové prvky YouTube na stranu, kde není její obličej. Když soubor chybí, long-form skončí neonovou kartou.
