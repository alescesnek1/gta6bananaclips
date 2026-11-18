# Stories: prompty na obrázky (ChatGPT / Gemini)

Pool pro `stories.py`: **20 univerzálních obrázků na účet**, které se točí dokola (stejný obrázek nejdřív po 5 dnech, záznam v `stories_state.json` → `pool`). Obrázky nejsou vázané na konkrétní den ani klip, takže vydrží celý launch.

## Postup
1. Do ChatGPT (nebo Gemini) přilož referenční obrázky uvedené u účtu a vlož společný úvod + jeden prompt. Jeden obrázek na zprávu.
2. Formát vždy **na výšku 9:16** (1080×1920 nebo víc).
3. Stažené obrázky naimportuj:
   ```
   .venv\Scripts\python tools\stories_import.py bananas C:\Users\Ales\Downloads\banan1.png C:\Users\Ales\Downloads\banan2.png
   .venv\Scripts\python tools\stories_import.py fanpage C:\Users\Ales\Downloads\fan*.png
   ```
   Skript je ořízne na 1080×1920, smaže metadata a uloží jako `assets/stories/<účet>/<účet>_NN.jpg` (max 20). Výměna jednoho: `--nahradit 07`.
4. Commitni `assets/stories/` (GitHub Actions je bere odtud).

## Pravidla pro všechny obrázky
- **Žádný text, loga ani vodoznaky v obrázku** (AI písmo vypadá jako slop; logo GTA/Rockstar = ochranná známka). Text do stories dává `stories.py` sám.
- Žádní skuteční streameři ani celebrity (práva k podobě). Jen náš maskot a naše modelka.
- Prostředí „inspirované Vice City“ (Miami, neon, palmy, sporťáky, pláž, noc), ne screenshot ze hry.
- Fanpage: svůdné, ale bezpečné pro reklamu a pravidla Instagramu: plavky nebo oblečení, žádná nahota, žádné explicitní pózy, nic, co by působilo nezletile.
- Dolních ~340 px a horních ~250 px obrázku zakrývá rozhraní Instagramu: hlavní motiv drž uprostřed.

---

## gta6bananaclips (bananas): 20 promptů

**Přilož:** `assets/brand/pfp_bananas.webp` (banánový maskot s kšiltovkou, brýlemi a zlatým řetězem + blondýna).

**Společný úvod (vlož před každý prompt):**
> Use the attached image as the character reference: the same cartoon banana mascot (backwards cap, sunglasses, gold chain, floral shirt) in the same bold, saturated cartoon illustration style. Vertical 9:16 poster composition, 1080x1920, main subject in the middle of the frame, Vice City inspired 1980s Miami neon palette (hot pink, cyan, sunset orange). No text, no letters, no logos, no watermark, no real people.

1. The banana mascot on a sunset beach boardwalk, jaw dropped, sunglasses sliding down his face, as if he just saw something unbelievable on his phone.
2. The banana mascot behind the wheel of a pink 80s sports convertible at night on a neon-lit highway, laughing hard, one hand on the wheel.
3. The banana mascot sitting on a couch with a game controller, leaning forward in total focus, face lit by a TV glow, living room with neon window light.
4. The banana mascot holding a golden scorecard with a big banana symbol on it (no numbers), proudly raising it like a judge at a contest, palm trees behind.
5. The banana mascot facepalming on a rooftop at night, city skyline with neon towers behind him.
6. The banana mascot running from a police car with flashing lights down a palm-lined street, cartoon motion lines, panicked but funny.
7. The banana mascot and the blonde woman from the reference taking a selfie on a yacht at golden hour, both making surprised faces.
8. The banana mascot wearing headphones in a gaming chair, screaming with excitement, arms in the air, RGB lights around.
9. The banana mascot on a jet ski jumping a wave, water spray, sunset, huge grin.
10. The banana mascot eating popcorn in a movie-theater style seat, eyes wide, watching a huge glowing screen (screen shows only light, no image).
11. The banana mascot standing in front of a wall of TV screens all glowing different neon colors, arms crossed, smug face.
12. The banana mascot slipping on a banana peel on a shiny wet neon street at night, frozen mid-air, comic style.
13. The banana mascot holding a stack of cash in one hand and a controller in the other, standing on the hood of a lowrider.
14. The banana mascot pointing straight at the viewer with a cheeky grin, close-up, palm tree shadows on a pink wall.
15. The banana mascot as a cartoon referee with a whistle, holding up a yellow card, beach volleyball court at sunset.
16. The banana mascot crying with laughter, rolling on a pool float in a neon-lit swimming pool at night.
17. The banana mascot peeking over sunglasses in disbelief, extreme close-up, reflection of fireworks in the lenses.
18. The banana mascot and the blonde woman from the reference on a motorbike, wind in their hair, ocean drive at night.
19. The banana mascot sitting on top of a giant pile of bananas like a throne, crown on his cap, neon sunset sky.
20. The banana mascot waving goodbye from the back of a speeding cartoon getaway car, cash bills flying out, playful mood.

