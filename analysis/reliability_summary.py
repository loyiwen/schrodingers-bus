#!/usr/bin/env python3
"""Combine every batch's reliability results into route-level findings, counting overlapping batches once."""
import argparse
import csv
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "app"))
from reliability import analyse_line, group_convoys, group_vanished, merge_repeat_arrivals  # noqa: E402

DATA = ROOT / "trial" / "data"
FOLDERS = ("part1-60m", "part2-60m", "part1-w5-120m", "part2-w5-120m")
NAME = re.compile(r"(?P<route>[A-Za-z0-9]+)-(?P<start>\d{8}T\d{6})-(?P<minutes>\d+)m")
MAX_GAP_SECONDS = 3600  # Longer gaps are breaks in service or data, not waits a passenger would sit through
MIN_STOP_GAPS = 30  # Stops need this many gaps for their own figures
MIN_HOUR_GAPS = 10000  # Hours with fewer gaps are mostly the 24-hour routes, so not comparable
BLUE, ORANGE, INK, MUTED = "#2a78d6", "#eb6834", "#0b0b0b", "#52514e"


def owned_ranges(starts, length):
    """Return the time range each batch owns, so every moment belongs to exactly one batch.

    A moment belongs to the batch whose window centre is nearest, among the batches covering it. This keeps
    events away from window edges, where a batch can't yet tell an arrival from a vanished bus.
    """
    ranges = []
    for i, start in enumerate(starts):
        centre = start + length / 2
        low, high = start, start + length
        if i > 0 and starts[i - 1] + length > start:  # Overlaps the previous batch
            low = (starts[i - 1] + length / 2 + centre) / 2
        if i + 1 < len(starts) and start + length > starts[i + 1]:  # Overlaps the next batch
            high = (centre + starts[i + 1] + length / 2) / 2
        ranges.append((low, high))
    return ranges


def analyse_batch_file(path):
    """Analyse one batch file, returning its events and the stop names it saw."""
    match = NAME.fullmatch(path.stem)
    start = datetime.strptime(match["start"], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).timestamp()
    polls = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    result = analyse_line(polls)
    names = {a["naptanId"]: a.get("stationName") for poll in polls for a in poll["arrivals"]}
    vanished = group_vanished(result["vanished"], result["vehicle_seen"], result["last_poll"]) if result["polls"] else []
    return {"folder": path.parent.name, "route": match["route"], "start": start, "length": int(match["minutes"]) * 60,
            "arrivals": result["arrivals"], "convoys": result["convoys"], "vanished": vanished, "names": names}


def excess_wait(gaps):
    """Return the average wait for a passenger arriving at random, and how much of it comes from uneven gaps.

    With gaps h, the average wait is sum(h^2) / (2 sum(h)). Evenly spaced buses would give mean(h) / 2, so the
    difference is the extra wait caused by irregularity, like TfL's excess wait time, but measured against the
    observed average rather than the timetable, which isn't used here.
    """
    total = sum(gaps)
    average = sum(gap * gap for gap in gaps) / (2 * total)
    return average, average - total / len(gaps) / 2


