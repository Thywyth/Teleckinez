"""E2: RuleParser vs LlmParser vs hybrid on datasets/nlu_commands.yaml (metrics.md §2).

Each phrase goes through the parser and the TargetResolver, and the outcome
(kind, action, devices, parameters, or an error class) is compared with the
label. Before an LLM run check `ollama ps`: the PROCESSOR column must say GPU,
otherwise the latency measures the CPU, not the method (metrics.md).

    python benchmarks/run_nlu.py                    # rule, llm, hybrid
    python benchmarks/run_nlu.py --modes rule       # no Ollama needed
"""

import argparse
import math
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from common import (
    DATASETS,
    bar_style,
    conditions,
    fmt,
    out_path,
    setup_plot,
    summary,
    write_csv,
    write_json,
)

from teleckinez.core.aio import run
from teleckinez.core.config import load_devices, load_settings
from teleckinez.devices.registry import DeviceRegistry
from teleckinez.nlu.llm_parser import LlmParser, ModeParser
from teleckinez.nlu.resolver import TargetResolver
from teleckinez.nlu.rule_parser import RuleParser

DATASET = DATASETS / "nlu_commands.yaml"
MODES = ("rule", "llm", "hybrid")
MODE_UK = {"rule": "Правила", "llm": "LLM", "hybrid": "Гібрид"}
CATEGORY_UK = {
    "basic": "базові", "case": "відмінки", "noverb": "без дієслова", "group": "групові",
    "param": "параметри", "step": "відносні", "query": "запити", "ambiguous": "неоднозначні",
    "stt": "помилки STT", "free": "вільні", "reject": "сторонні",
}  # fmt: skip
AMBIGUOUS = {"ambiguous_target", "ambiguous_device"}


def load_dataset(path: Path = DATASET) -> list[dict[str, Any]]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@dataclass
class Outcome:
    kind: str | None = None  # command | query | None (error)
    error: str | None = None  # parser/resolver reason
    actions: set[str] = field(default_factory=set)
    devices: set[str] = field(default_factory=set)
    params: dict[str, Any] = field(default_factory=dict)
    parser: str | None = None  # who produced the intent: rule | llm

    def describe(self) -> str:
        if self.error:
            return f"error:{self.error}"
        parts = [self.kind or "?", ",".join(sorted(self.actions)), ",".join(sorted(self.devices))]
        if self.params:
            parts.append(str(self.params))
        return " ".join(p for p in parts if p)


def outcome_of(result, resolver: TargetResolver, registry: DeviceRegistry) -> Outcome:
    if not result.ok:
        return Outcome(error=result.reason or "not_understood")
    intent = result.intent
    resolution = resolver.resolve(intent)
    if not resolution.ok:
        return Outcome(error=resolution.error, parser=intent.parser)
    out = Outcome(kind=resolution.kind, parser=intent.parser)
    if resolution.kind == "query":
        out.devices = {d.id for d in resolution.devices}
        return out
    for command in resolution.commands:
        dtype = registry.get(command.device_id).type
        out.actions.add(dtype.aliases.get(command.action, command.action))
        out.devices.add(command.device_id)
        out.params.update(command.params)
    return out


def _same_params(expected: dict[str, Any], got: dict[str, Any]) -> bool:
    if expected.keys() != got.keys():
        return False
    for key, value in expected.items():
        other = got[key]
        if isinstance(value, int | float) and isinstance(other, int | float):
            if not math.isclose(value, other, abs_tol=1e-6):
                return False
        elif value != other:
            return False
    return True


def score(expect: dict[str, Any], got: Outcome) -> dict[str, bool | None]:
    """intent: kind + action (or the right refusal); devices / params: None if not labelled."""
    error = expect.get("error")
    if error == "ambiguous":
        ok = got.error in AMBIGUOUS
        return {"intent_ok": ok, "devices_ok": None, "params_ok": None, "full_ok": ok}
    if error == "reject":
        ok = got.error is not None
        return {"intent_ok": ok, "devices_ok": None, "params_ok": None, "full_ok": ok}
    kind = expect["kind"]
    if kind == "query":
        intent_ok = got.kind == "query"
        wanted = set(expect.get("devices", []))
        devices_ok = (intent_ok and wanted <= got.devices) if wanted else None
        return {
            "intent_ok": intent_ok,
            "devices_ok": devices_ok,
            "params_ok": None,
            "full_ok": intent_ok and devices_ok is not False,
        }
    intent_ok = got.kind == "command" and got.actions == {expect["action"]}
    devices_ok = got.kind == "command" and got.devices == set(expect["devices"])
    params_ok = None
    if "params" in expect or got.params:
        params_ok = got.kind == "command" and _same_params(expect.get("params", {}), got.params)
    return {
        "intent_ok": intent_ok,
        "devices_ok": devices_ok,
        "params_ok": params_ok,
        "full_ok": intent_ok and devices_ok and params_ok is not False,
    }


