#!/usr/bin/env python3
"""Reliability measures for one route's batch of TfL predictions.

Results are apparent vanished departures and estimated arrivals, because the feed doesn't confirm either.
"""
import random
import re
import statistics
from collections import defaultdict
from datetime import datetime, timezone

EARLY_SECONDS = 120   # Leaving the feed this long before its expected time counts as vanishing
BUNCH_SECONDS = 120   # Consecutive arrivals this close count as bunched
CONVOY_SIZE = 3       # Buses needed in a bunched run to count as a convoy
MAX_GAP_POLLS = 3     # Longer recording gaps make a disappearance unclassifiable
BOOTSTRAP_RESAMPLES = 2000  # More resamples give steadier intervals, but take longer
MIN_BOOTSTRAP_GAPS = 5      # Fewer gaps than this can't give a meaningful interval
CONVOY_LINK_SECONDS = 1800  # The same buses bunched again within this time are the same convoy, further along
CONVOY_SHARED_BUSES = 2     # Detections sharing this many buses count as the same convoy, as members can change
VANISH_WAIT_SECONDS = 600   # A vehicle must stay out of the feed this long to count as vanished
REARRIVAL_SECONDS = 600     # The same bus "arriving" at the same stop again this soon is one arrival


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
        "vanished": [], "flickers": 0, "unclassified": 0, "gaps": [], "convoys": [], "gaps_by_stop": {},
        "vehicle_seen": defaultdict(list), "last_poll": None,
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
        # When each vehicle was in the feed, so a vanished one can be checked for coming back
        for vehicle in vehicles_now:
            result["vehicle_seen"][vehicle].append(now)

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
                    "stop": key[0], "direction": key[1], "vehicle": key[2], "trip": key[3],
                    "missing_at": now, "seconds_early": state["expected"] - now,
                    "observations": state["count"], "vehicle_still_in_feed": key[2] in vehicles_now,
                }
            else:
                # Gone around its due time, so assume it arrived, but no later than now
                result["arrivals"].append((key[0], key[1], min(state["expected"], now), key[2]))

    # Anything still pending at the end never came back
    result["vanished"] = list(pending.values())
    result["last_poll"] = times[-1]

    # A prediction can leave the feed at its due time, come back, and leave again, which would count twice
    result["arrivals"], result["repeat_arrivals"] = merge_repeat_arrivals(result["arrivals"])

    # Gaps are measured between consecutive arrivals at the same stop and direction
    by_stop = defaultdict(list)
    for stop, direction, when, vehicle in result["arrivals"]:
        by_stop[(stop, direction)].append((when, vehicle))
    for (stop, direction), arrivals in by_stop.items():
        arrivals.sort()
        run = [arrivals[0][1]] if arrivals else []  # Vehicles in the current bunched run
        for (before, _), (after, vehicle) in zip(arrivals, arrivals[1:]):
            gap = after - before
            result["gaps"].append(gap)
            result["gaps_by_stop"].setdefault((stop, direction), []).append(gap)
            run = run + [vehicle] if gap <= bunch else [vehicle]
            if len(run) == convoy_size:  # Count each run once, when it first reaches convoy size
                result["convoys"].append({"stop": stop, "direction": direction, "at": after, "vehicles": sorted(run)})
    return result


def merge_repeat_arrivals(arrivals, window=REARRIVAL_SECONDS):
    """Merge arrivals of the same bus at the same stop that are closer than window, and return them with a count.

    The last one is kept, as an earlier disappearance was the prediction leaving the feed too soon.
    """
    by_bus = defaultdict(list)
    for stop, direction, when, vehicle in arrivals:
        by_bus[(stop, direction, vehicle)].append(when)
    merged, repeats = [], 0
    for (stop, direction, vehicle), times in by_bus.items():
        times.sort()
        for i, when in enumerate(times):
            if i + 1 < len(times) and times[i + 1] - when < window:
                repeats += 1  # A later arrival of the same bus follows soon after, so this one is dropped
                continue
            merged.append((stop, direction, when, vehicle))
    return sorted(merged, key=lambda arrival: arrival[2]), repeats


def group_convoys(detections, link=CONVOY_LINK_SECONDS, shared=CONVOY_SHARED_BUSES):
    """Merge stop-level convoy detections into convoys, as one bunch of buses is detected at stop after stop."""
    convoys = []
    for detection in sorted(detections, key=lambda d: d["at"]):
        buses = set(detection["vehicles"])
        # The most recent convoy in this direction with enough of the same buses, seen recently enough
        match = next((convoy for convoy in reversed(convoys)
                      if convoy["direction"] == detection["direction"]
                      and detection["at"] - convoy["last"] <= link
                      and len(buses & convoy["vehicles"]) >= shared), None)
        if match:
            match["last"] = detection["at"]
            match["vehicles"] |= buses
            match["stops"].add(detection["stop"])
        else:
            convoys.append({"direction": detection["direction"], "first": detection["at"], "last": detection["at"],
                            "vehicles": buses, "stops": {detection["stop"]}})
    return [{**convoy, "vehicles": sorted(convoy["vehicles"]), "stops": len(convoy["stops"])} for convoy in convoys]


