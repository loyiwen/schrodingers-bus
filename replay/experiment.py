#!/usr/bin/env python3
"""Run the full experiment plan, one run at a time, in a fixed random order that can be resumed."""
import argparse
import json
import os
import random
import time
from pathlib import Path

from collect import RUNS, collect_run, version_warning
from send import choose_batches, send_run, utc_now

BATCHES = Path(__file__).parent.parent / "trial" / "data" / "batches"
REPEATS = 3
SEED = 1  # Sets both the run order and which batches each run sends
COOLDOWN_SECONDS = 300  # Idle time between runs, so each starts from a similar scaled-in state
WAIT_SECONDS = 900  # Longest wait for results, which covers the 10-minute recovery seen in the pilot
RATE_SECONDS = 120  # How long each steady-rate run sends for

# Nine frequent routes in 60-minute batches, plus W5 in 120-minute batches, as split from both recording parts
ALL_ROUTES = ("part1-60m", "part2-60m", "part1-w5-120m", "part2-w5-120m")
BURST_SIZES = (10, 50, 100, 250, 500, 1000)
RATES = (1, 2, 5, 10)  # Batches per second
LENGTH_MINUTES = (30, 60, 120)  # Batch lengths for the nine frequent routes, each overlapping by half
# Only recording part 2 is split at every length, as every length from both parts wouldn't fit on the VM's disk
LENGTH_COUNT = 250


def plan(repeats=REPEATS, seed=SEED):
    """Return every run in the plan, shuffled so that no experiment bunches up at one time of day."""
    runs = []
    for repeat in range(1, repeats + 1):
        for size in BURST_SIZES:
            runs.append({"run": f"burst_{size}_r{repeat}", "experiment": "burst", "level": size,
                         "repeat": repeat, "folders": ALL_ROUTES, "count": size, "rate": None})
        for rate in RATES:
            runs.append({"run": f"rate_{rate}_r{repeat}", "experiment": "rate", "level": rate,
                         "repeat": repeat, "folders": ALL_ROUTES, "count": rate * RATE_SECONDS, "rate": rate})
        for minutes in LENGTH_MINUTES:
            runs.append({"run": f"length_{minutes}_r{repeat}", "experiment": "length", "level": minutes,
                         "repeat": repeat, "folders": (f"part2-{minutes}m",),
                         "count": LENGTH_COUNT, "rate": None})
    random.Random(seed).shuffle(runs)
    return runs


def run_plan(runs, url, key, container_url, batches, out, workers, cooldown, wait):
    """Send and collect each run not yet finished, recording its settings and summaries in experiments.jsonl."""
    for number, item in enumerate(runs, 1):
        name = item["run"]
        # A saved summary means the run finished, so a restarted plan carries on from the next one
        if (out / f"{name}-summary.json").exists():
            print(f"[{number}/{len(runs)}] {name}: already done", flush=True)
            continue
        started = utc_now()
        sent = None
        # A send log without a summary means the plan stopped while collecting, so only collect again
        if not (out / f"{name}.jsonl").exists():
            print(f"[{number}/{len(runs)}] {name}: sending {item['count']} batches", flush=True)
            paths = choose_batches([batches / folder for folder in item["folders"]], item["count"], SEED)
            sent = send_run(url, key, name, paths, item["rate"], workers, out)
        summary = collect_run(container_url, out, name, wait)
        record = {**item, "folders": list(item["folders"]), "started_at": started, "finished_at": utc_now(),
                  "send": sent, "summary": summary}
        with (out / "experiments.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(record) + "\n")
        print(f"[{number}/{len(runs)}] {name}: {summary['results']}/{summary['sent']} results, "
              f"{summary['missing']} missing, {summary['hosts']} hosts, makespan {summary.get('makespan_seconds')} s",
              flush=True)
        if warning := version_warning(summary):
            print(warning, flush=True)
        if number < len(runs):
            time.sleep(cooldown)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="the Function App's address")
    parser.add_argument("--only", nargs="+", choices=("burst", "rate", "length"), help="run just these experiments")
    parser.add_argument("--repeats", type=int, default=REPEATS)
    parser.add_argument("--cooldown", type=int, default=COOLDOWN_SECONDS, help="idle seconds between runs")
    parser.add_argument("--wait", type=int, default=WAIT_SECONDS, help="longest wait for a run's results")
    parser.add_argument("--workers", type=int, default=32, help="requests in flight at once")
    parser.add_argument("--batches", type=Path, default=BATCHES)
    parser.add_argument("--out", type=Path, default=RUNS)
    parser.add_argument("--dry-run", action="store_true", help="list the runs without sending anything")
    args = parser.parse_args()

    runs = [item for item in plan(args.repeats) if not args.only or item["experiment"] in args.only]
    # Checked up front, so a missing split doesn't stop the plan hours in
    missing = sorted({folder for item in runs for folder in item["folders"] if not (args.batches / folder).is_dir()})
    if missing:
        parser.error(f"missing batch folders in {args.batches}: {', '.join(missing)}")

    if args.dry_run:
        for number, item in enumerate(runs, 1):
            print(f"{number:2} {item['run']:16} {item['count']:5} batches", "burst" if item["rate"] is None else f"at {item['rate']}/s")
        sending = sum(item["count"] / item["rate"] for item in runs if item["rate"])
        hours = (sending + len(runs) * (args.cooldown + 120)) / 3600  # Allows about 2 minutes per run to send and collect
        print(f"{len(runs)} runs, {sum(item['count'] for item in runs)} batches, roughly {hours:.1f} hours")
        return

    # Both come from the environment, so they stay out of the code and shell history
    key, container_url = os.environ.get("FUNCTION_KEY"), os.environ.get("RESULTS_SAS_URL")
    if not key or not container_url:
        parser.error("set FUNCTION_KEY and RESULTS_SAS_URL first")
    run_plan(runs, args.url, key, container_url, args.batches, args.out, args.workers, args.cooldown, args.wait)
    print("All runs done")


if __name__ == "__main__":
    main()
