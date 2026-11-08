#!/usr/bin/env python3
"""Check whether recorded TfL predictions can support reliability measures."""
import argparse
import json
import re
import statistics
from collections import defaultdict
from datetime import datetime, timezone

EARLY_SECONDS = 120   # Leaving the feed this long before its expected time counts as vanishing
BUNCH_SECONDS = 120   # Consecutive arrivals this close count as bunched
CONVOY_SIZE = 3       # Buses needed in a bunched run to count as a convoy
MAX_GAP_POLLS = 3     # Longer recording gaps make a disappearance unclassifiable


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


def percentile(values, fraction):
    """Return a simple nearest-rank percentile, such as 0.95 for the 95th."""
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def analyse_line(records, early=EARLY_SECONDS, bunch=BUNCH_SECONDS, convoy_size=CONVOY_SIZE):
    """Work out arrivals, vanished departures, gaps and convoys for one route."""
    polls, failed, empty = usable_polls(records)
    result = {
        "polls": len(polls), "failed_polls": failed, "empty_polls": empty,
        "predictions": 0, "vehicles": set(), "stops": set(), "arrivals": [],
        "vanished": [], "flickers": 0, "unclassified": 0, "gaps": [], "convoys": [],
    }
    if not polls:
        return result

    # A disappearance after a long recording gap can't be timed, so it's left unclassified
    times = [parse_time(p["polled_at"]) for p in polls]
    intervals = [b - a for a, b in zip(times, times[1:])]
    max_gap = MAX_GAP_POLLS * statistics.median(intervals) if intervals else 0

    # active: predictions in the latest poll
    # pending: predictions that vanished, but may still come back
    active, pending = {}, {}
    for now, poll in zip(times, polls):
        seen = set()
        vehicles_now = {a["vehicleId"] for a in poll["arrivals"]}

        # Record every prediction in this poll
        for arrival in poll["arrivals"]:
            # One key per vehicle, trip and stop, so the same bus on its next trip is new
            key = (arrival["naptanId"], arrival["direction"], arrival["vehicleId"], arrival["tripId"])
            seen.add(key)
            result["predictions"] += 1
            result["vehicles"].add(arrival["vehicleId"])
            result["stops"].add(arrival["naptanId"])
            if key in pending:  # It came back, so it never really vanished
                del pending[key]
                result["flickers"] += 1
            state = active.setdefault(key, {"count": 0})
            state.update(last_seen=now, expected=parse_time(arrival["expectedArrival"]))
            state["count"] += 1

        # Classify predictions that were in the last poll but not this one
        for key in [k for k in active if k not in seen]:
            state = active.pop(key)
            if now - state["last_seen"] > max_gap:
                result["unclassified"] += 1
            elif state["expected"] - now > early:
                # Gone well before it was due, so treat it as vanished unless it returns
                pending[key] = {
                    "stop": key[0], "direction": key[1], "vehicle": key[2],
                    "missing_at": now, "seconds_early": state["expected"] - now,
                    "observations": state["count"], "vehicle_still_in_feed": key[2] in vehicles_now,
                }
            else:
                # Gone around its due time, so assume it arrived, but no later than now
                result["arrivals"].append((key[0], key[1], min(state["expected"], now)))

    # Anything still pending at the end never came back
    result["vanished"] = list(pending.values())

    # Gaps are measured between consecutive arrivals at the same stop and direction
    by_stop = defaultdict(list)
    for stop, direction, when in result["arrivals"]:
        by_stop[(stop, direction)].append(when)
    for (stop, direction), arrivals in by_stop.items():
        arrivals.sort()
        run = 1  # Buses in the current bunched run
        for before, after in zip(arrivals, arrivals[1:]):
            gap = after - before
            result["gaps"].append(gap)
            run = run + 1 if gap <= bunch else 1
            if run == convoy_size:  # Count each run once, when it first reaches convoy size
                result["convoys"].append({"stop": stop, "direction": direction, "at": after})
    return result


def show(line, r):
    """Print a readable summary of one route's results."""
    print(f"\nRoute {line}")
    print(f"  Usable polls: {r['polls']} (failed {r['failed_polls']}, empty {r['empty_polls']})")
    print(f"  Predictions: {r['predictions']}, vehicles: {len(r['vehicles'])}, stops: {len(r['stops'])}")
    print(f"  Estimated arrivals: {len(r['arrivals'])}")
    if r["gaps"]:
        minutes = [g / 60 for g in r["gaps"]]
        print(f"  Gaps: {len(minutes)}, median {statistics.median(minutes):.1f} min, "
              f"95th percentile {percentile(minutes, 0.95):.1f} min")
    else:
        print("  Gaps: none yet")
    print(f"  Convoys of {CONVOY_SIZE}+: {len(r['convoys'])}")
    still = sum(v["vehicle_still_in_feed"] for v in r["vanished"])
    print(f"  Apparent vanished departures: {len(r['vanished'])} "
          f"({still} with the vehicle still in the feed, {len(r['vanished']) - still} with it gone)")
    print(f"  Disappeared then returned: {r['flickers']}, unclassified: {r['unclassified']}")


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
        show(line, analyse_line(records[line]))


if __name__ == "__main__":
    main()