import os
import sys
from pathlib import Path

import pytest

from teleckinez.voice.paths import ascii_safe_path


def test_ascii_path_returned_resolved(tmp_path: Path) -> None:
    target = tmp_path / "model"
    target.mkdir()
    result = ascii_safe_path(target)
    if str(target.resolve()).isascii():
        assert Path(result) == target.resolve()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific workaround")
def test_non_ascii_path_becomes_ascii(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "Курсова" / "модель"
    target.mkdir(parents=True)
    # From an unrelated cwd the relative path would contain Cyrillic, so short path is used.
    monkeypatch.chdir(tmp_path)
    result = ascii_safe_path(target)
    assert result.isascii()
    assert os.path.samefile(result, target)
