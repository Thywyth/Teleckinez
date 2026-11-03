import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("record_dataset", ROOT / "scripts/record_dataset.py")
rd = importlib.util.module_from_spec(spec)
sys.modules["record_dataset"] = rd
spec.loader.exec_module(rd)

PCM = b"\x00\x10" * 16_000  # 1 s of a quiet constant signal


def test_phrase_set_size() -> None:
    phrases = rd.load_phrases()
    uk = [p for p in phrases if p.lang == "uk"]
    en = [p for p in phrases if p.lang == "en"]
    assert 60 <= len(uk) <= 100
    assert 30 <= len(en) <= 50
    assert len({p.phrase_id for p in phrases}) == len(phrases)
    assert rd.load_phrases(lang="en") == en


def test_safe_name() -> None:
    assert rd.safe_name(" Roman Ts ") == "roman_ts"
    assert rd.safe_name("Анна") == "анна"
    with pytest.raises(ValueError):
        rd.safe_name("  ")


def run_session(tmp_path, answers, phrases):
    manifest = tmp_path / "voice" / "manifest.csv"
    takes = []

    def record():
        takes.append(1)
        return PCM

    replies = iter(answers)
    saved = rd.session(
        phrases, "roman", manifest, record, lambda pcm: None, ask=lambda _: next(replies),
        out=lambda _: None,
    )  # fmt: skip
    return saved, manifest, takes


def test_session_saves_redo_skip_and_resume(tmp_path) -> None:
    phrases = rd.load_phrases()[:4]
    # 1: save · 2: redo, then save · 3: skip · 4: quit
    saved, manifest, takes = run_session(tmp_path, ["", "r", "", "s", "q"], phrases)
    assert saved == 2
    assert len(takes) == 5
    rows = rd.read_manifest(manifest)
    assert [r["phrase_id"] for r in rows] == [phrases[0].phrase_id, phrases[1].phrase_id]
    assert rows[0]["text"] == phrases[0].text
    assert rows[0]["duration_s"] == "1.00"
    wav = manifest.parent / "roman" / f"{phrases[0].phrase_id}.wav"
    assert wav.exists()
    # resume: done ids are the saved ones; a redone take replaces its row
    done = rd.done_ids(rows, "roman", manifest.parent)
    assert done == {phrases[0].phrase_id, phrases[1].phrase_id}
    run_session(tmp_path, [""], phrases[:1])
    assert len(rd.read_manifest(manifest)) == 2


def test_wake_set_and_session(tmp_path) -> None:
    items = rd.load_wake_phrases()
    assert len(items) == rd.WAKE_ALONE + rd.WAKE_COMMANDS + rd.JARVIS
    engines = {engine for _, engine, _ in items}
    assert engines == {"vosk_kws", "openwakeword"}
    with_command = [item for item in items if item[2]]
    assert len(with_command) == rd.WAKE_COMMANDS
    assert all(p.text.startswith("Телекінез … ") for p, _, _ in with_command)

    manifest = tmp_path / "voice" / "wake_manifest.csv"
    chosen = [items[0], with_command[0]]
    replies = iter(["", ""])
    saved = rd.session(
        [p for p, _, _ in chosen], "roman", manifest, lambda: PCM, lambda pcm: None,
        ask=lambda _: next(replies), out=lambda _: None,
        wake={p.phrase_id: item for item in chosen for p in [item[0]]},
    )  # fmt: skip
    assert saved == 2
    rows = rd.read_manifest(manifest)
    assert list(rows[0]) == rd.WAKE_FIELDS
    alone, command = rows
    assert (alone["engine"], alone["wake"], alone["command"]) == ("vosk_kws", "1", "")
    assert Path(alone["file"]).parts[-3:-1] == ("wake", "roman")
    assert command["command"] == with_command[0][2]
    assert command["intent"]
