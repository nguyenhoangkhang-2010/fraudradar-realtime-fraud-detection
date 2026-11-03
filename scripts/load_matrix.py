"""Run the load test against each scorer configuration and collect the results. Needs Docker and the PaySim CSV.

    python scripts/load_matrix.py
    python scripts/load_matrix.py --configs lightgbm-redis-1,lightgbm-redis-3 --rates 500,1000 --duration 10

For each configuration it recreates the stack (broker, Redis where used, the scorers; not the simulator or the
api), waits for the scorers, runs `fraudradar.loadtest` in the Compose network, and samples `docker stats` once a
second so each step can be put next to the CPU the containers used. Results go to docs/results/load_test/.

The first Redis configuration starts from an empty Redis volume (`down -v`) so the runs do not depend on what an
earlier session left there.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "results" / "load_test"
REDIS_URL = "redis://redis:6379/0"

BASE, PAYSIM, REDIS, LOAD = (
    ["docker-compose.yml"], ["docker-compose.paysim.yml"], ["docker-compose.redis.yml"], ["docker-compose.loadtest.yml"]
)

CONFIGS: dict[str, dict] = {
    "heuristic-memory-1": dict(files=BASE, scorers=1, partitions=1, source="synthetic", redis=False,
                               what="hand-weighted heuristic, state in the scorer's memory, 1 scorer (synthetic payments)"),
    "lightgbm-memory-1": dict(files=BASE + PAYSIM, scorers=1, partitions=1, source="paysim", redis=False,
                              what="LightGBM, state in memory, 1 scorer, one payment at a time"),
    "lightgbm-redis-1": dict(files=BASE + PAYSIM + REDIS, scorers=1, partitions=1, source="paysim", redis=True,
                             what="LightGBM, state in Redis, 1 scorer, 1 partition, batched"),
    "lightgbm-redis-2": dict(files=BASE + PAYSIM + REDIS, scorers=2, partitions=2, source="paysim", redis=True,
                             what="LightGBM, state in Redis, 2 scorers, 2 partitions"),
    "lightgbm-redis-3": dict(files=BASE + PAYSIM + REDIS, scorers=3, partitions=3, source="paysim", redis=True,
                             what="LightGBM, state in Redis, 3 scorers, 3 partitions"),
    # The same, but LightGBM may use every core for each batch (its own default): what the first runs did.
    "lightgbm-redis-1-allthreads": dict(files=BASE + PAYSIM + REDIS, scorers=1, partitions=1, source="paysim",
                                        redis=True, threads=0,
                                        what="as lightgbm-redis-1, but LightGBM uses all cores for each batch"),
    "lightgbm-redis-3-allthreads": dict(files=BASE + PAYSIM + REDIS, scorers=3, partitions=3, source="paysim",
                                        redis=True, threads=0,
                                        what="as lightgbm-redis-3, but LightGBM uses all cores for each batch"),
}


def compose(files: list[str], *args: str, env: dict | None = None, check: bool = True, capture: bool = False):
    cmd = ["docker", "compose"]
    for f in files:
        cmd += ["-f", f]
    cmd += list(args)
    if not capture:
        print("+", " ".join(cmd), flush=True)
    return subprocess.run(
        cmd, cwd=ROOT, env={**os.environ, **(env or {})}, check=check,
        capture_output=capture, text=True, encoding="utf-8",
    )


class StatsSampler(threading.Thread):
    """`docker stats` once a second: (epoch ms, container, cpu percent of one core, memory MiB)."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.samples: list[tuple[int, str, float, float]] = []
        self._halt = threading.Event()

    def run(self) -> None:
        while not self._halt.is_set():
            out = subprocess.run(
                ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                capture_output=True, text=True, encoding="utf-8", check=False,
            ).stdout
            now = int(time.time() * 1000)
            for line in out.splitlines():
                try:
                    d = json.loads(line)
                    mem = d["MemUsage"].split("/")[0].strip()
                    unit = mem[-3:]
                    value = float(mem[:-3]) * {"KiB": 1 / 1024, "MiB": 1, "GiB": 1024}.get(unit, 1)
                    self.samples.append((now, d["Name"], float(d["CPUPerc"].rstrip("%")), round(value, 1)))
                except (ValueError, KeyError):
                    continue

    def stop(self) -> None:
        self._halt.set()


