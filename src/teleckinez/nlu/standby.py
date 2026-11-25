"""Phrases that switch the standby mode (no wake word) on and off (voice.md §2, ADR-014).

They are checked before the intent parser and must make up the whole
utterance ("Телекінез, режим очікування"), so a command that merely mentions
the words is never taken for one. Bare "стоп" is not used: it stops the
vacuum or the curtains (lexicon.ACTIONS["stop"]).
"""

from teleckinez.nlu.normalizer import tokenize

WAKE_WORDS = ("телекінез", "teleckinez", "telekinez")
FILLERS = ("будь", "ласка", "please")

ON = (
    "режим очікування", "увімкни режим очікування", "увімкнути режим очікування",
    "перейди в режим очікування", "перейди у режим очікування",
    "standby mode", "standby", "turn on standby", "start listening",
)  # fmt: skip
OFF = (
    "вимкни режим очікування", "вимкнути режим очікування",
    "вийди з режиму очікування", "вийти з режиму очікування",
    "exit standby", "standby off", "turn off standby", "stop listening",
)  # fmt: skip


def strip_wake_word(text: str) -> str:
    """'Телекінез, увімкни світло' -> 'увімкни світло' (standby mode needs no wake word)."""
    words = text.split()
    if words and words[0].strip(",.!?").lower() in WAKE_WORDS:
        return " ".join(words[1:]).lstrip(",.!? ")
    return text


def standby_request(text: str) -> bool | None:
    """True / False if the whole phrase asks to enter / leave standby, else None."""
    tokens = [t for t in tokenize(text) if t not in FILLERS]
    if tokens and tokens[0] in WAKE_WORDS:
        tokens = tokens[1:]
    phrase = " ".join(tokens)
    if phrase in OFF:
        return False
    if phrase in ON:
        return True
    return None
