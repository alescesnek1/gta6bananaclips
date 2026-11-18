"""
cenzura.py - swear words, slurs and sexual words in every text that goes out get asterisks but stay readable
(owner 18. 11.: "strip club" -> "str** club"; the word stays readable, the networks' filters see stars).

  from cenzura import cenzura
  cenzura("He went to the strip club, holy shit")  ->  "He went to the str** club, holy s**t"

Rule: a word of up to 4 letters keeps its first and last letter ("s**t", "f**k"), a longer one its first three
("str** club", "bit**", "fuc*****"). Case is kept. Used on captions, YouTube titles, long-form titles and
descriptions, the e-mail's copy and the stories' text. Hashtags / tags with such a word are dropped instead
(a starred tag finds nothing): tag_ok().
"""
import re

# a word that STARTS with one of these stems is starred (fuck -> fucking, fucked; motherf -> motherfucker)
STEMS = ["fuck", "fuk", "fck", "shit", "bitch", "bastard", "asshole", "dick", "pussy", "cunt", "whore", "slut",
         "nigg", "fag", "retard", "porn", "naked", "boob", "titty", "titties", "stripper", "stripclub", "striptease", "hooker",
         "prostitut", "sexual", "horny", "dildo", "jizz", "wank", "twat", "goddamn", "motherf", "bullshit", "dumbass",
         "jackass", "orgasm", "penis", "vagina", "cocks", "cocksucker", "milf", "cumming"]
# short words only as whole words (as stems they would hit "class", "shoe", "cumulative", "title", "comic strip")
WHOLE = {"ass", "asses", "hoe", "hoes", "cum", "sex", "sexy", "tit", "tits", "damn", "piss", "pissed",
         "cock", "nude", "nudes", "thot", "dick", "dicks", "shitty", "fucky"}
# "strip" only in these phrases ("comic strip", "strip the car" stay)
_STRIP = re.compile(r"\b(strip)(?=[\s-]*(club|clubs|bar|joint|show)\b)", re.I)
# words that start like a stem but are fine
SAFE = {"dickens", "dickinson", "titan", "titans"}

_WORD = re.compile(r"[A-Za-z][A-Za-z']*")


def bad(word):
    w = word.lower().strip("'")
    if w.endswith("'s"):
        w = w[:-2]
    if w in SAFE:
        return False
    return w in WHOLE or any(w.startswith(s) for s in STEMS)


def star(word):
    """Up to 4 letters keep the first and last letter ('s**t'), longer keep the first three ('str**')."""
    if len(word) <= 4:
        return word[0] + "*" * (len(word) - 2) + word[-1] if len(word) > 2 else word[0] + "*"
    return word[:3] + "*" * min(len(word) - 3, 5)


def cenzura(text):
    if not text:
        return text
    text = _STRIP.sub(lambda m: star(m.group(1)), text)
    return _WORD.sub(lambda m: star(m.group(0)) if bad(m.group(0)) else m.group(0), text)


def tag_ok(tag):
    """A hashtag / YouTube tag with a starred word finds nothing: drop it instead."""
    return cenzura(tag or "") == (tag or "")


if __name__ == "__main__":
    for t in ["He went to the strip club, holy shit", "WHAT THE FUCK bro", "Stripper dances", "class assist cocktail",
              "Strips the car", "that's a bitch move", "motherfucker", "Kai gets sexy dance"]:
        print(f"{t!r:42} -> {cenzura(t)!r}")