def wait_for_scorers(files: list[str], n: int, env: dict, timeout: float = 180.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        ids = compose(files, "ps", "-q", "scorer", env=env, capture=True).stdout.split()
        ready = 0
        for cid in ids:
            logs = subprocess.run(["docker", "logs", cid], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8").stdout  # python logs to stderr
            ready += "scorer running" in logs
        if ready >= n:
            return
        time.sleep(2)
    raise SystemExit(f"{n} scorer(s) did not come up in {timeout:.0f} s")


def cpu_per_step(samples: list, steps: list[dict]) -> None:
    """Attach, to every step, the mean CPU of the scorers, Redis and the broker over its window."""
    for step in steps:
        start, end = step["window_epoch_ms"]
        mean: dict[str, list[float]] = {}
        for ts, name, cpu, _ in samples:
            if start <= ts <= end:
                group = "scorer" if "scorer" in name else "redis" if "redis" in name else "redpanda" if "redpanda" in name else None
                if group:
                    mean.setdefault(group + "|" + name, []).append(cpu)
        per_container = {k.split("|")[1]: round(sum(v) / len(v), 0) for k, v in mean.items()}
        step["cpu_percent_of_one_core"] = {
            "scorers_each": [v for k, v in sorted(per_container.items()) if "scorer" in k],
            "redis": next((v for k, v in per_container.items() if "redis" in k), None),
            "redpanda": next((v for k, v in per_container.items() if "redpanda" in k), None),
        }


def run_config(name: str, cfg: dict, args: argparse.Namespace, first_redis: bool) -> dict:
    files = cfg["files"]
    env = {"PARTITIONS": str(cfg["partitions"]), "LGBM_THREADS": str(cfg.get("threads", 1))}
    all_files = files + LOAD
    compose(all_files, "down", *(["-v"] if cfg["redis"] and first_redis else []), env=env, check=False)
    if cfg["redis"]:
        compose(all_files, "up", "-d", "redis", env=env)
        compose(all_files, "run", "--rm", "-T", "warmup", env=env)
    compose(all_files, "up", "-d", "--build", "--scale", f"scorer={cfg['scorers']}", "scorer", env=env)
    wait_for_scorers(all_files, cfg["scorers"], env)

    sampler = StatsSampler()
    sampler.start()
    out_file = f"{name}{args.suffix}.json"
    cmd = ["run", "--rm", "-T", "loadtest", "python", "-m", "fraudradar.loadtest",
           "--rates", args.rates, "--duration", str(args.duration), "--source", cfg["source"],
           "--out", f"/out/{out_file}"]
    if cfg["redis"]:
        cmd += ["--redis", REDIS_URL]
    try:
        compose(all_files, *cmd, env=env)
    finally:
        sampler.stop()
        sampler.join(timeout=10)
    result = json.loads((OUT / out_file).read_text(encoding="utf-8"))
    cpu_per_step(sampler.samples, result["steps"])
    result.update(name=name, what=cfg["what"], scorers=cfg["scorers"], partitions=cfg["partitions"],
                  memory_mib_max={n: max(m for _, nm, _, m in sampler.samples if nm == n)
                                  for n in {nm for _, nm, _, _ in sampler.samples} if "scorer" in n or "redis" in n})
    (OUT / out_file).write_text(json.dumps(result, indent=1), encoding="utf-8")
    compose(all_files, "down", env=env, check=False)
    return result


def table(results: list[dict]) -> str:
    lines = ["| Configuration | Rate (tx/s) | Decided in window (tx/s) | p50 (ms) | p99 (ms) | max (ms) | Lost | CPU of the busiest scorer |",
             "|---|---|---|---|---|---|---|---|"]
    for r in results:
        for s in r["steps"]:
            cpu = max(s.get("cpu_percent_of_one_core", {}).get("scorers_each") or [float("nan")])
            lines.append(
                f"| {r['name']} | {s['target_rate']} | {s['decided_in_window_rate']:.0f} | {s['e2e_ms']['p50']:.0f} | "
                f"{s['e2e_ms']['p99']:.0f} | {s['e2e_ms']['max']:.0f} | {s['lost']} | {cpu:.0f}%"
                f"{' (saturated)' if s['saturated'] else ''} |"
            )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--configs", default=",".join(CONFIGS), help="comma separated; default all")
    parser.add_argument("--rates", default="250,500,1000,2000,4000,8000")
    parser.add_argument("--duration", type=int, default=15)
    parser.add_argument("--tag", default="", help="suffix for the result files, so a second run does not overwrite the first")
    args = parser.parse_args()
    args.suffix = f".{args.tag}" if args.tag else ""

    OUT.mkdir(parents=True, exist_ok=True)
    names = args.configs.split(",")
    unknown = [n for n in names if n not in CONFIGS]
    if unknown:
        sys.exit(f"unknown configuration(s) {unknown}; choose from {list(CONFIGS)}")
    results, seen_redis = [], False
    for name in names:
        cfg = CONFIGS[name]
        results.append(run_config(name, cfg, args, first_redis=cfg["redis"] and not seen_redis))
        seen_redis |= cfg["redis"]
    summary = table(results)
    (OUT / f"summary{args.suffix}.md").write_text(summary + "\n", encoding="utf-8")
    print("\n" + summary)


if __name__ == "__main__":
    main()
