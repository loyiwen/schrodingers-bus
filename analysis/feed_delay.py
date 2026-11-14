#!/usr/bin/env python3
"""Measure feed delay: how old TfL's predictions already were when the recorder polled them."""
import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from functools import cache
from pathlib import Path

DATA = Path(__file__).parent.parent / "trial" / "data"
RECORDINGS = ("tfl-20261109T060532Z.jsonl", "tfl-20261110T142957Z.jsonl")
MAX_SECONDS = 3600  # Ages above this go in the last bin, as they're almost certainly stale feed entries
BLUE, INK, MUTED = "#2a78d6", "#0b0b0b", "#52514e"


@cache
def seconds_since_epoch(text):
    """Turn a TfL or recorder timestamp into seconds, cached because each poll repeats a few values many times."""
    # TfL gives up to 7 fractional digits, more than datetime accepts, and whole seconds are enough here
    return datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def percentile_from_counts(counts, fraction):
    """Return the nearest-rank percentile from a {value: count} histogram."""
    target = fraction * sum(counts.values())
    running = 0
    for value in sorted(counts):
        running += counts[value]
        if running >= target:
            return value
    return None


def measure(paths):
    """Return 1-second histograms of prediction age, overall and per route, plus counts of odd values."""
    overall, by_route = Counter(), defaultdict(Counter)
    future = 0  # Predictions stamped after the poll, which a clock difference could cause
    for path in paths:
        with path.open(encoding="utf-8") as source:
            for text in source:
                try:
                    poll = json.loads(text)
                except json.JSONDecodeError:
                    continue
                polled = seconds_since_epoch(poll["polled_at"])
                for arrival in poll["arrivals"]:
                    if not arrival.get("timestamp"):
                        continue
                    age = int(polled - seconds_since_epoch(arrival["timestamp"]))
                    if age < 0:
                        future += 1
                        age = 0
                    age = min(age, MAX_SECONDS)
                    overall[age] += 1
                    by_route[poll["line"]][age] += 1
            seconds_since_epoch.cache_clear()  # Keeps memory flat across a long recording
    return overall, by_route, future


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recordings", type=Path, nargs="*", default=[DATA / name for name in RECORDINGS])
    parser.add_argument("--out", type=Path, default=DATA / "analysis")
    args = parser.parse_args()
    overall, by_route, future = measure(args.recordings)

    rows = [("All routes", overall)] + sorted(by_route.items())
    lines = ["| Route | Predictions | Median age (s) | 95th percentile (s) | Over 5 min |", "|---|---:|---:|---:|---:|"]
    for name, counts in rows:
        total = sum(counts.values())
        stale = sum(count for age, count in counts.items() if age > 300)
        lines.append(f"| {name} | {total:,} | {percentile_from_counts(counts, 0.5)} | "
                     f"{percentile_from_counts(counts, 0.95)} | {100 * stale / total:.2f}% |")
    lines.append(f"\nPredictions stamped after the poll, counted as age 0: {future:,}")
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "feed_delay.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (args.out / "feed_delay_histogram.json").write_text(json.dumps({"overall": overall, "by_route": by_route}), encoding="utf-8")
    print("\n".join(lines))

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from analyse_runs import style
    style(plt)
    # Cumulative share of predictions by age, which reads off the median and tail directly
    ages = sorted(age for age in overall if age <= 300)
    total, running, shares = sum(overall.values()), 0, []
    for age in ages:
        running += overall[age]
        shares.append(100 * running / total)
    fig, ax = plt.subplots(figsize=(6.4, 3.2))
    ax.plot(ages, shares, color=BLUE)
    median, p95 = percentile_from_counts(overall, 0.5), percentile_from_counts(overall, 0.95)
    for value, label in ((median, "median"), (p95, "95th percentile")):
        ax.axvline(value, color=MUTED, linestyle="--", linewidth=1)
        ax.annotate(f"{label}: {value} s", (value, 8), xytext=(4, 0), textcoords="offset points", color=INK)
    ax.set(xlabel="Age of prediction when polled (s)", ylabel="Predictions (cumulative %)",
           title="Feed delay: how old TfL's predictions were when polled", xlim=(0, ages[-1] + 10), ylim=(0, 100))
    fig.savefig(args.out / "feed_delay.png")
    print(f"Wrote feed_delay.md and feed_delay.png to {args.out}")


if __name__ == "__main__":
    main()