def group_vanished(vanished, vehicle_seen, last_poll, wait=VANISH_WAIT_SECONDS):
    """Turn per-stop vanished predictions into one event per vehicle and trip, classified after a waiting period.

    Kinds: "vanished" if the vehicle stayed out of the feed for the whole wait, "returned" if it was in the feed at
    any point during the wait (its trip's predictions went, but the bus carried on or came back), and "unconfirmed"
    if the batch ended too soon to tell.
    """
    events = {}
    for row in vanished:
        event = events.setdefault((row["vehicle"], row["trip"]), {
            "vehicle": row["vehicle"], "trip": row["trip"], "direction": row["direction"],
            "missing_at": row["missing_at"], "stops": 0, "seconds_early": 0})
        event["missing_at"] = min(event["missing_at"], row["missing_at"])
        event["stops"] += 1
        event["seconds_early"] = max(event["seconds_early"], row["seconds_early"])
    for event in events.values():
        start = event["missing_at"]
        if any(start <= moment <= start + wait for moment in vehicle_seen.get(event["vehicle"], ())):
            event["kind"] = "returned"
        elif last_poll - start < wait:
            event["kind"] = "unconfirmed"
        else:
            event["kind"] = "vanished"
    return sorted(events.values(), key=lambda e: e["missing_at"])


def bootstrap_median_interval(values, resamples, rng):
    """Return a 95% bootstrap confidence interval for the median of values."""
    # Resample with replacement many times, to see how much the median could vary
    medians = [statistics.median(rng.choices(values, k=len(values))) for _ in range(resamples)]
    return percentile(medians, 0.025), percentile(medians, 0.975)


def stop_intervals(gaps_by_stop, resamples, seed):
    """Return the median gap and its confidence interval for each stop with enough gaps."""
    rng = random.Random(seed)  # Seeded, so reprocessing a batch gives identical results
    summaries = []
    for (stop, direction), gaps in sorted(gaps_by_stop.items()):  # Sorted, so the seed is applied in a fixed order
        if len(gaps) < MIN_BOOTSTRAP_GAPS:
            continue
        minutes = [gap / 60 for gap in gaps]
        low, high = bootstrap_median_interval(minutes, resamples, rng)
        summaries.append({
            "stop": stop, "direction": direction, "gaps": len(minutes),
            "median_gap_minutes": statistics.median(minutes),
            "interval_low_minutes": low, "interval_high_minutes": high,
        })
    return summaries


def report(polls, resamples=BOOTSTRAP_RESAMPLES, seed="batch"):
    """Return a JSON-safe reliability summary for one route's batch."""
    r = analyse_line(polls)
    minutes = [gap / 60 for gap in r["gaps"]]
    events = group_vanished(r["vanished"], r["vehicle_seen"], r["last_poll"]) if r["polls"] else []
    kinds = {kind: sum(event["kind"] == kind for event in events) for kind in ("vanished", "returned", "unconfirmed")}
    return {
        "polls": r["polls"],
        "failed_polls": r["failed_polls"],
        "empty_polls": r["empty_polls"],
        "predictions": r["predictions"],
        "vehicles": len(r["vehicles"]),
        "stops": len(r["stops"]),
        "estimated_arrivals": len(r["arrivals"]),
        "repeat_arrivals": r.get("repeat_arrivals", 0),  # Merged, as the prediction left the feed more than once
        "gaps": len(minutes),
        # None rather than 0 when there's nothing to measure, as 0 would mean no wait at all
        "median_gap_minutes": statistics.median(minutes) if minutes else None,
        "p95_gap_minutes": percentile(minutes, 0.95) if minutes else None,
        "convoys": len(r["convoys"]),  # Detections at single stops, so one bunch counts at each stop it passes
        "convoy_groups": len(group_convoys(r["convoys"])),  # The same detections, merged along the route
        "apparent_vanished": len(r["vanished"]),  # Predictions at single stops
        "vanished_vehicles": kinds["vanished"],  # Vehicles that left the feed early and stayed out for the wait
        "vanished_returned": kinds["returned"],  # A trip's predictions went, but the vehicle was back in the feed within the wait
        "vanished_unconfirmed": kinds["unconfirmed"],  # Too close to the batch's end to tell
        "flickers": r["flickers"],
        "unclassified": r["unclassified"],
        "stop_gaps": stop_intervals(r["gaps_by_stop"], resamples, seed),
    }