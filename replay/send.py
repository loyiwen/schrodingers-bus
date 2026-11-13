#!/usr/bin/env python3
"""Send recorded batches to the HTTP function, as a burst or at a steady rate."""
import argparse
import json
import math
import os
import random
import re
import statistics
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

REQUEST_TIMEOUT_SECONDS = 120
ACCEPTED = 202  # What Function 1 returns once a batch is saved and queued


def utc_now():
    """Return the current UTC time as an ISO 8601 string, to the millisecond."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def choose_batches(folders, count, seed):
    """Return batch files in an order that only depends on the seed."""
    # Sort first, as folders list their files in no fixed order
    paths = sorted(path for folder in folders for path in folder.glob("*.jsonl"))
    if not paths:
        raise ValueError("no batches found")
    # Shuffled so even a small burst mixes routes and times of day
    random.Random(seed).shuffle(paths)
    # Asking for more than exist reuses files, and a smaller run is the start of a bigger one
    return [paths[number % len(paths)] for number in range(count or len(paths))]


def send(base_url, key, batch_id, path, due):
    """Send one batch once it's due, and return a record of what happened."""
    wait = due - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    result = {
        "batch_id": batch_id,
        "sent_at": utc_now(),
        "late_seconds": round(max(0, time.monotonic() - due), 4),  # Above 0 means this tool fell behind
        "status": None,
        "error": None,
    }
    # Read here rather than up front, as a large burst wouldn't fit in memory
    body = path.read_bytes()
    result["bytes"] = len(body)
    request = urllib.request.Request(f"{base_url}/api/batches/{batch_id}", data=body, method="PUT")
    request.add_header("Content-Type", "application/x-ndjson")
    if key:  # In a header, so the key can't end up in a logged URL
        request.add_header("x-functions-key", key)

    clock = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            response.read()
            result["status"] = response.status
    except urllib.error.HTTPError as error:  # A refused batch is a result, so keep going
        error.close()
        result["status"] = error.code
    except Exception as error:  # Same for timeouts and dropped connections
        result["error"] = type(error).__name__
    result["seconds"] = round(time.perf_counter() - clock, 4)
    return result


def replay(base_url, key, run_id, paths, rate, workers):
    """Send every batch, and return the records and the total time taken."""
    clock = time.perf_counter()
    start = time.monotonic()
    with ThreadPoolExecutor(workers) as pool:
        # No rate means a burst, where everything is due straight away
        # The number keeps IDs unique, and the run ID keeps repeats from overwriting each other
        futures = [
            pool.submit(send, base_url, key, f"{run_id}-{number:05d}-{path.stem}", path, start + (number / rate if rate else 0))
            for number, path in enumerate(paths)
        ]
        results = [future.result() for future in futures]
    return results, time.perf_counter() - clock


def summarise(results, elapsed):
    """Return the headline figures for a run."""
    accepted = [result for result in results if result["status"] == ACCEPTED]
    times = sorted(result["seconds"] for result in accepted)
    megabytes = sum(result["bytes"] for result in accepted) / 1_000_000
    return {
        "sent": len(results),
        "accepted": len(accepted),
        "failed": len(results) - len(accepted),
        "elapsed_seconds": round(elapsed, 2),
        "batches_per_second": round(len(accepted) / elapsed, 2) if elapsed else 0,
        "megabytes_per_second": round(megabytes / elapsed, 1) if elapsed else 0,
        "median_seconds": round(statistics.median(times), 3) if times else None,
        "p95_seconds": times[math.ceil(0.95 * len(times)) - 1] if times else None,
        "max_late_seconds": max(result["late_seconds"] for result in results),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batches", type=Path, nargs="+", help="folders of batch files")
    parser.add_argument("--url", required=True, help="such as http://localhost:7071")
    parser.add_argument("--run", required=True, help="unique name for this run")
    parser.add_argument("--count", type=int, help="batches to send, or all of them if left out")
    parser.add_argument("--rate", type=float, help="batches per second, or a burst if left out")
    parser.add_argument("--workers", type=int, default=32, help="requests in flight at once")
    parser.add_argument("--seed", type=int, default=1, help="sets the order of the batches")
    parser.add_argument("--out", type=Path, default=Path(__file__).parent.parent / "trial" / "data" / "runs")
    args = parser.parse_args()
    # No hyphens, so the run can be split back off the front of a batch ID
    if not re.fullmatch(r"[A-Za-z0-9_]{1,40}", args.run):
        parser.error("--run can only use letters, digits and underscores")

    # Stops a repeated run name from overwriting earlier blobs and results
    log_path = args.out / f"{args.run}.jsonl"
    if log_path.exists():
        parser.error(f"{log_path} already exists, so choose a new run name")
    paths = choose_batches(args.batches, args.count, args.seed)
    args.out.mkdir(parents=True, exist_ok=True)

    # The key comes from the environment, so it stays out of the code and shell history
    results, elapsed = replay(args.url.rstrip("/"), os.environ.get("FUNCTION_KEY"), args.run, paths, args.rate, args.workers)
    summary = summarise(results, elapsed)
    with log_path.open("w", encoding="utf-8") as log:
        for result in results:
            log.write(json.dumps(result) + "\n")
    print(json.dumps(summary, indent=2))
    print(f"Saved {log_path}")


if __name__ == "__main__":
    main()