def build_parser(mode: str, registry: DeviceRegistry, settings) -> ModeParser:
    nlu = settings.nlu
    rules = RuleParser(
        registry, fuzzy_threshold=nlu.fuzzy_threshold, min_confidence=nlu.min_confidence
    )
    llm = None
    if mode != "rule":
        llm = LlmParser(
            registry,
            model=nlu.llm_model,
            host=nlu.ollama_host,
            timeout_s=nlu.llm_timeout_s,
            keep_alive=nlu.llm_keep_alive,
        )
    return ModeParser(rules, llm, mode, nlu.hybrid_threshold)


async def evaluate(mode: str, items, registry, settings, repeats: int) -> list[dict[str, Any]]:
    parser = build_parser(mode, registry, settings)
    resolver = TargetResolver(registry)
    if parser.llm is not None:
        await parser.llm.warmup()
    rows = []
    for n, item in enumerate(items, 1):
        times = []
        for _ in range(repeats if mode == "rule" else 1):
            started = time.perf_counter()
            result = await parser.aparse(item["text"])
            times.append((time.perf_counter() - started) * 1000)
        got = outcome_of(result, resolver, registry)
        marks = score(item["expect"], got)
        llm_error = parser.llm.last_error if parser.llm is not None else None
        rows.append(
            {
                "mode": mode,
                "text": item["text"],
                "lang": item["lang"],
                "cat": item["cat"],
                "expected": _describe_expect(item["expect"]),
                "got": got.describe(),
                **marks,
                "latency_ms": round(sum(times) / len(times), 3),
                "parser_used": got.parser or "",
                "llm_error": llm_error or "",
            }
        )
        if mode != "rule" and n % 25 == 0:
            print(f"  {mode}: {n}/{len(items)}", flush=True)
    return rows


def _describe_expect(expect: dict[str, Any]) -> str:
    if "error" in expect:
        return f"error:{expect['error']}"
    parts = [expect["kind"], expect.get("action", ""), ",".join(sorted(expect.get("devices", [])))]
    if expect.get("params"):
        parts.append(str(expect["params"]))
    return " ".join(p for p in parts if p)


def share(rows, key: str) -> float | None:
    marks = [r[key] for r in rows if r[key] is not None]
    return 100 * sum(marks) / len(marks) if marks else None


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_mode: dict[str, Any] = {}
    for mode in dict.fromkeys(r["mode"] for r in rows):
        mine = [r for r in rows if r["mode"] == mode]
        cats = defaultdict(list)
        for r in mine:
            cats[r["cat"]].append(r)
        langs = defaultdict(list)
        for r in mine:
            langs[r["lang"]].append(r)
        by_mode[mode] = {
            "n": len(mine),
            "full_accuracy": share(mine, "full_ok"),
            "intent_accuracy": share(mine, "intent_ok"),
            "device_accuracy": share(mine, "devices_ok"),
            "params_accuracy": share(mine, "params_ok"),
            "latency_ms": summary([r["latency_ms"] for r in mine]),
            "llm_used": sum(r["parser_used"] == "llm" for r in mine),
            "llm_errors": sum(bool(r["llm_error"]) for r in mine),
            "by_category": {c: share(rs, "full_ok") for c, rs in cats.items()},
            "category_n": {c: len(rs) for c, rs in cats.items()},
            "by_lang": {lang: share(rs, "full_ok") for lang, rs in langs.items()},
        }
    return by_mode


