"""Shared helpers for the experiments (docs/specs/metrics.md).

Every run_*.py writes `results/<name>_<date>.csv` with the measurements, a
`.json` file with the conditions (hardware, versions, settings) and a
print-ready monochrome `.png` with Ukrainian labels.
"""

import csv
import datetime as dt
import importlib.metadata
import json
import platform
import statistics
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "benchmarks"
DATASETS = BENCH / "datasets"
RESULTS = BENCH / "results"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

# Grey levels and hatches: series stay distinguishable on a black-and-white printout.
GREYS = ["#222222", "#777777", "#bbbbbb", "#ffffff"]
HATCHES = ["", "//", "..", "xx"]


def stamp() -> str:
    return dt.date.today().isoformat()


def out_path(name: str, suffix: str) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    return RESULTS / f"{name}_{stamp()}{suffix}"


def write_csv(name: str, rows: Sequence[dict[str, Any]]) -> Path:
    path = out_path(name, ".csv")
    fields: list[str] = []
    for row in rows:
        fields += [k for k in row if k not in fields]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def write_json(name: str, data: dict[str, Any]) -> Path:
    path = out_path(name, ".json")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _run(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def conditions(**extra: Any) -> dict[str, Any]:
    """Hardware and software the numbers were measured on (metrics.md §3)."""
    import psutil

    gpu = _run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.used,memory.total,driver_version",
            "--format=csv,noheader",
        ]
    )
    cpu = platform.processor()
    if sys.platform == "win32":
        name = _run(
            ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).Name"]
        )
        cpu = name or cpu
    packages = (
        "aiomqtt", "amqtt", "rapidfuzz", "ollama", "vosk", "faster-whisper", "ctranslate2",
        "piper-tts", "openwakeword", "onnxruntime", "webrtcvad-wheels", "fastapi",
    )  # fmt: skip
    return {
        "date": dt.datetime.now().isoformat(timespec="seconds"),
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "cpu": cpu,
        "cpu_count": psutil.cpu_count(),
        "ram_gb": round(psutil.virtual_memory().total / 2**30, 1),
        "gpu": gpu or None,
        "python": platform.python_version(),
        "packages": {p: v for p in packages if (v := _version(p))},
        **extra,
    }


def percentile(values: Iterable[float], q: float) -> float | None:
    data = sorted(values)
    if not data:
        return None
    if len(data) == 1:
        return data[0]
    k = (len(data) - 1) * q / 100
    lo, hi = int(k), min(int(k) + 1, len(data) - 1)
    return data[lo] + (data[hi] - data[lo]) * (k - lo)


def summary(values: Sequence[float]) -> dict[str, float | None]:
    vals = list(values)
    return {
        "n": len(vals),
        "mean": statistics.fmean(vals) if vals else None,
        "p50": percentile(vals, 50),
        "p95": percentile(vals, 95),
        "max": max(vals) if vals else None,
    }


def setup_plot():
    """matplotlib in a print-ready monochrome style; returns pyplot."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "font.family": "serif",
            "font.serif": ["Times New Roman", "DejaVu Serif"],
            "font.size": 12,
            "axes.edgecolor": "black",
            "axes.labelcolor": "black",
            "axes.grid": True,
            "grid.color": "#cccccc",
            "grid.linestyle": "--",
            "grid.linewidth": 0.6,
            "xtick.color": "black",
            "ytick.color": "black",
            "legend.frameon": True,
            "legend.edgecolor": "black",
            "savefig.dpi": 200,
            "savefig.bbox": "tight",
            "figure.figsize": (7.5, 4.2),
        }
    )
    return plt


def bar_style(i: int) -> dict[str, Any]:
    return {
        "color": GREYS[i % len(GREYS)],
        "hatch": HATCHES[i % len(HATCHES)],
        "edgecolor": "black",
        "linewidth": 0.8,
    }


def fmt(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def fmt_uk(value: float | None, digits: int = 1) -> str:
    """Decimal comma for Ukrainian labels: 0,28."""
    return fmt(value, digits).replace(".", ",")
