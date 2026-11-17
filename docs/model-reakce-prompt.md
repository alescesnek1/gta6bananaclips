# Reakce modelky fanpage – prompty pro Gemini (jednorázově)

Na co: krátké tiché „samolepky“ modelky v hooku a na pointě fanpage shortů (jako banán u bananas). Vygeneruješ je jednou, pak je program používá sám.

## Postup
1. Otevři Gemini (nebo ChatGPT), nahraj profilovku `assets/brand/pfp_fanpage.webp` jako referenci.
2. Pro každou reakci níže vlož společný začátek + řádek reakce. Vygeneruj, vyber nejlepší.
3. Ulož jako PNG do `assets/model_reakce/` s názvem podle reakce (`gasp.png`, `laugh.png`, …).
4. Spusť `python tools/model/klicuj.py`: odstraní zelené pozadí a uloží průhledné PNG zpátky do `assets/model_reakce/`.

## Společný začátek (vložit vždy)
```
Use the attached photo as the exact same woman: same face, same hair, same skin tone, same makeup. Waist-up portrait, facing the camera, bright even studio light, sharp focus, photorealistic. Solid pure green background (#00FF00), no shadows on the background, nothing else in the frame. She wears a stylish fitted Vice City outfit (cropped jacket over a fitted top), tasteful and fully clothed. Expression and pose:
```

## Reakce (jeden řádek = jeden obrázek)
- `gasp.png`: shocked gasp, mouth wide open, eyes wide, one hand on her cheek
- `laugh.png`: laughing hard, head tilted back a little, eyes squeezed, hand near her mouth
- `wink.png`: playful wink with a confident half smile
- `shocked.png`: jaw dropped in disbelief, both hands on her head
- `smirk.png`: teasing smirk, one eyebrow raised, chin slightly down
- `eyeroll.png`: dramatic eye roll, unimpressed, arms crossed

## Pravidla
- Žádná nahota ani lechtivé pózy (TikTok/IG/YouTube jinak omezí dosah).
- Pozadí musí být čistě zelené, jinak vystřižení nebude čisté.