def summarise(batches):
    """Keep each event only in the batch that owns its moment, then work out gaps and measures per route."""
    by_route_part = defaultdict(list)
    for batch in batches:
        by_route_part[(batch["route"], batch["folder"])].append(batch)
    arrivals, convoy_detections, vanished = defaultdict(list), defaultdict(list), defaultdict(list)
    naive = Counter()
    names = {}
    for (route, folder), group in by_route_part.items():
        group.sort(key=lambda batch: batch["start"])
        for batch, (low, high) in zip(group, owned_ranges([b["start"] for b in group], group[0]["length"])):
            names.update(batch["names"])
            naive[route] += len(batch["arrivals"])
            arrivals[(route, folder)] += [a for a in batch["arrivals"] if low <= a[2] < high]
            convoy_detections[(route, folder)] += [c for c in batch["convoys"] if low <= c["at"] < high]
            vanished[route] += [v for v in batch["vanished"] if low <= v["missing_at"] < high]

    routes, stop_gaps, hours = {}, defaultdict(list), defaultdict(list)
    for (route, folder), owned in arrivals.items():
        # Repeats can also straddle two batches' ranges, so merge again across the whole recording part
        owned, _ = merge_repeat_arrivals(owned)
        # A repeated arrival would mean ownership failed, so check rather than assume
        assert len(owned) == len({(a[0], a[1], round(a[2]), a[3]) for a in owned}), f"duplicate arrivals in {route}"
        by_stop = defaultdict(list)
        for stop, direction, when, vehicle in owned:
            by_stop[(stop, direction)].append(when)
        record = routes.setdefault(route, {"route": route, "arrivals": 0, "gaps": [], "naive_arrivals": naive[route],
                                           "convoys": [], "detections": 0, "days": defaultdict(list)})
        record["detections"] += len(convoy_detections[(route, folder)])
        record["arrivals"] += len(owned)
        record["convoys"] += group_convoys(convoy_detections[(route, folder)])
        for (stop, direction), times in by_stop.items():
            times.sort()
            pairs = [(after, after - before) for before, after in zip(times, times[1:]) if after - before <= MAX_GAP_SECONDS]
            gaps = [gap for _, gap in pairs]
            record["gaps"] += gaps
            for moment, gap in pairs:
                moment = datetime.fromtimestamp(moment, timezone.utc)
                record["days"][moment.date()].append(gap)
                hours[moment.hour].append(gap)
            stop_gaps[(route, stop, direction)] += gaps  # Pooled across both recording parts

    stops = []
    for (route, stop, direction), gaps in stop_gaps.items():
        if len(gaps) >= MIN_STOP_GAPS:
            average, excess = excess_wait(gaps)
            stops.append({"route": route, "stop": stop, "name": names.get(stop), "direction": direction,
                          "gaps": len(gaps), "median_gap_minutes": statistics.median(gaps) / 60,
                          "average_wait_minutes": average / 60, "excess_wait_minutes": excess / 60})

    rows = []
    for route, record in sorted(routes.items()):
        gaps = record["gaps"]
        average, excess = excess_wait(gaps)
        per_day = [excess_wait(day)[1] / 60 for day in record["days"].values() if len(day) >= 100]
        events = Counter(event["kind"] for event in vanished[route])
        convoys = record["convoys"]
        rows.append({
            "route": route, "arrivals": record["arrivals"], "naive_arrivals": record["naive_arrivals"],
            "gaps": len(gaps), "median_gap_minutes": statistics.median(gaps) / 60,
            "p95_gap_minutes": sorted(gaps)[int(0.95 * (len(gaps) - 1))] / 60,
            "average_wait_minutes": average / 60, "excess_wait_minutes": excess / 60,
            "excess_wait_daily_range": (min(per_day), max(per_day)) if per_day else None,
            "convoys": len(convoys), "convoy_detections": record["detections"],  # Stop by stop, before grouping
            "median_convoy_stops": statistics.median(c["stops"] for c in convoys) if convoys else 0,
            "vanished_vehicles": events["vanished"], "vanished_returned": events["returned"],
            "vanished_unconfirmed": events["unconfirmed"],
        })
    hour_rows = [{"hour": hour, "gaps": len(gaps), "median_gap_minutes": statistics.median(gaps) / 60,
                  "excess_wait_minutes": excess_wait(gaps)[1] / 60} for hour, gaps in sorted(hours.items())]
    return rows, stops, hour_rows


