# Modelka fanpage – prompty (jednorázově)

Postup: **ChatGPT** vygeneruje úvodní snímky (přilož profilovku `assets/brand/pfp_fanpage.webp`), **CapCut AI video** z nich udělá videa. Modelka je v bikinách (Aleš 17. 11.). U long-formu na YouTube to může omezit reklamy, u shortů je to v pořádku. Žádná nahota ani explicitní pózy.

## 1) ChatGPT – úvodní snímek NA VÝŠKU (pro grimasy, zelené pozadí)
Přilož profilovku. Zelené pozadí je nutné, program ho pak z videa vystřihne.
```
Use the attached photo as the exact same woman: same face, same hair, same skin tone, same makeup, same body. Vertical 9:16 photo, waist-up, centred, facing the camera with a relaxed neutral smile. She wears a stylish Vice City bikini (hot pink with gold details). Bright even studio light, sharp focus, photorealistic, natural skin. Solid pure green background (#00FF00), evenly lit, no shadows on the background, nothing else in the frame. Leave some green space around her head and arms. No text, no logos.
```

## 2) ChatGPT – úvodní snímek NA ŠÍŘKU (pro outro)
Přilož profilovku.
```
Use the attached photo as the exact same woman: same face, same hair, same skin tone, same makeup, same body. Cinematic 16:9 landscape photo. Golden-hour Vice City beach boulevard: palm trees, pastel art-deco buildings, pink neon signs, warm backlight, wet reflective sidewalk. She is walking towards the camera on the left side of the frame, full body to knees visible, wearing a stylish Vice City bikini (hot pink with gold details), confident smile. Keep the right half of the frame calm and uncluttered. Photorealistic, sharp, natural skin, shallow depth of field. No text, no logos.
```

## 3) CapCut – video NA VÝŠKU: všechny grimasy (z obrázku 1)
Vlož obrázek 1 jako první snímek. Délka 10 s (když CapCut dovolí jen 5 s, rozděl na dvě videa po 3 grimasách).
```
Start from this image and keep her exact face, body and the pure green background. Static camera, no zoom, no camera movement, the background stays solid green the whole time. 10 seconds. She performs six quick facial reactions in this exact order, each about 1.5 seconds, briefly back to a neutral smile between them: 1) shocked gasp with a hand on her cheek, 2) laughing hard, 3) playful wink, 4) jaw dropped in disbelief with both hands on her head, 5) teasing smirk with one eyebrow raised, 6) dramatic eye roll with arms crossed. Expressive, natural movement, photorealistic, no text.
```
Ulož jako `assets/model_reakce/grimasy.mp4` (dvě videa: `grimasy1.mp4`, `grimasy2.mp4`) a spusť:
```
python tools/model/klicuj.py --video
```
Skript video rozdělí na reakce, odstraní zelené pozadí a uloží průhledné obrázky i krátká průhledná videa.

## 4) CapCut – video NA ŠÍŘKU: outro, přijde a promluví (z obrázku 2)
Vlož obrázek 2 jako první snímek. Délka 6–8 s.
```
Start from this image and keep her exact face and look. She walks a few steps towards the camera along the boulevard, stops on the left third of the frame, looks straight into the camera, smiles and says: "If you want to see more, follow us." Then she winks and gives a small wave. Slow cinematic camera push-in, gentle breeze in her hair, palm leaves moving, neon signs flickering softly. Natural lip movement, photorealistic, no text on screen.
```
Ulož jako `assets/outro/fanpage_outro_16x9.mp4`. Když CapCut nepřidá hlas: v CapCutu text na řeč (ženský americký hlas) + synchronizace rtů, jinak nech beze slov a program větu přidá jako titulky.

Program na outro sám přidá jméno účtu a místo pro koncové prvky YouTube na stranu, kde není její obličej.
