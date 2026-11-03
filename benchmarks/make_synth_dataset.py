"""Synthetic voice set: the `voice: true` phrases spoken by Piper (metrics.md §1, voice_synth).

Only an auxiliary set - TTS speech is cleaner and more regular than a person
in a room, so every number measured on it is labelled `synthetic` and kept
apart from the real recordings (scripts/record_dataset.py).

Writes benchmarks/datasets/voice_synth/:
  <voice>/<phrase_id>.wav + manifest.csv  - commands (same columns as the real manifest)
  wake/*.wav + wake_manifest.csv          - wake word clips for E5 and E3

    python benchmarks/make_synth_dataset.py
"""

import argparse
import csv
import importlib.util
import json
import sys

import numpy as np
from common import DATASETS, ROOT

from teleckinez.core.config import load_settings
from teleckinez.voice.audio_io import SAMPLE_RATE, resample, write_wav
from teleckinez.voice.paths import ascii_safe_path, model_path
from teleckinez.voice.tts.speakable import speakable

OUT = DATASETS / "voice_synth"
# Ukrainian: 3 speakers of the uk_UA-ukrainian_tts model; English: one voice at 3 speeds.
VOICES = {
    "uk": [(spk, sid, speed) for spk, sid in (("lada", 0), ("mykyta", 1), ("tetiana", 2))
           for speed in (1.0, 1.25)],
    "en": [("lessac", None, speed) for speed in (0.9, 1.0, 1.25)],
}  # fmt: skip
WAKE = {"vosk_kws": ("uk", "Телекінез"), "openwakeword": ("en", "Hey Jarvis")}
PAD_S = 0.4


def _record_dataset():
    spec = importlib.util.spec_from_file_location("rd", ROOT / "scripts" / "record_dataset.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Synth:
    def __init__(self) -> None:
        from piper import PiperVoice, SynthesisConfig
        from piper.voice import ESPEAK_DATA_DIR

        self.config_cls = SynthesisConfig
        voice = load_settings().voice
        espeak = ascii_safe_path(ESPEAK_DATA_DIR)
        self.voices = {
            "uk": PiperVoice.load(model_path(voice.piper_voice_uk), espeak_data_dir=espeak),
            "en": PiperVoice.load(model_path(voice.piper_voice_en), espeak_data_dir=espeak),
        }

    def say(self, text: str, lang: str, speaker: int | None, speed: float) -> np.ndarray:
        voice = self.voices[lang]
        text = speakable(text, lang)
        if lang == "uk":
            text = text.lower()
        cfg = self.config_cls(speaker_id=speaker, length_scale=speed)
        audio = np.concatenate([c.audio_int16_array for c in voice.synthesize(text, cfg)])
        audio = resample(audio, voice.config.sample_rate, SAMPLE_RATE)
        return audio.astype(np.int16)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SAMPLE_RATE), np.int16)


def make_commands(synth: Synth, rd) -> int:
    rows = []
    for phrase in rd.load_phrases():
        for name, sid, speed in VOICES[phrase.lang]:
            speaker = f"piper-{name}-{speed:g}"
            audio = np.concatenate([silence(PAD_S), synth.say(phrase.text, phrase.lang, sid, speed),
                                    silence(PAD_S)])  # fmt: skip
            wav = OUT / speaker / f"{phrase.phrase_id}.wav"
            wav.parent.mkdir(parents=True, exist_ok=True)
            write_wav(wav, audio)
            row = rd.manifest_row(phrase, speaker, wav, audio.tobytes(), "synthetic")
            rows.append(row)
    rd.write_manifest(OUT / "manifest.csv", rows)
    return len(rows)


def make_wake(synth: Synth, rd) -> int:
    """Wake word alone, wake word + pause + command (E3), and commands without it (negatives)."""
    rows = []
    phrases = rd.load_phrases()
    for engine, (lang, word) in WAKE.items():
        for name, sid, speed in VOICES[lang]:
            speaker = f"piper-{name}-{speed:g}"
            for k, phrase in enumerate(p for p in phrases if p.lang == lang):
                wake = synth.say(word, lang, sid, speed)
                parts = [silence(PAD_S), wake, silence(1.0)]
                cmd = synth.say(phrase.text, lang, sid, speed)
                if k % 2 == 0:  # half of the clips: wake word + command (E3 voice chain)
                    parts += [cmd, silence(1.5)]
                audio = np.concatenate(parts)
                file = OUT / "wake" / f"{engine}_{speaker}_{k:03d}.wav"
                file.parent.mkdir(parents=True, exist_ok=True)
                write_wav(file, audio)
                rows.append(
                    {
                        "file": file.relative_to(DATASETS).as_posix(),
                        "engine": engine,
                        "wake": 1,
                        "text": f"{word}. {phrase.text}" if k % 2 == 0 else word,
                        "command": phrase.text if k % 2 == 0 else "",
                        "command_id": phrase.phrase_id if k % 2 == 0 else "",
                        "intent": json.dumps(phrase.expect, ensure_ascii=False)
                        if k % 2 == 0
                        else "",
                        "speaker": speaker,
                        "duration_s": f"{audio.size / SAMPLE_RATE:.2f}",
                        "source": "synthetic",
                    }
                )
    # negatives: every command clip of the set, without any wake word
    with (OUT / "manifest.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(
                {"file": f"voice_synth/{r['file']}" if not r["file"].startswith("voice_synth")
                 else r["file"], "engine": "", "wake": 0, "text": r["text"], "command": "",
                 "command_id": "", "intent": "", "speaker": r["speaker"],
                 "duration_s": r["duration_s"], "source": "synthetic"}
            )  # fmt: skip
    path = OUT / "wake_manifest.csv"
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", choices=("commands", "wake"), help="build one part only")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    rd = _record_dataset()
    rd.DATASETS = DATASETS
    synth = Synth()
    if args.only in (None, "commands"):
        print(f"commands: {make_commands(synth, rd)} clips -> {OUT / 'manifest.csv'}")
    if args.only in (None, "wake"):
        print(f"wake: {make_wake(synth, rd)} clips -> {OUT / 'wake_manifest.csv'}")


if __name__ == "__main__":
    main()
