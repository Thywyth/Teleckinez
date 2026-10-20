"""Download offline models into models/ (run once, needs internet).

Usage:
    python scripts/download_models.py            # default set (~0.8 GB)
    python scripts/download_models.py --all      # + large optional models (~1.9 GB more)
    python scripts/download_models.py --only vosk piper
    python scripts/download_models.py --check    # only report what is present/missing
    python scripts/download_models.py --verify   # also load every present model and run it once
    python scripts/download_models.py --list

Already downloaded models are skipped, so the script is safe to re-run.
Sources (checked 20.10.2026): alphacephei.com/vosk/models, huggingface.co
(Systran/faster-whisper-*, rhasspy/piper-voices), github.com/dscripka/openWakeWord.
"""

import argparse
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = ROOT / "models"

VOSK_URL = "https://alphacephei.com/vosk/models/{name}.zip"
PIPER_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/{path}"
OWW_URL = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/{name}"


@dataclass(frozen=True)
class ModelSpec:
    name: str
    group: str  # vosk | whisper | piper | openwakeword
    dest: str  # path relative to models/
    size_mb: int  # approximate download size
    default: bool = True
    url: str = ""  # vosk: zip archive to extract into dest
    files: dict[str, str] = field(default_factory=dict)  # file name -> URL (piper, oww)
    marker: str = ""  # file that must exist inside dest for the model to count as present

    def path(self, models_dir: Path) -> Path:
        return models_dir / self.dest

    def is_present(self, models_dir: Path) -> bool:
        base = self.path(models_dir)
        needed = [self.marker] if self.marker else list(self.files)
        return base.is_dir() and all((base / name).is_file() for name in needed)


def _piper(voice_path: str, size_mb: int) -> ModelSpec:
    voice = voice_path.rsplit("/", 1)[-1]
    return ModelSpec(
        name=voice,
        group="piper",
        dest=f"piper/{voice}",
        size_mb=size_mb,
        files={
            f"{voice}.onnx": PIPER_URL.format(path=f"{voice_path}.onnx"),
            f"{voice}.onnx.json": PIPER_URL.format(path=f"{voice_path}.onnx.json"),
        },
    )


MODELS: list[ModelSpec] = [
    ModelSpec(
        name="vosk-model-small-uk-v3-small",
        group="vosk",
        dest="vosk/vosk-model-small-uk-v3-small",
        size_mb=144,
        url=VOSK_URL.format(name="vosk-model-small-uk-v3-small"),
        marker="am/final.mdl",
    ),
    ModelSpec(
        name="vosk-model-small-en-us-0.15",
        group="vosk",
        dest="vosk/vosk-model-small-en-us-0.15",
        size_mb=41,
        url=VOSK_URL.format(name="vosk-model-small-en-us-0.15"),
        marker="am/final.mdl",
    ),
    ModelSpec(
        name="vosk-model-uk-v3",
        group="vosk",
        dest="vosk/vosk-model-uk-v3",
        size_mb=371,
        default=False,
        url=VOSK_URL.format(name="vosk-model-uk-v3"),
        marker="am/final.mdl",
    ),
    ModelSpec(
        name="faster-whisper-small",
        group="whisper",
        dest="whisper/small",
        size_mb=484,
        marker="model.bin",
    ),
    ModelSpec(
        name="faster-whisper-medium",
        group="whisper",
        dest="whisper/medium",
        size_mb=1530,
        default=False,
        marker="model.bin",
    ),
    _piper("uk/uk_UA/ukrainian_tts/medium/uk_UA-ukrainian_tts-medium", 77),
    _piper("en/en_US/lessac/medium/en_US-lessac-medium", 63),
    ModelSpec(
        name="openwakeword-hey_jarvis",
        group="openwakeword",
        dest="openwakeword",
        size_mb=3,
        # Only ONNX: tflite-runtime is not available on Windows (voice.md §2).
        files={
            name: OWW_URL.format(name=name)
            for name in ("melspectrogram.onnx", "embedding_model.onnx", "hey_jarvis_v0.1.onnx")
        },
    ),
]

