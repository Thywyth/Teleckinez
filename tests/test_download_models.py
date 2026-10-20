import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "download_models", Path(__file__).resolve().parents[1] / "scripts" / "download_models.py"
)
dm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dm)


def test_default_selection_excludes_optional() -> None:
    names = {m.name for m in dm.select_models()}
    assert "vosk-model-small-uk-v3-small" in names
    assert "uk_UA-ukrainian_tts-medium" in names
    assert "vosk-model-uk-v3" not in names
    assert "faster-whisper-medium" not in names


def test_only_group_and_explicit_optional_name() -> None:
    assert {m.group for m in dm.select_models(["piper"])} == {"piper"}
    names = {m.name for m in dm.select_models(["vosk-model-uk-v3"])}
    assert names == {"vosk-model-uk-v3"}


def test_unknown_name_rejected() -> None:
    with pytest.raises(ValueError):
        dm.select_models(["nope"])


def test_presence_check(tmp_path: Path) -> None:
    vosk = next(m for m in dm.MODELS if m.name == "vosk-model-small-en-us-0.15")
    piper = next(m for m in dm.MODELS if m.group == "piper")
    assert not vosk.is_present(tmp_path)
    (vosk.path(tmp_path) / "am").mkdir(parents=True)
    (vosk.path(tmp_path) / "am" / "final.mdl").write_bytes(b"x")
    assert vosk.is_present(tmp_path)

    first, *_ = piper.files
    piper.path(tmp_path).mkdir(parents=True)
    (piper.path(tmp_path) / first).write_bytes(b"x")
    assert not piper.is_present(tmp_path)  # .onnx.json still missing
    assert dm.main(["--check", "--models-dir", str(tmp_path), "--only", "piper"]) == 1
