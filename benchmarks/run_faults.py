"""E4: does the assistant detect device failures, and how fast? (metrics.md §2)

Needs a broker and the simulator (preferably with a fixed seed):
    docker compose up -d mosquitto
    python -m simulator --seed 42
    python benchmarks/run_faults.py                  # all scenarios, N from the YAML
    python benchmarks/run_faults.py --only freeze --repeats 3

For every scenario of benchmarks/fault_scenarios.yaml: inject the fault through
the simulator's control topic, send a command through the real dispatcher,
record the final status, attempts and timings, clear the fault.
"""

import argparse
import asyncio
import contextlib
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml
from common import (
    BENCH,
    bar_style,
    conditions,
    fmt,
    out_path,
    setup_plot,
    summary,
    write_csv,
    write_json,
)

from teleckinez.app import App
from teleckinez.core.aio import run
from teleckinez.core.config import load_devices, load_settings
from teleckinez.core.events import DeviceAvailabilityChanged, DeviceStateChanged
from teleckinez.core.models import Availability, DeviceCommand

SCENARIOS = BENCH / "fault_scenarios.yaml"
SETTLE_S = 0.4  # pause between repetitions: late messages of the previous one die out


def opposite_command(device, reported: dict[str, Any]) -> DeviceCommand:
    """A command that really changes the state, so convergence must be observed."""
    dtype = device.type
    if dtype.has("locked"):
        return DeviceCommand(device.id, "unlock" if reported.get("locked", True) else "lock")
    if dtype.has("position"):
        return DeviceCommand(device.id, "close" if reported.get("position", 0) > 50 else "open")
    return DeviceCommand(device.id, "turn_off" if reported.get("on") else "turn_on")


class Probe:
    """Timestamps of the bus events E4 cares about."""

    def __init__(self, app: App) -> None:
        self.events: list[tuple[float, Any]] = []
        app.bus.subscribe(self._on, DeviceAvailabilityChanged)
        app.bus.subscribe(self._on, DeviceStateChanged)

    def _on(self, event) -> None:
        self.events.append((time.time(), event))

    async def wait_for(self, predicate, since: float, timeout_s: float) -> float | None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            for ts, event in self.events:
                if ts >= since and predicate(event):
                    return ts
            await asyncio.sleep(0.005)
        return None


async def wait_until(condition, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.01)
    return condition()


async def set_fault(app: App, device_id: str, fault: str, params: dict, enabled: bool) -> bool:
    """Inject / clear and wait until the simulator reports it (cmd and fault topics race)."""
    await app.send_fault(device_id, fault, params, enabled)
    if fault in ("drift", "clear"):
        return await wait_until(lambda: device_id not in app.sim_faults, 3.0) or fault == "drift"
    return await wait_until(lambda: (fault in app.sim_faults.get(device_id, {})) == enabled, 3.0)


async def one_run(app: App, probe: Probe, sc: dict[str, Any], rep: int) -> dict[str, Any]:
    device = app.registry.get(sc["device"])
    twin = app.twins.get(device.id)
    fault, params = sc.get("fault"), sc.get("params") or {}
    row: dict[str, Any] = {"scenario": sc["name"], "rep": rep, "device": device.id}
    t_fault = time.time()
    if fault:
        row["fault_confirmed"] = await set_fault(app, device.id, fault, params, True)
    detect_ts = None
    try:
        if sc["measure"] == "availability":
            detect_ts = await probe.wait_for(
                lambda e: (
                    isinstance(e, DeviceAvailabilityChanged)
                    and e.device_id == device.id
                    and e.availability is Availability.OFFLINE
                ),
                t_fault,
                10.0,
            )
        if fault == "drift":
            detect_ts = await probe.wait_for(
                lambda e: (
                    isinstance(e, DeviceStateChanged)
                    and e.device_id == device.id
                    and e.source == "drift"
                ),
                t_fault,
                5.0,
            )
            row["status"] = "drift" if detect_ts else "missed"
        else:
            command = opposite_command(device, twin.reported)
            group = await app.dispatcher.dispatch([command], session_id=f"e4-{sc['name']}-{rep}")
            (record,) = await group.wait(60)
            durations = record.durations_ms()
            row.update(
                action=command.action,
                status=str(record.status),
                attempts=record.attempts,
                error=record.error or "",
                ack_ms=durations["ack"],
                total_ms=durations["total"],
                after_ack_ms=(
                    round((record.t_finished - record.t_ack) * 1000, 1)
                    if record.t_ack and record.t_finished
                    else None
                ),
            )
            if sc["measure"] == "total":
                row["detect_ms"] = durations["total"]
            elif sc["measure"] == "after_ack":
                row["detect_ms"] = row["after_ack_ms"]
        if detect_ts is not None:
            row["detect_ms"] = round((detect_ts - t_fault) * 1000, 1)
    finally:
        if fault and fault != "drift":
            await set_fault(app, device.id, "clear", {}, True)
        if fault == "offline":
            await wait_until(lambda: twin.availability is Availability.ONLINE, 15.0)
        await asyncio.sleep(sc.get("settle_s", SETTLE_S))
    row["correct"] = row.get("status") in sc["expect"]
    return row