def plot(stats: dict[str, Any], name: str) -> list[Path]:
    plt = setup_plot()
    modes = list(stats)
    cats = list(next(iter(stats.values()))["by_category"])
    width = 0.8 / len(modes)
    fig, ax = plt.subplots(figsize=(9, 4.5))
    for i, mode in enumerate(modes):
        xs = [c + (i - (len(modes) - 1) / 2) * width for c in range(len(cats))]
        ys = [stats[mode]["by_category"].get(c) or 0 for c in cats]
        ax.bar(xs, ys, width, label=MODE_UK[mode], **bar_style(i))
    n = next(iter(stats.values()))["category_n"]
    ax.set_xticks(range(len(cats)))
    ax.set_xticklabels([f"{CATEGORY_UK.get(c, c)}\n(n={n[c]})" for c in cats], fontsize=9)
    ax.set_ylabel("Точність повного збігу, %")
    ax.set_ylim(0, 105)
    ax.legend(loc="lower left")
    accuracy_png = out_path(f"{name}_accuracy", ".png")
    fig.savefig(accuracy_png)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6.5, 4))
    p50 = [stats[m]["latency_ms"]["p50"] for m in modes]
    p95 = [stats[m]["latency_ms"]["p95"] for m in modes]
    xs = range(len(modes))
    ax.bar([x - 0.18 for x in xs], p50, 0.36, label="медіана (p50)", **bar_style(0))
    ax.bar([x + 0.18 for x in xs], p95, 0.36, label="p95", **bar_style(2))
    ax.set_yscale("log")
    ax.set_xticks(list(xs))
    ax.set_xticklabels([MODE_UK[m] for m in modes])
    ax.set_ylabel("Затримка розбору, мс (лог. шкала)")
    for x, a, b in zip(xs, p50, p95, strict=True):
        ax.text(x - 0.18, a * 1.15, fmt(a, 2 if a < 10 else 0), ha="center", fontsize=9)
        ax.text(x + 0.18, b * 1.15, fmt(b, 2 if b < 10 else 0), ha="center", fontsize=9)
    ax.legend(loc="upper left")
    latency_png = out_path(f"{name}_latency", ".png")
    fig.savefig(latency_png)
    plt.close(fig)
    return [accuracy_png, latency_png]


def ollama_ps() -> str:
    try:
        return subprocess.run(["ollama", "ps"], capture_output=True, text=True, timeout=10).stdout
    except OSError:
        return "ollama not found"


async def main_async(args) -> None:
    settings = load_settings()
    registry = DeviceRegistry(load_devices())
    items = load_dataset(Path(args.dataset))[: args.limit or None]
    rows: list[dict[str, Any]] = []
    ps_before = ps_after = None
    for mode in args.modes:
        print(f"E2: {mode}, {len(items)} phrases", flush=True)
        rows += await evaluate(mode, items, registry, settings, args.repeats)
        if mode != "rule":
            ps_after = ollama_ps()
            ps_before = ps_before or ps_after
    stats = aggregate(rows)
    csv_path = write_csv(args.name, rows)
    pngs = plot(stats, args.name)
    meta = conditions(
        experiment="E2 NLU",
        dataset=str(Path(args.dataset).relative_to(DATASETS.parents[1])),
        phrases=len(items),
        rule_repeats=args.repeats,
        nlu_settings=settings.nlu.model_dump(),
        ollama_ps=ps_after,
        results=stats,
    )
    write_json(args.name, meta)
    for mode, s in stats.items():
        lat = s["latency_ms"]
        print(
            f"{mode:7s} full {fmt(s['full_accuracy'])} %  intent {fmt(s['intent_accuracy'])} %  "
            f"devices {fmt(s['device_accuracy'])} %  params {fmt(s['params_accuracy'])} %  "
            f"p50 {fmt(lat['p50'], 2)} ms  p95 {fmt(lat['p95'], 2)} ms  llm used {s['llm_used']}"
        )
    print(f"-> {csv_path}\n-> {', '.join(str(p) for p in pngs)}")
    if ps_after:
        print(ps_after)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    ap.add_argument("--dataset", default=str(DATASET))
    ap.add_argument("--limit", type=int, default=0, help="first N phrases only (smoke test)")
    ap.add_argument("--repeats", type=int, default=20, help="rule mode: timing repeats")
    ap.add_argument("--name", default="e2_nlu")
    run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
