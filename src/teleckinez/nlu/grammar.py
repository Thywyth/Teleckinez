"""Restricted grammar for Vosk, built from the same sources as the NLU (nlu.md §2).

With a grammar the recognizer only "hears" known words, which sharply
improves accuracy on commands. Words come from devices.yaml (rooms, names,
groups), nlu/lexicon.py and number words, so STT and NLU never disagree
about the vocabulary.
"""

import re
from collections.abc import Iterable
from pathlib import Path

from teleckinez.core.text import normalize_name
from teleckinez.devices.registry import DeviceRegistry
from teleckinez.nlu import lexicon, numbers
from teleckinez.nlu.normalizer import DEGREE_WORDS, PERCENT_WORDS

WAKE_WORDS = {"uk": ["телекінез"], "en": []}
# Vosk's own name for "any other word"; models trained with Kaldi recipes call it <UNK>
# (vosk-model-uk-v3*), and Vosk ignores "[unk]" there with a warning.
UNK_SYMBOLS = ("[unk]", "<UNK>", "<unk>")
_CYR = re.compile(r"[а-яіїєґ]")


def _lang_of(word: str) -> str:
    return "uk" if _CYR.search(word) else "en"


def _words(phrases: Iterable[str]) -> list[str]:
    out = []
    for phrase in phrases:
        out.extend(normalize_name(phrase).replace("-", " ").split())
    return out


def command_phrases(registry: DeviceRegistry, lang: str) -> list[str]:
    """Multi-word phrases (device and room names) - they help the decoder with word order."""
    phrases: list[str] = []
    for room in registry.rooms.values():
        phrases += room.get(lang)
    for dev in registry:
        phrases += dev.config.names.get(lang)
    for names in registry.config.groups.values():
        phrases += names.get(lang)
    return [normalize_name(p).replace("-", " ") for p in phrases if " " in p.strip()]


def command_words(registry: DeviceRegistry, lang: str) -> list[str]:
    """Every single word the NLU understands in `lang`."""
    sources: list[str] = []
    for room in registry.rooms.values():
        sources += room.all()
    for dev in registry:
        sources += dev.config.names.all()
    for names in registry.config.groups.values():
        sources += names.all()
    for phrases in (*lexicon.ACTIONS.values(), *lexicon.ATTRIBUTES.values(),
                    *lexicon.COLORS.values(), *lexicon.AC_MODES.values(),
                    *lexicon.TYPE_WORDS.values()):  # fmt: skip
        sources += phrases
    sources += list(lexicon.STEPS) + lexicon.QUERY_WORDS + lexicon.ALL_WORDS
    sources += lexicon.STATE_ADJECTIVES + sorted(lexicon.STOPWORDS)
    sources += [*numbers.UNITS, *numbers.TENS, *numbers.SPECIAL, *numbers.ORDINALS, *numbers.HALF]
    sources += [*PERCENT_WORDS, *DEGREE_WORDS]
    words = [w for w in _words(sources) if _lang_of(w) == lang and w.replace("'", "").isalpha()]
    return sorted(set(words))


def unk_symbol(vocabulary: set[str] | None) -> str:
    """The model's out-of-vocabulary word: <UNK> if the model lists it, else Vosk's [unk]."""
    if vocabulary is not None:
        for symbol in ("<UNK>", "<unk>"):
            if symbol in vocabulary:
                return symbol
    return "[unk]"


def build_grammar(
    registry: DeviceRegistry,
    lang: str,
    vocabulary: set[str] | None = None,
    *,
    include_wake_word: bool = True,
) -> tuple[list[str], list[str]]:
    """(grammar for Vosk, words missing from the model vocabulary).

    Words the acoustic model does not know cannot be recognized anyway; they
    are dropped (Vosk would only log a warning) and reported, so the lexicon
    can be adjusted (ROADMAP 2.9).
    """
    words = command_words(registry, lang)
    if include_wake_word:
        words = sorted(set(words) | set(WAKE_WORDS.get(lang, [])))
    phrases = command_phrases(registry, lang)
    missing: list[str] = []
    if vocabulary is not None:
        missing = [w for w in words if w not in vocabulary]
        words = [w for w in words if w in vocabulary]
        phrases = [p for p in phrases if all(w in vocabulary for w in p.split())]
    return [*words, *sorted(set(phrases)), unk_symbol(vocabulary)], missing


def load_vocabulary(model_dir: str | Path) -> set[str] | None:
    """Words of a Vosk model (graph/words.txt), or None if the model does not ship the list."""
    path = Path(model_dir) / "graph" / "words.txt"
    if not path.is_file():
        return None
    with path.open(encoding="utf-8") as f:
        return {line.split(" ", 1)[0] for line in f}
