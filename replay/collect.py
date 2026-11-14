#!/usr/bin/env python3
"""Download a run's results from blob storage, and line them up with what was sent."""
import argparse
import csv
import json
import math
import os
import statistics
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

RUNS = Path(__file__).parent.parent / "trial" / "data" / "runs"
ACCEPTED = 202
CHECK_EVERY_SECONDS = 15  # How often --wait checks for new results
# Columns of the per-batch table, in order
COLUMNS = (
    "batch_id", "status", "bytes", "predictions", "sent_at", "send_seconds", "queued_at", "started_at",
    "finished_at", "written_at", "queue_delay_seconds", "runtime_seconds", "end_to_end_seconds",
    "instance_id", "instance_run", "dequeue_count",
)


def parse_time(text):
    """Turn a millisecond UTC timestamp, such as 2026-11-13T07:56:01.390Z, into a datetime."""
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def blob_url(container_url, name="", **params):
    """Add a blob name and extra query parameters to a container's SAS URL."""
    base, _, sas = container_url.partition("?")
    query = "&".join(part for part in (sas, urllib.parse.urlencode(params)) if part)
    return f"{base}/{urllib.parse.quote(name)}?{query}" if name else f"{base}?{query}"


def get(url):
    """Return the body of a GET request."""
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def list_blobs(container_url, prefix):
    """Return the name and last-modified time of every blob starting with prefix."""
    blobs, marker = {}, ""
    while True:
        # Azure returns at most 5,000 names at a time, and a marker to carry on from
        url = blob_url(container_url, restype="container", comp="list", prefix=prefix, marker=marker)
        root = ElementTree.fromstring(get(url))
        for blob in root.iter("Blob"):
            modified = parsedate_to_datetime(blob.findtext("Properties/Last-Modified"))
            blobs[blob.findtext("Name")] = modified.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        marker = root.findtext("NextMarker") or ""
        if not marker:
            return blobs


def download(container_url, run, folder, workers=16):
    """Save every result for a run into folder, skipping ones already there, and return their write times."""
    folder.mkdir(parents=True, exist_ok=True)
    blobs = list_blobs(container_url, f"{run}-")
    missing = [name for name in blobs if not (folder / name).exists()]

    def fetch(name):
        (folder / name).write_bytes(get(blob_url(container_url, name)))

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(fetch, missing))
    # Kept, as the blob's write time is when the result was really saved
    (folder / "written_at.json").write_text(json.dumps(blobs, indent=1), encoding="utf-8")
    return blobs


def percentile(values, fraction):
    """Return the nearest-rank percentile of values, or None if there are none."""
    values = sorted(values)
    return values[math.ceil(fraction * len(values)) - 1] if values else None


def combine(log_path, folder):
    """Return one row per batch sent, joining the send log with its result, if any."""
    written = json.loads((folder / "written_at.json").read_text(encoding="utf-8"))
    rows = []
    for text in log_path.read_text(encoding="utf-8").splitlines():
        sent = json.loads(text)
        row = dict.fromkeys(COLUMNS)
        row.update(batch_id=sent["batch_id"], status=sent["status"], bytes=sent["bytes"],
                   sent_at=sent["sent_at"], send_seconds=sent["seconds"])
        name = f"{sent['batch_id']}.json"
        if (folder / name).exists():
            result = json.loads((folder / name).read_text(encoding="utf-8"))
            row.update({column: result.get(column) for column in COLUMNS if column in result})
            row["written_at"] = written.get(name)
            # Results from before the wall-clock times were added have no finished_at
            if result.get("finished_at"):
                row["end_to_end_seconds"] = round(
                    (parse_time(result["finished_at"]) - parse_time(sent["sent_at"])).total_seconds(), 3)
        rows.append(row)
    return rows


def summarise(rows):
    """Return the headline figures for a run."""
    done = [row for row in rows if row["runtime_seconds"] is not None]
    timed = [row for row in done if row["end_to_end_seconds"] is not None]
    summary = {
        "sent": len(rows),
        "accepted": sum(row["status"] == ACCEPTED for row in rows),
        "results": len(done),
        # Accepted but never analysed, so check the poison queue
        "missing": sum(row["status"] == ACCEPTED and row["runtime_seconds"] is None for row in rows),
        "instances": len({row["instance_id"] for row in done}),
        "cold_starts": sum(row["instance_run"] == 1 for row in done),
        "retried": sum((row["dequeue_count"] or 1) > 1 for row in done),
    }
    for column in ("send_seconds", "queue_delay_seconds", "runtime_seconds", "end_to_end_seconds"):
        values = [row[column] for row in (timed if column == "end_to_end_seconds" else done)]
        summary[f"median_{column}"] = round(statistics.median(values), 3) if values else None
        summary[f"p95_{column}"] = round(percentile(values, 0.95), 3) if values else None
    if timed:
        # From the first batch sent to the last result, which covers the whole pipeline
        first = min(parse_time(row["sent_at"]) for row in rows)
        last = max(parse_time(row["finished_at"]) for row in timed)
        summary["makespan_seconds"] = round((last - first).total_seconds(), 3)
        summary["results_per_second"] = round(len(timed) / summary["makespan_seconds"], 3)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", help="the run name given to send.py")
    parser.add_argument("--runs", type=Path, default=RUNS, help="folder holding the send logs")
    parser.add_argument("--offline", action="store_true", help="use results already downloaded")
    parser.add_argument("--wait", type=int, default=0, help="seconds to keep checking until nothing is missing")
    args = parser.parse_args()

    log_path = args.runs / f"{args.run}.jsonl"
    folder = args.runs / args.run
    if not log_path.exists():
        parser.error(f"no send log at {log_path}")
    # A read-only SAS link to the results container, kept out of the code and shell history
    container_url = os.environ.get("RESULTS_SAS_URL")
    if not args.offline and not container_url:
        parser.error("set RESULTS_SAS_URL first")

    deadline = time.monotonic() + args.wait
    while True:
        if not args.offline:
            try:
                download(container_url, args.run, folder)
            except Exception as error:  # Only the type, as the message could include the SAS link
                parser.error(f"download failed: {type(error).__name__}")
        rows = combine(log_path, folder)
        missing = summarise(rows)["missing"]
        # Stops once every accepted batch has a result, or when the wait runs out
        if args.offline or not missing or time.monotonic() >= deadline:
            break
        print(f"{missing} results still missing, checking again in {CHECK_EVERY_SECONDS} s", flush=True)
        time.sleep(CHECK_EVERY_SECONDS)

    with (args.runs / f"{args.run}.csv").open("w", newline="", encoding="utf-8") as table:
        writer = csv.DictWriter(table, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    summary = summarise(rows)
    (args.runs / f"{args.run}-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"Saved {args.run}.csv and {args.run}-summary.json in {args.runs}")


if __name__ == "__main__":
    main()
