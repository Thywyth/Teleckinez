import teleckinez
from teleckinez.__main__ import main


def test_version() -> None:
    assert teleckinez.__version__


def test_main_runs(capsys) -> None:
    assert main([]) == 0
    assert "Teleckinez" in capsys.readouterr().out