def aggregate(rows: list[dict[str, Any]], scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    by = defaultdict(list)
    for r in rows:
        by[r["scenario"]].append(r)
    out = {}
    for sc in scenarios:
        mine = by.get(sc["name"], [])
        if not mine:
            continue
        statuses: dict[str, int] = defaultdict(int)
        for r in mine:
            statuses[r.get("status", "?")] += 1
        retried = [r for r in mine if (r.get("attempts") or 0) > 1]
        out[sc["name"]] = {
            "title": sc["title"],
            "n": len(mine),
            "correct": sum(r["correct"] for r in mine),
            "correct_pct": 100 * sum(r["correct"] for r in mine) / len(mine),
            "statuses": dict(statuses),
            "detect_ms": summary([r["detect_ms"] for r in mine if r.get("detect_ms") is not None]),
            "attempts_mean": (
                sum(r.get("attempts") or 0 for r in mine) / len(mine)
                if any("attempts" in r for r in mine)
                else None
            ),
            "retried": len(retried),
            "recovered_by_retry": sum(r["status"] == "completed" for r in retried),
            "false_alarms": (sum(not r["correct"] for r in mine) if sc.get("control") else None),
        }
    return out


def plot(stats: dict[str, Any], name: str) -> Path:
    plt = setup_plot()
    names = list(stats)[::-1]
    p50 = [(stats[n]["detect_ms"]["p50"] or 0) / 1000 for n in names]
    p95 = [(stats[n]["detect_ms"]["p95"] or 0) / 1000 for n in names]
    fig, ax = plt.subplots(figsize=(8.5, 0.55 * len(names) + 1.2))
    ys = range(len(names))
    ax.barh(ys, p50, 0.6, label="медіана", **bar_style(1))
    ax.errorbar(p95, ys, fmt="|", color="black", markersize=12, label="p95")
    for y, n, v in zip(ys, names, p95, strict=True):
        s = stats[n]
        ax.text(v + 0.15, y, f"{s['correct']}/{s['n']}", va="center", fontsize=10)
    ax.set_yticks(list(ys))
    ax.set_yticklabels([stats[n]["title"] for n in names], fontsize=10)
    ax.set_xlabel("Час виявлення / реакції, с")
    ax.set_xlim(0, max(p95) * 1.18 + 0.5)
    ax.legend(loc="lower right")
    path = out_path(name, ".png")
    fig.savefig(path)
    plt.close(fig)
    return path


async def main_async(args) -> None:
    spec = args.spec
    repeats = args.repeats or spec["repeats"]
    scenarios = [s for s in spec["scenarios"] if not args.only or s["name"] in args.only]
    settings = load_settings()
    settings.notifications.enabled = False
    app = App(settings, load_devices(), use_storage=False)
    probe = Probe(app)
    rows: list[dict[str, Any]] = []
    await app.start(settle_s=2.0)
    try:
        offline = [
            d.id
            for d in app.registry
            if app.twins.get(d.id).availability is not Availability.ONLINE
        ]
        if offline:
            raise SystemExit(f"simulator not running or devices offline: {offline[:5]}")
        await app.send_fault("*", "clear", {}, True)
        await asyncio.sleep(0.5)
        for sc in scenarios:
            print(f"E4 {sc['name']}: ", end="", flush=True)
            for rep in range(1, repeats + 1):
                row = await one_run(app, probe, sc, rep)
                rows.append(row)
                print("." if row["correct"] else "x", end="", flush=True)
            print()
    finally:
        with contextlib.suppress(Exception):
            await app.send_fault("*", "clear", {}, True)
        await app.stop()
    stats = aggregate(rows, scenarios)
    csv_path = write_csv(args.name, rows)
    png = plot(stats, args.name)
    write_json(
        args.name,
        conditions(
            experiment="E4 faults",
            repeats=repeats,
            broker=args.broker,
            dispatcher=settings.dispatcher.model_dump(),
            health=settings.health.model_dump(),
            simulator_latency_ms=settings.simulator.latency_ms,
            results=stats,
        ),
    )
    for n, s in stats.items():
        d = s["detect_ms"]
        print(
            f"{n:15s} {s['correct']:2d}/{s['n']}  p50 {fmt(d['p50'], 0)} ms  "
            f"p95 {fmt(d['p95'], 0)} ms  statuses {s['statuses']}  "
            f"retried {s['retried']} recovered {s['recovered_by_retry']}"
        )
    print(f"-> {csv_path}\n-> {png}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scenarios", default=str(SCENARIOS))
    ap.add_argument("--only", nargs="+", help="scenario names")
    ap.add_argument("--repeats", type=int, default=0, help="override N from the YAML")
    ap.add_argument("--broker", default="mosquitto", help="label for the conditions file")
    ap.add_argument("--name", default="e4_faults")
    args = ap.parse_args()
    args.spec = yaml.safe_load(Path(args.scenarios).read_text(encoding="utf-8"))
    run(main_async(args))


if __name__ == "__main__":
    main()
