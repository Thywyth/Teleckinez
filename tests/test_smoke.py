import pytest

import teleckinez
from teleckinez.__main__ import main


def test_version() -> None:
    assert teleckinez.__version__


def test_main_without_any_frontend_is_an_error(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--no-web", "--no-voice"])
    assert exc.value.code == 2 and "nothing to run" in capsys.readouterr().err


def test_all_modules_import() -> None:
    """Modules that tests do not touch directly (e.g. repl) must at least import."""
    import importlib
    import pkgutil

    import simulator

    for package in (teleckinez, simulator):
        for info in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
            importlib.import_module(info.name)
