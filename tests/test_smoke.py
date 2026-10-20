import teleckinez
from teleckinez.__main__ import main


def test_version() -> None:
    assert teleckinez.__version__


def test_main_without_any_frontend_explains_modes(capsys) -> None:
    assert main(["--no-web"]) == 2  # voice pipeline arrives in stage 2
    assert "--text" in capsys.readouterr().out