def write_outputs(rows, stops, hour_rows, out):
    """Write the route and stop tables, a Markdown summary and two figures."""
    def number(value):
        if isinstance(value, tuple):
            return f"{value[0]:.2f} to {value[1]:.2f}"
        return f"{value:.2f}" if isinstance(value, float) else str(value)

    for name, table in (("reliability_routes.csv", rows), ("reliability_stops.csv", stops), ("reliability_hours.csv", hour_rows)):
        with (out / name).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(table[0]))
            writer.writeheader()
            writer.writerows({key: number(value) for key, value in row.items()} for row in table)

    columns = ("route", "arrivals", "median_gap_minutes", "p95_gap_minutes", "average_wait_minutes",
               "excess_wait_minutes", "excess_wait_daily_range", "convoys", "median_convoy_stops",
               "vanished_vehicles", "vanished_returned", "vanished_unconfirmed")
    lines = ["## Routes", "", "| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    lines += ["| " + " | ".join(number(row[c]) for c in columns) + " |" for row in rows]
    worst = sorted(stops, key=lambda s: s["excess_wait_minutes"], reverse=True)[:10]
    lines += ["", f"## Stops with the most excess wait (at least {MIN_STOP_GAPS} gaps)", "",
              "| Route | Stop | Direction | Gaps | Median gap (min) | Average wait (min) | Excess wait (min) |",
              "|---|---|---|---:|---:|---:|---:|"]
    lines += [f"| {s['route']} | {s['name']} ({s['stop']}) | {s['direction']} | {s['gaps']} | {s['median_gap_minutes']:.1f} | "
              f"{s['average_wait_minutes']:.1f} | {s['excess_wait_minutes']:.1f} |" for s in worst]
    total = sum(row["arrivals"] for row in rows)
    naive = sum(row["naive_arrivals"] for row in rows)
    lines += ["", f"Arrivals counted once: {total:,}. Summing every batch would have counted {naive:,}, "
              f"{naive / total:.2f} times as many, because batches overlap by half."]
    (out / "reliability.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from analyse_runs import style
    style(plt)
    ordered = sorted(rows, key=lambda row: row["excess_wait_minutes"])
    fig, (left, right) = plt.subplots(1, 2, figsize=(6.4, 3.4), sharey=True)
    labels = [row["route"] for row in ordered]
    left.barh(labels, [row["excess_wait_minutes"] for row in ordered], color=BLUE, height=0.6)
    left.set(xlabel="Minutes", title="Excess wait from uneven gaps")
    right.barh(labels, [row["convoys"] / 80 * 24 for row in ordered], color=ORANGE, height=0.6)
    right.set(xlabel="Per 24 hours recorded", title="Convoys of 3+ buses")
    for ax in (left, right):
        ax.grid(axis="y", visible=False)
    left.set_ylabel("Route")
    fig.tight_layout()
    fig.savefig(out / "reliability_routes.png")
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6.4, 3.0))
    # Night hours have few buses, mostly the two 24-hour routes, so they're shown hollow rather than as comparable
    quiet = [row for row in hour_rows if row["gaps"] < MIN_HOUR_GAPS]
    # NaN at quiet hours breaks the line there, rather than joining across the night
    ax.plot([row["hour"] for row in hour_rows],
            [row["excess_wait_minutes"] if row["gaps"] >= MIN_HOUR_GAPS else float("nan") for row in hour_rows],
            color=BLUE, marker="o", label="All routes running")
    ax.scatter([row["hour"] for row in quiet], [row["excess_wait_minutes"] for row in quiet], facecolors="none",
               edgecolors=MUTED, label=f"Night: under {MIN_HOUR_GAPS:,} gaps, few routes")
    ax.set(xlabel="Hour of day (UK time)", ylabel="Excess wait (min)",
           title="Excess wait from uneven gaps, by hour", xticks=range(0, 24, 3), ylim=(0, None))
    ax.legend(loc="upper right")
    fig.savefig(out / "reliability_hours.png")
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batches", type=Path, default=DATA / "batches")
    parser.add_argument("--out", type=Path, default=DATA / "analysis")
    parser.add_argument("--workers", type=int, default=None, help="processes to use, by default one per core")
    args = parser.parse_args()
    paths = sorted(path for folder in FOLDERS for path in (args.batches / folder).glob("*.jsonl"))
    if not paths:
        parser.error(f"no batches found in {args.batches}")
    with Pool(args.workers) as pool:
        batches = pool.map(analyse_batch_file, paths, chunksize=4)
    args.out.mkdir(parents=True, exist_ok=True)
    rows, stops, hour_rows = summarise(batches)
    write_outputs(rows, stops, hour_rows, args.out)
    print(f"Analysed {len(paths)} batches. Wrote reliability.md, CSVs and figures to {args.out}")


if __name__ == "__main__":
    main()
