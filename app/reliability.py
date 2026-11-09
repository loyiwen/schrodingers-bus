#!/usr/bin/env python3
"""Reliability measures for one route's batch of TfL predictions.

Results are apparent vanished departures and estimated arrivals, because the feed doesn't confirm either.
"""
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


def report(polls):
    """Return a JSON-safe reliability summary for one route's batch."""
    r = analyse_line(polls)
    minutes = [gap / 60 for gap in r["gaps"]]
    return {
        "polls": r["polls"],
        "failed_polls": r["failed_polls"],
        "empty_polls": r["empty_polls"],
        "predictions": r["predictions"],
        "vehicles": len(r["vehicles"]),
        "stops": len(r["stops"]),
        "estimated_arrivals": len(r["arrivals"]),
        "gaps": len(minutes),
        # None rather than 0 when there's nothing to measure, as 0 would mean no wait at all
        "median_gap_minutes": statistics.median(minutes) if minutes else None,
        "p95_gap_minutes": percentile(minutes, 0.95) if minutes else None,
        "convoys": len(r["convoys"]),
        "apparent_vanished": len(r["vanished"]),
        "flickers": r["flickers"],
        "unclassified": r["unclassified"],
    }