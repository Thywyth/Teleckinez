import teleckinez
from teleckinez.__main__ import main


def test_version() -> None:
    assert teleckinez.__version__


def test_main_without_any_frontend_explains_modes(capsys) -> None:
    assert main(["--no-web"]) == 2  # voice pipeline arrives in stage 2
    assert "--text" in capsys.readouterr().out


def test_all_modules_import() -> None:
    """Modules that tests do not touch directly (e.g. repl) must at least import."""
    import importlib
    import pkgutil

    import simulator

    for package in (teleckinez, simulator):
        for info in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
            if ".voice.stt" in info.name:  # stage 2.3 work in progress, not committed yet
                continue
            importlib.import_module(info.name)