GROUPS = sorted({m.group for m in MODELS})


def select_models(only: list[str] | None = None, include_optional: bool = False) -> list[ModelSpec]:
    selected = [m for m in MODELS if include_optional or m.default]
    if only:
        unknown = set(only) - set(GROUPS) - {m.name for m in MODELS}
        if unknown:
            raise ValueError(f"unknown model/group: {', '.join(sorted(unknown))}")
        pool = MODELS if include_optional else selected
        # Naming a model explicitly selects it even if it is optional.
        selected = [m for m in MODELS if m.name in only] + [
            m for m in pool if m.group in only and m.name not in only
        ]
    return selected


def _download(url: str, target: Path) -> None:
    """Download url to target via a .part file, so a partial file never looks complete."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "teleckinez-download/0.1"})
    with urllib.request.urlopen(request, timeout=60) as response, part.open("wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        while chunk := response.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r    {target.name}: {done * 100 // total:3d}% of {total >> 20} MB", end="")
    print()
    part.replace(target)


def _install_vosk(spec: ModelSpec, models_dir: Path) -> None:
    dest = spec.path(models_dir)
    with tempfile.TemporaryDirectory(dir=models_dir) as tmp:
        archive = Path(tmp) / f"{spec.name}.zip"
        _download(spec.url, archive)
        print("    extracting...")
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(tmp)
        extracted = Path(tmp) / spec.name  # archives contain a single top-level folder
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(extracted), dest)


def _install_whisper(spec: ModelSpec, models_dir: Path) -> None:
    from faster_whisper import download_model

    size = spec.dest.rsplit("/", 1)[-1]
    print(f"    from huggingface.co/Systran/faster-whisper-{size}")
    download_model(size, output_dir=str(spec.path(models_dir)))


def _install_files(spec: ModelSpec, models_dir: Path) -> None:
    for name, url in spec.files.items():
        target = spec.path(models_dir) / name
        if not target.is_file():
            _download(url, target)


def install(spec: ModelSpec, models_dir: Path) -> None:
    models_dir.mkdir(parents=True, exist_ok=True)
    if spec.group == "vosk":
        _install_vosk(spec, models_dir)
    elif spec.group == "whisper":
        _install_whisper(spec, models_dir)
    else:
        _install_files(spec, models_dir)


def _verify_vosk(spec: ModelSpec, models_dir: Path) -> str:
    import json

    import vosk

    from teleckinez.voice.paths import ascii_safe_path

    vosk.SetLogLevel(-1)
    model = vosk.Model(ascii_safe_path(spec.path(models_dir)))
    rec = vosk.KaldiRecognizer(model, 16_000)
    rec.AcceptWaveform(bytes(16_000 * 2))  # 1 s of silence
    return f"recognized silence as {json.loads(rec.FinalResult()).get('text', '')!r}"


def _verify_whisper(spec: ModelSpec, models_dir: Path) -> str:
    import numpy as np
    from faster_whisper import WhisperModel

    from teleckinez.voice.cuda import register_cuda_dlls

    register_cuda_dlls()
    try:
        model = WhisperModel(str(spec.path(models_dir)), device="cuda", compute_type="float16")
        device = "cuda float16"
    except Exception as exc:  # no GPU / no CUDA libs -> CPU fallback (voice.md §4)
        print(f"    cuda unavailable ({type(exc).__name__}: {exc}), trying cpu")
        model = WhisperModel(str(spec.path(models_dir)), device="cpu", compute_type="int8")
        device = "cpu int8"
    segments, _ = model.transcribe(np.zeros(16_000, dtype=np.float32), language="uk")
    list(segments)  # transcription is lazy; force it to actually run the model
    return f"ran on {device}"


def _verify_piper(spec: ModelSpec, models_dir: Path) -> str:
    from piper import PiperVoice
    from piper.voice import ESPEAK_DATA_DIR

    from teleckinez.voice.paths import ascii_safe_path

    # espeak-ng cannot open its data folder from a non-ASCII path and then exits the process.
    voice = PiperVoice.load(
        spec.path(models_dir) / f"{spec.name}.onnx",
        espeak_data_dir=ascii_safe_path(ESPEAK_DATA_DIR),
    )
    # The uk voice is character-based (phoneme_type: text) and knows only lowercase letters.
    text = "світло на кухні увімкнено." if spec.name.startswith("uk") else "Kitchen light is on."
    samples = sum(len(chunk.audio_int16_bytes) // 2 for chunk in voice.synthesize(text))
    seconds = samples / voice.config.sample_rate
    return f"synthesized {seconds:.1f} s of speech"


def _verify_openwakeword(spec: ModelSpec, models_dir: Path) -> str:
    import numpy as np
    from openwakeword.model import Model

    base = spec.path(models_dir)
    model = Model(
        wakeword_models=[str(base / "hey_jarvis_v0.1.onnx")],
        melspec_model_path=str(base / "melspectrogram.onnx"),
        embedding_model_path=str(base / "embedding_model.onnx"),
        inference_framework="onnx",
    )
    scores = model.predict(np.zeros(1280, dtype=np.int16))
    return f"scores on silence {scores}"


VERIFIERS = {
    "vosk": _verify_vosk,
    "whisper": _verify_whisper,
    "piper": _verify_piper,
    "openwakeword": _verify_openwakeword,
}


def verify(selected: list[ModelSpec], models_dir: Path) -> int:
    """Load and run every present model once; return the number of failures."""
    failures = 0
    for m in selected:
        if not m.is_present(models_dir):
            continue
        try:
            print(f"  verify {m.name}: {VERIFIERS[m.group](m, models_dir)}")
        except Exception as exc:
            failures += 1
            print(f"  verify {m.name}: FAILED {type(exc).__name__}: {exc}")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--all", action="store_true", help="include large optional models")
    parser.add_argument(
        "--only", nargs="+", metavar="NAME", help=f"groups ({', '.join(GROUPS)}) or model names"
    )
    parser.add_argument("--check", action="store_true", help="do not download, only report")
    parser.add_argument("--list", action="store_true", help="list known models")
    parser.add_argument("--verify", action="store_true", help="load and run present models once")
    parser.add_argument("--models-dir", type=Path, default=MODELS_DIR)
    args = parser.parse_args(argv)

    if args.list:
        for m in MODELS:
            kind = "default" if m.default else "optional"
            print(f"{m.group:<13} {m.name:<38} {m.size_mb:>5} MB  {kind}")
        return 0

    try:
        selected = select_models(args.only, args.all)
    except ValueError as exc:
        parser.error(str(exc))

    missing = [m for m in selected if not m.is_present(args.models_dir)]
    for m in selected:
        status = "missing" if m in missing else "ok"
        print(f"[{status:>7}] {m.group:<13} {m.name}  -> models/{m.dest}")
    if args.check or args.verify or not missing:
        print("all models present" if not missing else f"{len(missing)} model(s) missing")
        failures = verify(selected, args.models_dir) if args.verify else 0
        return 1 if missing or failures else 0

    total = sum(m.size_mb for m in missing)
    print(f"\ndownloading {len(missing)} model(s), ~{total} MB ...")
    failed = []
    for m in missing:
        print(f"  -> {m.name} (~{m.size_mb} MB)")
        try:
            install(m, args.models_dir)
        except Exception as exc:  # keep going with other models, report at the end
            failed.append(m.name)
            print(f"    FAILED: {type(exc).__name__}: {exc}")
    if failed:
        print(f"\nfailed: {', '.join(failed)}; re-run the script to retry")
        return 1
    print("\ndone")
    return 0


if __name__ == "__main__":
    sys.exit(main())
