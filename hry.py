"""
hry.py - which streams we clip and how a game is named (owner 26. 11.: "nemusime klipovat jen GTA 6, ale proste
streamery, kteri hraji hry"). A week after launch the GTA VI category gave ~130 clips a day and a day passed with
no short at all; the account becomes a gaming clip account, GTA 6 first.

  is_gaming(category)  a Twitch / Kick category that is a game (not Just Chatting, IRL, casino, music ...)
  is_primary(category) GTA 6 - ranked first (publikuj score x primary_boost), named "GTA 6"
  label(category)      the game's short name for titles ("Grand Theft Auto VI" -> "GTA 6")
  hashtag(category)    "#gta6", "#fortnite", ...

sources.json: categories_exclude (more non-game categories), other_games (how many top Twitch games besides GTA
VI are searched for clips), primary_boost.
"""
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Twitch / Kick categories that are no game: never clipped (talking, casino, IRL, music, events ...)
NOT_GAMES = {
    "just chatting", "irl", "slots", "virtual casino", "poker", "casino", "gambling", "sports", "music",
    "asmr", "pools, hot tubs, and beaches", "talk shows & podcasts", "special events", "art", "makers & crafting",
    "food & drink", "travel & outdoors", "science & technology", "software and game development",
    "fitness & health", "beauty & body art", "animals, aquariums, and zoos", "politics", "crypto", "co-working & studying",
    "dj", "movies & tv", "watch party", "news", "lotto", "roulette", "blackjack", "stake", "",
}
PRIMARY = re.compile(r"Grand Theft Auto VI|GTA ?VI\b|GTA ?6\b", re.I)
SHORT = {
    "grand theft auto vi": "GTA 6", "grand theft auto v": "GTA 5", "counter-strike": "CS2", "counter-strike 2": "CS2",
    "call of duty: warzone": "Warzone", "call of duty: black ops 7": "Black Ops 7", "call of duty: black ops 6": "Black Ops 6",
    "league of legends": "League", "tom clancy's rainbow six siege": "Rainbow Six Siege", "red dead redemption 2": "RDR2",
    "escape from tarkov": "Tarkov", "dead by daylight": "Dead by Daylight", "ea sports fc 26": "FC 26",
}


def _cfg():
    try:
        return json.loads((HERE / "sources.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def is_gaming(category):
    """Any game category; '' (unknown) is no game - the render looks it up first."""
    cats = [c.strip().lower() for c in re.split(r"\s{2,}|\|", category or "") if c.strip()] or [(category or "").strip().lower()]
    block = NOT_GAMES | {x.lower() for x in _cfg().get("categories_exclude", [])}
    return any(c and c not in block for c in cats)


def is_primary(category):
    return bool(PRIMARY.search(category or ""))


def label(category):
    """Short game name for a title: 'GTA 6', 'Fortnite', 'Warzone'... ('this game' when unknown)."""
    c = (category or "").strip()
    if not c:
        return "this game"
    if is_primary(c):
        return "GTA 6"
    s = SHORT.get(c.lower())
    if s:
        return s
    c = re.sub(r"[™®©]", "", c).strip()
    return c if len(c) <= 22 else c.split(":")[0].strip()[:22]


def hashtag(category):
    t = re.sub(r"[^a-z0-9]", "", label(category).lower())
    return f"#{t}" if t and t != "thisgame" else "#gaming"
