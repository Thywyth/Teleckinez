import teleckinez
from teleckinez.__main__ import main


def test_version() -> None:
    assert teleckinez.__version__


def test_main_without_text_explains_text_mode(capsys) -> None:
    assert main([]) == 2  # voice pipeline arrives in stage 2
    assert "--text" in capsys.readouterr().out
