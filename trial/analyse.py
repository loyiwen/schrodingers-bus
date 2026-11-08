#!/usr/bin/env python3
"""Check whether recorded TfL predictions can support reliability measures."""
import argparse
import json
import re
from collections import defaultdict
from datetime import datetime, timezone

EARLY_SECONDS = 120   # Leaving the feed this long before its expected time counts as vanishing


def parse_time(text):
    """Convert a TfL UTC timestamp to seconds since the epoch."""
    # TfL can send 7 fractional digits, which fromisoformat may reject, so drop the fraction
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?Z?", text)
    if not match:
        raise ValueError(f"unexpected time: {text!r}")
    return datetime.fromisoformat(match.group(1)).replace(tzinfo=timezone.utc).timestamp()


def usable_polls(records):
    """Return usable polls in time order, plus the failed and empty counts.

    An empty poll can't show that a bus vanished, as the whole feed may be down.
    """
    usable, failed, empty = [], 0, 0
    for record in sorted(records, key=lambda r: r["polled_at"]):
        if not record["ok"]:
            failed += 1
        elif not record["arrivals"]:
            empty += 1
        else:
            usable.append(record)
    return usable, failed, empty


def analyse_line(records, early=EARLY_SECONDS):
    """Classify each prediction that leaves the feed as an arrival or a vanished departure."""
    polls, failed, empty = usable_polls(records)
    result = {"polls": len(polls), "failed_polls": failed, "empty_polls": empty, "arrivals": [], "vanished": []}

    active = {}  # Predictions in the latest poll
    for poll in polls:
        now = parse_time(poll["polled_at"])
        seen = set()
        for arrival in poll["arrivals"]:
            # One key per vehicle, trip and stop, so the same bus on its next trip is new
            key = (arrival["naptanId"], arrival["direction"], arrival["vehicleId"], arrival["tripId"])
            seen.add(key)
            active[key] = parse_time(arrival["expectedArrival"])

        # Classify predictions that were in the last poll but not this one
        for key in [k for k in active if k not in seen]:
            expected = active.pop(key)
            if expected - now > early:
                result["vanished"].append({"stop": key[0], "vehicle": key[2], "seconds_early": expected - now})
            else:
                # Gone around its due time, so assume it arrived, but no later than now
                result["arrivals"].append((key[0], key[1], min(expected, now)))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+")
    args = parser.parse_args()

    # Group polls by route, as each route is analysed separately
    records = defaultdict(list)
    for name in args.files:
        with open(name, encoding="utf-8") as source:
            for line in source:
                record = json.loads(line)
                records[record["line"]].append(record)

    for line in sorted(records):
        r = analyse_line(records[line])
        print(f"Route {line}: {r['polls']} usable polls (failed {r['failed_polls']}, empty {r['empty_polls']}), "
              f"{len(r['arrivals'])} estimated arrivals, {len(r['vanished'])} left early")


if __name__ == "__main__":
    main()