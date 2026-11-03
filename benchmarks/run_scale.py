"""E6: how the assistant copes with 10 / 25 / 50 / 100 simulated devices (metrics.md §2).

For each size a home is generated (instant device types only, so the numbers
show messaging and bookkeeping, not curtain motors), a separate simulator is
started on its own topic prefix, and the assistant measures:
  * time until every device is online after start;
  * ack latency of single commands sent one after another;
  * one group command to all devices: time until the last one completed;
  * CPU and RAM of the assistant and simulator processes, idle and under load.

    python benchmarks/run_scale.py                 # needs a running broker
    python benchmarks/run_scale.py --sizes 10 25 --commands 30
"""

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import psutil
import yaml
from common import (
    ROOT,
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
from teleckinez.core.models import Availability, DeviceCommand

TYPES = ("light", "socket", "fan", "tv", "boiler", "humidifier")  # converge right after the ack
PREFIX = "tkzscale"
ROOMS_PER_10 = 2


def generate_home(n: int) -> dict[str, Any]:
    rooms = max(1, n * ROOMS_PER_10 // 10)
    home: dict[str, Any] = {
        "language_default": "uk",
        "rooms": {f"room{r}": {"uk": [f"кімната {r}"], "en": [f"room {r}"]} for r in range(rooms)},
        "groups": {"all_scale": {"uk": ["усе"], "en": ["everything"]}},
        "devices": [],
    }
    for i in range(n):
        kind = TYPES[i % len(TYPES)]
        home["devices"].append(
            {
                "id": f"{kind}.d{i:03d}",
                "type": kind,
                "room": f"room{i % rooms}",
                "names": {"uk": [f"пристрій {i}"], "en": [f"device {i}"]},
                "groups": ["all_scale"],
            }
        )
    return home


def command_for(app: App, device_id: str) -> DeviceCommand:
    on = bool(app.twins.get(device_id).reported.get("on"))
    return DeviceCommand(device_id, "turn_off" if on else "turn_on")


def cpu_sample(procs: list[psutil.Process], seconds: float) -> list[float]:
    for p in procs:
        p.cpu_percent(None)
    time.sleep(seconds)
    return [p.cpu_percent(None) for p in procs]


async def measure(n: int, args, settings_base: dict, workdir: Path) -> dict[str, Any]:
    devices_file = workdir / f"devices_{n}.yaml"
    settings_file = workdir / f"settings_{n}.yaml"
    devices_file.write_text(yaml.safe_dump(generate_home(n), allow_unicode=True), "utf-8")
    cfg = dict(settings_base)
    # Fresh prefix (and so fresh client ids) per run: amqtt 0.12.1 crashes when many
    # clients reconnect with the ids of an earlier session.
    prefix = f"{PREFIX}{int(time.time()) % 100000}n{n}"
    cfg["mqtt"] = {**cfg.get("mqtt", {}), "prefix": prefix, "client_id": f"tkz-scale-{n}"}
    settings_file.write_text(yaml.safe_dump(cfg, allow_unicode=True), "utf-8")

    settings = load_settings(settings_file)
    settings.notifications.enabled = False
    app = App(settings, load_devices(devices_file), use_storage=False)
    me = psutil.Process(os.getpid())
    # Popen, not asyncio subprocesses: the selector loop used by aiomqtt on
    # Windows does not support them.
    sim = subprocess.Popen(  # noqa: ASYNC220
        [sys.executable, "-m", "simulator", "--config", str(devices_file),
         "--settings", str(settings_file), "--seed", "7",
         "--stagger-ms", str(args.stagger_ms)],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=open(workdir / "sim.log", "w"),  # noqa: ASYNC230, SIM115
    )  # fmt: skip
    row: dict[str, Any] = {"devices": n}
    try:
        await app.start(settle_s=0.1)
        started = time.monotonic()

        def online() -> int:
            return sum(
                app.twins.get(d.id).availability is Availability.ONLINE for d in app.registry
            )

        while online() < n and time.monotonic() - started < 60:  # noqa: ASYNC110
            await asyncio.sleep(0.05)
        row["online"] = online()
        row["startup_s"] = round(time.monotonic() - started, 2)
        sim_proc = psutil.Process(sim.pid)
        # a venv python.exe on Windows is a launcher: the simulator runs in its child
        sim_proc = next(iter(sim_proc.children(recursive=True)), sim_proc)
        procs = [me, sim_proc]
        await asyncio.sleep(2)
        idle = await asyncio.to_thread(cpu_sample, procs, args.idle_s)
        row["cpu_idle_assistant"], row["cpu_idle_sim"] = idle
        row["rss_assistant_mb"] = round(me.memory_info().rss / 2**20, 1)
        row["rss_sim_mb"] = round(sim_proc.memory_info().rss / 2**20, 1)

        # sequential single commands
        ids = [d.id for d in app.registry]
        acks, totals = [], []
        for p in procs:
            p.cpu_percent(None)
        t0 = time.monotonic()
        for k in range(args.commands):
            group = await app.dispatcher.dispatch([command_for(app, ids[k % n])])
            (record,) = await group.wait(30)
            d = record.durations_ms()
            if record.status.is_success:
                acks.append(d["ack"])
                totals.append(d["total"])
        load_s = time.monotonic() - t0
        row["cpu_load_assistant"], row["cpu_load_sim"] = (p.cpu_percent(None) for p in procs)
        row["single_ok"] = len(totals)
        for key, values in (("ack", acks), ("total", totals)):
            s = summary(values)
            row[f"{key}_p50_ms"], row[f"{key}_p95_ms"] = s["p50"], s["p95"]
        row["commands_per_s"] = round(args.commands / load_s, 1)

        # one group command to all devices
        await asyncio.sleep(0.5)
        group = await app.dispatcher.dispatch([command_for(app, i) for i in ids])
        records = await group.wait(60)
        finished = [r.t_finished for r in records if r.t_finished]
        row["group_ok"] = sum(r.status.is_success for r in records)
        row["group_s"] = round(max(finished) - min(r.t_created for r in records), 3)
        row["group_ack_p95_ms"] = summary([r.durations_ms()["ack"] for r in records if r.t_ack])[
            "p95"
        ]
        row["rss_assistant_after_mb"] = round(me.memory_info().rss / 2**20, 1)
    finally:
        await app.stop()
        if sim.poll() is not None:  # the simulator died: show why
            print((workdir / "sim.log").read_text(encoding="utf-8", errors="replace")[-2000:])
        sim.terminate()
        try:
            await asyncio.to_thread(sim.wait, 10)
        except subprocess.TimeoutExpired:
            sim.kill()
    return row


def plot(rows: list[dict[str, Any]], name: str) -> Path:
    plt = setup_plot()
    ns = [r["devices"] for r in rows]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(10, 4))
    a1.plot(ns, [r["ack_p50_ms"] for r in rows], "o-", color="black", label="ack, медіана")
    a1.plot(ns, [r["ack_p95_ms"] for r in rows], "s--", color="black", label="ack, p95")
    a1.plot(ns, [r["group_s"] * 1000 for r in rows], "^:", color="#555555",
            label="групова команда на всі")  # fmt: skip
    a1.set_xlabel("Кількість пристроїв")
    a1.set_ylabel("Час, мс")
    a1.set_xticks(ns)
    a1.legend(fontsize=9)
    width = 0.35
    xs = range(len(ns))
    a2.bar([x - width / 2 for x in xs], [r["cpu_idle_assistant"] for r in rows], width,
           label="асистент, простій", **bar_style(0))  # fmt: skip
    a2.bar([x + width / 2 for x in xs], [r["cpu_idle_sim"] for r in rows], width,
           label="симулятор, простій", **bar_style(2))  # fmt: skip
    a2.set_xticks(list(xs))
    a2.set_xticklabels([str(n) for n in ns])
    a2.set_xlabel("Кількість пристроїв")
    a2.set_ylabel("CPU, % одного ядра")
    a2.legend(fontsize=9)
    path = out_path(name, ".png")
    fig.savefig(path)
    plt.close(fig)
    return path


async def main_async(args) -> None:
    base = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for n in args.sizes:
            print(f"E6: {n} devices ...", flush=True)
            row = await measure(n, args, base, Path(tmp))
            rows.append(row)
            print(
                f"  online {row['online']}/{n} in {row['startup_s']} s; ack p50 "
                f"{fmt(row['ack_p50_ms'])} p95 {fmt(row['ack_p95_ms'])} ms; group "
                f"{row['group_ok']}/{n} in {row['group_s']} s; CPU idle "
                f"{row['cpu_idle_assistant']} / {row['cpu_idle_sim']} %; RSS "
                f"{row['rss_assistant_mb']} / {row['rss_sim_mb']} MB",
                flush=True,
            )
            await asyncio.sleep(2)
    csv_path = write_csv(args.name, rows)
    png = plot(rows, args.name)
    write_json(
        args.name,
        conditions(
            experiment="E6 scale",
            broker=args.broker,
            commands=args.commands,
            idle_s=args.idle_s,
            device_types=TYPES,
            heartbeat_s=10,
            results=rows,
        ),
    )
    print(f"-> {csv_path}\n-> {png}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sizes", nargs="+", type=int, default=[10, 25, 50, 100])
    ap.add_argument("--commands", type=int, default=100, help="sequential single commands")
    ap.add_argument("--idle-s", type=float, default=10.0, help="CPU sampling window at idle")
    ap.add_argument("--broker", default="mosquitto", help="label for the conditions file")
    ap.add_argument(
        "--stagger-ms",
        type=float,
        default=0.0,
        help="simulator: pause between device connects (amqtt needs ~20)",
    )
    ap.add_argument("--name", default="e6_scale")
    run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
