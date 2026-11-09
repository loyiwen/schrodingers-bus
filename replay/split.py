#!/usr/bin/env python3
"""Split a recording into overlapping batches, one route and time window per file."""
import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

# A window still counts as complete if the recording stopped just before it closed
POLL_SLACK_SECONDS = 60


def parse_time(text):
    """Turn one of the recorder's UTC timestamps into a datetime."""
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def window_numbers(seconds, length, step):
    """Return the numbers of the windows holding a poll this many seconds after the start."""
    # Window n covers n * step up to, but not including, n * step + length
    first = max(0, (seconds - length) // step + 1)
    return range(first, seconds // step + 1)


def batch_name(line, start, minutes):
    """Name a batch by route, start time and length, so different lengths never collide."""
    return f"{line}-{start:%Y%m%dT%H%M%S}-{minutes}m"


def split(recording, out_dir, minutes, step_minutes, lines=None):
    """Write overlapping batches to out_dir, and return their paths and the bad line count."""
    # Batches are built by appending, so leftovers would end up with repeated polls
    if out_dir.exists() and any(out_dir.iterdir()):
        raise ValueError(f"{out_dir} already contains files")
    out_dir.mkdir(parents=True, exist_ok=True)

    length, step = minutes * 60, step_minutes * 60
    first_poll = last_poll = None
    batches = {}  # Maps each batch's path to its window number
    bad_lines = 0

    # Read one line at a time, as a full recording is too big to hold in memory
    with recording.open(encoding="utf-8") as source:
        for text in source:
            try:
                poll = json.loads(text)
            except json.JSONDecodeError:  # A copy taken mid-recording can end part-way through a line
                bad_lines += 1
                continue
            polled_at = parse_time(poll["polled_at"])
            first_poll = first_poll or polled_at
            last_poll = polled_at
            if lines and poll["line"] not in lines:
                continue
            seconds = int((polled_at - first_poll).total_seconds())
            for number in window_numbers(seconds, length, step):
                start = first_poll + timedelta(seconds=number * step)
                path = out_dir / f"{batch_name(poll['line'], start, minutes)}.jsonl"
                batches[path] = number
                # Opening per write stops a long recording running out of file handles
                with path.open("a", encoding="utf-8") as batch:
                    batch.write(text)

    # Cut-off windows would be shorter than the rest, and skew runtime comparisons
    kept = []
    for path, number in batches.items():
        end = first_poll + timedelta(seconds=number * step + length)
        if last_poll + timedelta(seconds=POLL_SLACK_SECONDS) >= end:
            kept.append(path)
        else:
            path.unlink()
    return sorted(kept), bad_lines


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("out", type=Path, help="empty folder for the batches")
    parser.add_argument("--minutes", type=int, default=60, help="length of each batch")
    parser.add_argument("--step", type=int, default=30, help="minutes between batch starts")
    parser.add_argument("--lines", help="comma-separated routes, or every route if left out")
    args = parser.parse_args()
    # A step longer than the batch would leave periods no batch covers
    if not 0 < args.step <= args.minutes:
        parser.error("--step must be above 0 and no more than --minutes")

    lines = set(args.lines.split(",")) if args.lines else None
    paths, bad_lines = split(args.recording, args.out, args.minutes, args.step, lines)
    size = sum(path.stat().st_size for path in paths) / 1_000_000
    print(f"{len(paths)} batches, {size:.1f} MB in {args.out}")
    if bad_lines:
        print(f"Skipped {bad_lines} unreadable lines")


if __name__ == "__main__":
    main()