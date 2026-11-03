"""Render docs/diagrams/*.puml to PNG for the report (REPORT_PLAN §3).

Uses the PlantUML jar (Java). The jar is downloaded once into build/tools/;
version 1.2023.13 is the newest that still runs on Java 8. Layout uses the
built-in Smetana engine (`!pragma layout smetana`), so Graphviz is not needed.

    python scripts/render_diagrams.py              # all diagrams -> docs/diagrams/png/
    python scripts/render_diagrams.py 06_command_states
"""

import argparse
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "docs" / "diagrams"
OUT = SRC / "png"
JAR = ROOT / "build" / "tools" / "plantuml.jar"
JAR_URL = "https://github.com/plantuml/plantuml/releases/download/v1.2023.13/plantuml-1.2023.13.jar"


def ensure_jar() -> Path:
    if not JAR.exists():
        JAR.parent.mkdir(parents=True, exist_ok=True)
        print(f"downloading PlantUML -> {JAR}")
        urllib.request.urlretrieve(JAR_URL, JAR)
    return JAR


def render(names: list[str]) -> list[Path]:
    sources = sorted(SRC.glob("[0-9]*.puml"))
    if names:
        sources = [s for s in sources if s.stem in names]
    if not sources:
        raise SystemExit(f"no diagrams matched {names}")
    OUT.mkdir(parents=True, exist_ok=True)
    cmd = ["java", "-Djava.awt.headless=true", "-jar", str(ensure_jar()),
           "-tpng", "-charset", "UTF-8", "-o", str(OUT), *map(str, sources)]  # fmt: skip
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.stderr.write(result.stdout + result.stderr)
        raise SystemExit(f"PlantUML failed (exit {result.returncode})")
    return [OUT / f"{s.stem}.png" for s in sources]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("names", nargs="*", help="diagram names without .puml (default: all)")
    for png in render(ap.parse_args().names):
        print(png.relative_to(ROOT))


if __name__ == "__main__":
    main()
