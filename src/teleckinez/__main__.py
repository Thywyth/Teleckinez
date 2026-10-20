"""Composition root. Wires modules together according to config/settings.yaml."""

import argparse

from teleckinez import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="teleckinez", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--text", action="store_true", help="read commands from the keyboard")
    parser.parse_args(argv)
    print(f"Teleckinez {__version__}: not implemented yet (see docs/ROADMAP.md)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