---

## gta6fanpageclip (fanpage): 20 promptů

**Přilož:** `assets/brand/pfp_fanpage.webp` (modelka, selfie v Miami) a `assets/model_reakce/wink.png` (stejná modelka, postava do pasu).

**Společný úvod (vlož před každý prompt):**
> Use the attached photos as the identity reference: the same young adult woman (long wavy blonde hair, hoop earrings, tanned skin, pink palm-print bikini or stylish outfit as described). Photorealistic, shot like a high-end lifestyle photo on a phone or 35 mm camera, natural skin, vertical 9:16, 1080x1920, subject in the middle of the frame. Vice City inspired Miami at night or golden hour, neon pink and cyan light. Tasteful and flirty but safe for Instagram ads: no nudity, no lingerie, no explicit pose. No text, no letters, no logos, no watermark, no other identifiable people.

1. She sits cross-legged on a white sofa holding a game controller, looking over her shoulder at the camera with a playful smile, pink neon light from the window.
2. Rooftop pool at night, she leans on the edge in her pink palm-print bikini, city skyline glowing behind her, relaxed confident look.
3. She sits on the hood of a cherry-red 80s sports car on an empty neon street, wearing a cropped white jacket and denim shorts, winking.
4. Close-up selfie in a gaming chair with headphones around her neck, RGB lights behind, biting her lip in concentration.
5. Golden hour on a yacht deck, oversized white shirt over the bikini, wind in her hair, laughing at the camera.
6. She holds a controller and blows a pink bubble gum bubble, sitting on the floor in front of a glowing TV (screen shows only light).
7. Ocean Drive at night, she walks toward the camera in a pink satin slip dress and sneakers, neon signs blurred behind (no readable text).
8. Mirror selfie in a stylish apartment with palm-shadow light, wearing a fitted pink sports set, phone covering part of her face.
9. She lies on her stomach on a bed with a controller, feet up, looking at the camera with a teasing smile, warm neon glow, fully clothed in shorts and a crop top.
10. Beach at sunset, she sits on a lifeguard tower in the pink bikini and a sheer sarong, looking over her sunglasses.
11. Inside a retro arcade with neon lights, she leans against a cabinet in a denim jacket, popping a hip, confident smirk.
12. Night drive selfie from the passenger seat of a convertible, hair blowing, city lights streaking behind.
13. She sits on a kitchen counter eating popcorn, wearing an oversized gaming hoodie, laughing hard at something off camera.
14. Balcony at blue hour, she holds a cocktail with a tiny umbrella, wearing a pink bikini top and white linen pants, palm trees and skyline behind.
15. Close-up, she lowers heart-shaped sunglasses and raises an eyebrow, pink neon reflection on her face.
16. Poolside at night, she sits on a float in the pink bikini holding a controller up like a trophy, pool lights glowing cyan.
17. Motorbike parked under neon lights, she sits sideways on it in a leather jacket and shorts, looking back at the camera.
18. She lies on a couch with her head upside down off the edge, playing on a controller, hair spilling to the floor, playful chaos, casual clothes.
19. Sunset jog on the beach in a pink sports set, mid-stride, glancing at the camera with a smile.
20. Late night on a hotel bed, she yawns and stretches with the controller still in hand, soft pink lamp light, cozy pajama shorts and tank top.
