"""Text normalization shared by configuration and NLU."""

import re
import unicodedata

# Typographic and modifier apostrophes that STT, keyboards and YAML files produce.
_APOSTROPHES = str.maketrans({"’": "'", "ʼ": "'", "`": "'", "‘": "'", "´": "'"})
_SPACES = re.compile(r"\s+")


def normalize_name(text: str) -> str:
    """Lowercase, unify apostrophes and whitespace: "П’ять  Хвилин" -> "п'ять хвилин"."""
    text = unicodedata.normalize("NFC", text).translate(_APOSTROPHES).lower()
    return _SPACES.sub(" ", text).strip()
