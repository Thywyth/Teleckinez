"""Text normalization before parsing (nlu.md §1.1-1.2)."""

import re

from teleckinez.core.text import normalize_name
from teleckinez.nlu.numbers import words_to_numbers

# Units are turned into standalone tokens so "40%" and "40 відсотків" look the same.
PERCENT_WORDS = {
    "відсоток", "відсотки", "відсотків", "відсотка", "процент", "процентів", "проценти",
    "percent", "percents", "per cent",
}  # fmt: skip
DEGREE_WORDS = {"градус", "градуси", "градусів", "градуса", "degree", "degrees", "celsius"}

_NUM_UNIT = re.compile(r"(\d+(?:[.,]\d+)?)\s*([%°])")
_PUNCT = re.compile(r"[^\w\s'%°+\-]", re.UNICODE)
_CYR = re.compile(r"[а-яіїєґ]")
_LAT = re.compile(r"[a-z]")


def detect_lang(text: str) -> str:
    """'uk' if Cyrillic letters dominate, else 'en' (mixed phrases are allowed)."""
    cyr, lat = len(_CYR.findall(text)), len(_LAT.findall(text))
    return "uk" if cyr >= lat and cyr > 0 else "en"


def tokenize(text: str) -> list[str]:
    """Normalized tokens: lowercase, unified apostrophes, numbers as digits, '%'/'°' units."""
    text = normalize_name(text)
    text = _NUM_UNIT.sub(r"\1 \2", text)
    text = text.replace("-", " ")  # "twenty-two", "робот-пилосос" -> separate words
    text = _PUNCT.sub(" ", text)
    tokens = []
    for tok in text.split():
        tok = tok.strip("'")
        if not tok:
            continue
        if tok in PERCENT_WORDS:
            tok = "%"
        elif tok in DEGREE_WORDS:
            tok = "°"
        tokens.append(tok.replace(",", "."))
    return words_to_numbers(tokens)
