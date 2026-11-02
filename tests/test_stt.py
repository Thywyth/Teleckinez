"""STT (voice.md §4): Vosk grammar built from devices.yaml; real models on Piper-synthesized speech.

Tests marked `models` need scripts/download_models.py; they are skipped when a
model is missing and can be deselected with `pytest -m "not models"`.
"""

import pytest

from teleckinez.core.config import VoiceSettings
from teleckinez.nlu.grammar import build_grammar, command_words, unk_symbol
from teleckinez.nlu.resolver import TargetResolver
from teleckinez.nlu.rule_parser import RuleParser
from teleckinez.voice.paths import model_path

VOICE = VoiceSettings()
UK_MODEL = model_path(VOICE.vosk_model_uk)
EN_MODEL = model_path(VOICE.vosk_model_en)

# (phrase spoken by Piper, device the NLU must reach from the transcript)
UK_COMMANDS = [
    ("увімкни світло на кухні", "light.kitchen_main"),
    ("відкрий штори у вітальні", "curtains.living_room"),
    ("вимкни телевізор", "tv.living_room"),
    ("закрий гаражні ворота", "gate.garage"),
    ("встанови світло у спальні на сорок відсотків", "light.bedroom_main"),
    ("увімкни чайник", "kettle.kitchen"),
    ("зроби телевізор тихіше", "tv.living_room"),
    ("увімкни нічник у дитячій", "light.kids_nightlight"),
]
EN_COMMANDS = [
    ("turn on the kitchen light", "light.kitchen_main"),
    ("open the bedroom curtains", "curtains.bedroom"),
    ("turn off the tv", "tv.living_room"),
]


def test_grammar_has_device_room_and_action_words(registry) -> None:
    grammar, missing = build_grammar(registry, "uk")
    assert missing == []  # without a vocabulary nothing is filtered
    for word in ("світло", "кухні", "увімкни", "штори", "сорок", "відсотків", "телекінез"):
        assert word in grammar
    assert "увімкни світло" not in grammar  # actions are single words, names may be phrases
    assert "гаражні ворота" in grammar
    assert grammar[-1] == "[unk]"
    en, _ = build_grammar(registry, "en")
    assert "kitchen" in en and "light" in en and "світло" not in en


def test_grammar_drops_words_the_model_does_not_know(registry) -> None:
    vocabulary = set(command_words(registry, "uk")) - {"чайник"} | {"<UNK>", "телекінез"}
    grammar, missing = build_grammar(registry, "uk", vocabulary)
    assert missing == ["чайник"] and "чайник" not in grammar
    assert grammar[-1] == "<UNK>"


def test_unk_symbol() -> None:
    assert unk_symbol(None) == "[unk]"
    assert unk_symbol({"<UNK>", "a"}) == "<UNK>"


def test_wake_word_optional(registry) -> None:
    grammar, _ = build_grammar(registry, "uk", include_wake_word=False)
    assert "телекінез" not in grammar


def test_ids_used_in_tests_exist(registry) -> None:
    for _, device in UK_COMMANDS + EN_COMMANDS:
        assert device in registry


# ------------------------------------------------------------------ real models


def _need(path) -> None:
    if not path.is_dir():
        pytest.skip(f"model missing: {path.name} (python scripts/download_models.py)")


@pytest.fixture(scope="module")
def vosk_uk(registry):
    _need(UK_MODEL)
    from teleckinez.voice.stt.vosk_stt import create_vosk

    return create_vosk(VoiceSettings(stt_lang="uk"), registry)


@pytest.fixture(scope="module")
def vosk_en(registry):
    _need(EN_MODEL)
    from teleckinez.voice.stt.vosk_stt import create_vosk

    return create_vosk(VoiceSettings(stt_lang="en"), registry)


def reaches(registry, text: str) -> list[str]:
    result = RuleParser(registry).parse(text)
    assert result.ok, f"NLU did not understand the transcript {text!r}"
    return [c.device_id for c in TargetResolver(registry).resolve(result.intent).commands]


@pytest.mark.models
@pytest.mark.parametrize(("phrase", "device"), UK_COMMANDS)
def test_vosk_grammar_uk(vosk_uk, speech, registry, phrase, device) -> None:
    transcript = vosk_uk.transcribe(speech(phrase, "uk"), "uk")
    assert transcript.engine == "vosk-grammar" and transcript.lang == "uk"
    assert transcript.confidence > 0.7
    assert reaches(registry, transcript.text) == [device], transcript.text


@pytest.mark.models
@pytest.mark.parametrize(("phrase", "device"), EN_COMMANDS)
def test_vosk_grammar_en(vosk_en, speech, registry, phrase, device) -> None:
    transcript = vosk_en.transcribe(speech(phrase, "en"), "en")
    assert reaches(registry, transcript.text) == [device], transcript.text


@pytest.mark.models
def test_vosk_hears_the_wake_word(vosk_uk, speech) -> None:
    assert vosk_uk.transcribe(speech("телекінез", "uk")).text == "телекінез"


@pytest.mark.models
def test_vosk_silence_is_empty(vosk_uk) -> None:
    import numpy as np

    transcript = vosk_uk.transcribe(np.zeros(16_000, np.int16))
    assert transcript.text == "" and transcript.confidence == 0.0


@pytest.mark.models
async def test_transcribe_async_runs_in_a_thread(vosk_uk, speech) -> None:
    from teleckinez.voice.stt import transcribe_async

    transcript = await transcribe_async(vosk_uk, speech("увімкни світло на кухні"), "uk")
    assert transcript.text == "увімкни світло на кухні" and transcript.rtf < 1
