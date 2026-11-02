"""TTS (voice.md §5): text normalization for the voice, Piper, SAPI fallback, text-only.

The round trip test synthesizes an answer with Piper and recognizes it with
Vosk: what the assistant says must be understandable.
"""

import numpy as np
import pytest

from teleckinez.core.config import VoiceSettings
from teleckinez.voice.paths import model_path
from teleckinez.voice.tts import TextOnly, create_tts
from teleckinez.voice.tts.speakable import number_uk, speakable


@pytest.mark.parametrize(
    ("n", "feminine", "words"),
    [
        (0, False, "нуль"),
        (1, True, "одна"),
        (2, True, "дві"),
        (2, False, "два"),
        (11, False, "одинадцять"),
        (22, True, "двадцять дві"),
        (40, False, "сорок"),
        (115, False, "сто п'ятнадцять"),
        (1250, False, "одна тисяча двісті п'ятдесят"),
        (2000, False, "дві тисячі"),
        (-5, False, "мінус п'ять"),
    ],
)
def test_number_uk(n, feminine, words) -> None:
    assert number_uk(n, feminine) == words


@pytest.mark.parametrize(
    ("text", "spoken"),
    [
        ("Температура 22 °C.", "Температура двадцять два градуси."),
        ("вологість 41 %", "вологість сорок один відсоток"),
        ("відкрито на 50 %", "відкрито на п'ятдесят відсотків"),
        ("дані 1 хв тому", "дані одна хвилина тому"),
        ("дані 2 хв тому", "дані дві хвилини тому"),
        ("21 с тому", "двадцять одна секунда тому"),
        ("встановлено 21,5 °C", "встановлено двадцять одна кома п'ять градуса"),
        ("споживає 1250 Вт", "споживає одна тисяча двісті п'ятдесят ват"),
        ("Увімкнено 3 з 11 пристроїв.", "Увімкнено три з одинадцять пристроїв."),
        ("Світло на кухні увімкнено.", "Світло на кухні увімкнено."),
    ],
)
def test_speakable_uk(text, spoken) -> None:
    assert speakable(text, "uk") == spoken


def test_speakable_en_keeps_digits() -> None:
    assert speakable("Temperature 22 °C, humidity 41 %", "en") == (
        "Temperature 22 degrees, humidity 41 percent"
    )


def test_text_only() -> None:
    assert TextOnly().synthesize("будь-що") is None
    assert create_tts(VoiceSettings(tts="text")).name == "text"


def test_missing_piper_voice_falls_back(tmp_path) -> None:
    settings = VoiceSettings(piper_voice_uk=str(tmp_path / "none.onnx"))
    assert create_tts(settings).name in ("sapi", "text")


# ------------------------------------------------------------------ real engines


@pytest.fixture(scope="module")
def piper():
    settings = VoiceSettings()
    if not model_path(settings.piper_voice_uk).is_file():
        pytest.skip("Piper voices missing (python scripts/download_models.py)")
    from teleckinez.voice.tts.piper_tts import create_piper

    return create_piper(settings)


@pytest.mark.models
@pytest.mark.parametrize(
    ("text", "lang"),
    [("Світло на кухні увімкнено.", "uk"), ("Kitchen light is on.", "en")],
)
def test_piper_speaks(piper, text, lang) -> None:
    speech = piper.synthesize(text, lang)
    assert speech.rate == 22_050 and 0.8 < speech.duration_s < 4
    assert np.abs(speech.samples).max() > 3000  # not silence


@pytest.mark.models
def test_piper_numbers_are_spoken(piper) -> None:
    """With digits left in, the character-based uk voice skipped them: much shorter audio."""
    with_number = piper.synthesize("Температура 22 °C.", "uk").duration_s
    without = piper.synthesize("Температура.", "uk").duration_s
    assert with_number > without + 0.8


@pytest.mark.models
def test_piper_answer_is_understandable(piper, registry) -> None:
    """Round trip: Piper says the answer, Vosk (free vocabulary) hears the same words."""
    if not model_path(VoiceSettings().vosk_model_uk).is_dir():
        pytest.skip("Vosk uk model missing")
    from teleckinez.voice.audio_io import SAMPLE_RATE, resample
    from teleckinez.voice.stt.vosk_stt import create_vosk

    speech = piper.synthesize("Штори у вітальні відкрито на 50 %.", "uk")
    pause = np.zeros(SAMPLE_RATE // 3, np.int16)  # Piper starts at once; Vosk drops word 1
    audio = np.concatenate([pause, resample(speech.samples, speech.rate, SAMPLE_RATE), pause])
    stt = create_vosk(VoiceSettings(stt_grammar=False), registry)
    text = stt.transcribe(audio, "uk").text
    # the point is that numbers and units are spoken (the first consonant is weak in Piper)
    assert text.endswith("у вітальні відкрито на п'ятдесят відсотків"), text


@pytest.mark.models
def test_sapi_fallback_has_no_ukrainian_voice_or_speaks_it() -> None:
    pytest.importorskip("pyttsx3")
    from teleckinez.voice.tts.sapi_tts import SapiTts

    try:
        sapi = SapiTts()
    except Exception as exc:
        pytest.skip(f"SAPI unavailable: {exc}")
    if "en" in sapi.voices:
        speech = sapi.synthesize("Kitchen light is on.", "en")
        assert speech is not None and speech.duration_s > 0.5
    uk = sapi.synthesize("Світло увімкнено.", "uk")
    assert (uk is None) == ("uk" not in sapi.voices)
