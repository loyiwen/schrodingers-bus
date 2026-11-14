#!/usr/bin/env python3
"""Turn the experiment runs into the tables and figures for the report."""
import argparse
import csv
import json
import math
import random
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DATA = Path(__file__).parent.parent / "trial" / "data"
RESAMPLES = 2000  # Bootstrap resamples for each confidence interval
SEED = 3212  # Fixed, so the intervals come out the same every time

# Reference palette, light mode: the first three categorical slots, which stay distinct for colour-blind readers
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"


def parse_time(text):
    """Turn a millisecond UTC timestamp into a datetime."""
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def number(text):
    """Return a CSV cell as a float, or None if it's empty."""
    return float(text) if text not in ("", None) else None


def percentile(values, fraction):
    """Return the nearest-rank percentile of values."""
    values = sorted(values)
    return values[math.ceil(fraction * len(values)) - 1]


def bootstrap_interval(values, statistic, rng, resamples=RESAMPLES):
    """Return a 95% percentile bootstrap interval for statistic over values."""
    estimates = sorted(statistic(rng.choices(values, k=len(values))) for _ in range(resamples))
    return estimates[int(0.025 * resamples)], estimates[int(0.975 * resamples) - 1]


def load_run(runs, name):
    """Return one run's per-batch rows, and its send log keyed by batch ID."""
    with (runs / f"{name}.csv").open(encoding="utf-8") as table:
        rows = list(csv.DictReader(table))
    sent = {}
    for line in (runs / f"{name}.jsonl").read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        sent[record["batch_id"]] = record
    return rows, sent


def run_metrics(rows, sent):
    """Return the figures for one run, plus the per-batch values that get pooled across repeats."""
    done = [row for row in rows if number(row["runtime_seconds"]) is not None]
    first_sent = min(parse_time(row["sent_at"]) for row in rows)
    last_sent = max(parse_time(row["sent_at"]) for row in rows)
    finished = [parse_time(row["finished_at"]) for row in done if row["finished_at"]]
    started = [parse_time(row["started_at"]) for row in done if row["started_at"]]
    send_ends = [parse_time(row["sent_at"]).timestamp() + number(row["send_seconds"]) for row in rows]
    metrics = {
        "sent": len(rows),
        "results": len(done),
        "missing": sum(row["status"] == "202" and number(row["runtime_seconds"]) is None for row in rows),
        "retried": sum((number(row["dequeue_count"]) or 1) > 1 for row in done),
        "hosts": len({row["host_name"] for row in done if row["host_name"]}),
        "cold_starts": sum(row["instance_run"] == "1" for row in done),
        "send_window_seconds": (last_sent - first_sent).total_seconds(),
        # How fast batches got in: sent and accepted, divided by the time from first send to last reply
        "intake_rate": len(rows) / (max(send_ends) - first_sent.timestamp()),
        "max_late_seconds": max((record.get("late_seconds") or 0) for record in sent.values()),
        "end_to_end": [number(row["end_to_end_seconds"]) for row in done if row["end_to_end_seconds"]],
        "send": [number(row["send_seconds"]) for row in done],
        "queue": [number(row["queue_delay_seconds"]) for row in done],
        "runtime": [number(row["runtime_seconds"]) for row in done],
        "predictions": [number(row["predictions"]) for row in done if row["predictions"]],
    }
    if finished:
        metrics["makespan_seconds"] = (max(finished) - first_sent).total_seconds()
        metrics["throughput"] = len(finished) / metrics["makespan_seconds"]
        # From the first batch starting to the last finishing, so upload time is left out
        processing = (max(finished) - min(started)).total_seconds()
        metrics["processing_throughput"] = len(finished) / processing if processing else None
    return metrics


def summarise_level(runs, rng):
    """Pool one level's repeats: latency statistics with bootstrap intervals, and run-level medians and ranges."""
    pooled = {key: [value for run in runs for value in run[key]] for key in ("end_to_end", "send", "queue", "runtime", "predictions")}
    summary = {"repeats": len(runs), "batches": sum(run["results"] for run in runs),
               "missing": sum(run["missing"] for run in runs), "retried": sum(run["retried"] for run in runs)}
    e2e = pooled["end_to_end"]
    if e2e:
        summary["e2e_median"] = statistics.median(e2e)
        summary["e2e_median_ci"] = bootstrap_interval(e2e, statistics.median, rng)
        summary["e2e_p95"] = percentile(e2e, 0.95)
        summary["e2e_p95_ci"] = bootstrap_interval(e2e, lambda sample: percentile(sample, 0.95), rng)
    for stage in ("send", "queue", "runtime"):
        summary[f"{stage}_median"] = statistics.median(pooled[stage]) if pooled[stage] else None
    if pooled["predictions"] and pooled["runtime"]:
        summary["predictions_median"] = statistics.median(pooled["predictions"])
    for key in ("makespan_seconds", "throughput", "processing_throughput", "hosts", "cold_starts", "max_late_seconds", "intake_rate"):
        values = [run[key] for run in runs if run.get(key) is not None]
        if values:
            summary[key] = statistics.median(values)
            summary[f"{key}_range"] = (min(values), max(values))
    return summary


def load_experiments(runs):
    """Return {experiment: {level: [run metrics]}} for every run recorded in experiments.jsonl."""
    grouped = defaultdict(lambda: defaultdict(list))
    latest = {}
    # A run collected twice, after a restart, keeps its last record
    for line in (runs / "experiments.jsonl").read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        latest[record["run"]] = record
    for name, record in latest.items():
        rows, sent = load_run(runs, name)
        metrics = run_metrics(rows, sent)
        metrics["run"] = name
        grouped[record["experiment"]][record["level"]].append(metrics)
    return grouped


def is_cold(run):
    """A run counts as cold when most of its hosts were new, after the platform had scaled in."""
    return run["hosts"] and run["cold_starts"] >= run["hosts"] / 2


def write_run_table(grouped, out, rng):
    """Write one row per run, and a Markdown comparison of cold and warm runs, pooled across experiments."""
    columns = ("run", "experiment", "level", "cold", "hosts", "cold_starts", "makespan_seconds", "intake_rate",
               "e2e_median", "e2e_p95", "queue_median", "runtime_median")
    rows = []
    for experiment, by_level in grouped.items():
        for level, runs in sorted(by_level.items()):
            for run in runs:
                rows.append({"run": run["run"], "experiment": experiment, "level": level, "cold": bool(is_cold(run)),
                             "hosts": run["hosts"], "cold_starts": run["cold_starts"],
                             "makespan_seconds": round(run.get("makespan_seconds", 0), 2),
                             "intake_rate": round(run["intake_rate"], 2),
                             "e2e_median": round(statistics.median(run["end_to_end"]), 2),
                             "e2e_p95": round(percentile(run["end_to_end"], 0.95), 2),
                             "queue_median": round(statistics.median(run["queue"]), 2),
                             "runtime_median": round(statistics.median(run["runtime"]), 3)})
    with (out / "runs.csv").open("w", newline="", encoding="utf-8") as table:
        writer = csv.DictWriter(table, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)

    # Bursts and lengths only: steady-rate runs warm themselves up within seconds, so their start state hardly matters
    lines = ["## Cold against warm runs (bursts and batch lengths)", "",
             "| Runs | Count | Batches | Queue delay median (s) | End-to-end median (s) [95% CI] | End-to-end p95 (s) [95% CI] |",
             "|---|---:|---:|---:|---|---|"]
    for label, cold in (("Cold", True), ("Warm", False)):
        chosen = [run for experiment in ("burst", "length") for runs in grouped.get(experiment, {}).values()
                  for run in runs if bool(is_cold(run)) == cold]
        e2e = [value for run in chosen for value in run["end_to_end"]]
        queue = [value for run in chosen for value in run["queue"]]
        if not e2e:
            continue
        median_ci = bootstrap_interval(e2e, statistics.median, rng)
        p95_ci = bootstrap_interval(e2e, lambda sample: percentile(sample, 0.95), rng)
        lines.append(f"| {label} | {len(chosen)} | {len(e2e)} | {statistics.median(queue):.2f} | "
                     f"{statistics.median(e2e):.2f} [{median_ci[0]:.2f} to {median_ci[1]:.2f}] | "
                     f"{percentile(e2e, 0.95):.2f} [{p95_ci[0]:.2f} to {p95_ci[1]:.2f}] |")
    return "\n".join(lines) + "\n"


def write_tables(levels_by_experiment, out):
    """Write a CSV per experiment and one Markdown file with every table, for the report."""
    columns = ("level", "repeats", "batches", "missing", "retried", "hosts", "hosts_range", "cold_starts",
               "makespan_seconds", "makespan_seconds_range", "throughput", "processing_throughput",
               "e2e_median", "e2e_median_ci", "e2e_p95", "e2e_p95_ci", "send_median", "queue_median",
               "runtime_median", "predictions_median", "max_late_seconds", "intake_rate")
    markdown = []

    def show(value):
        if isinstance(value, tuple):
            return f"{value[0]:.2f} to {value[1]:.2f}" if isinstance(value[0], float) else f"{value[0]} to {value[1]}"
        return f"{value:.2f}" if isinstance(value, float) else ("" if value is None else str(value))

    for experiment, levels in levels_by_experiment.items():
        rows = [{"level": level, **summary} for level, summary in sorted(levels.items())]
        with (out / f"summary_{experiment}.csv").open("w", newline="", encoding="utf-8") as table:
            writer = csv.DictWriter(table, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows({key: show(row.get(key)) for key in columns} for row in rows)
        shown = [column for column in columns if any(row.get(column) is not None for row in rows)]
        markdown += [f"## {experiment}", "", "| " + " | ".join(shown) + " |", "|" + "---|" * len(shown)]
        markdown += ["| " + " | ".join(show(row.get(column)) for column in shown) + " |" for row in rows]
        markdown.append("")
    (out / "tables.md").write_text("\n".join(markdown), encoding="utf-8")


def style(plt):
    """Apply quiet axes, a recessive grid and readable type to every figure."""
    plt.rcParams.update({
        "figure.dpi": 150, "savefig.dpi": 200, "savefig.bbox": "tight", "font.size": 9,
        "axes.edgecolor": MUTED, "axes.labelcolor": INK, "axes.titlesize": 10, "axes.titleweight": "bold",
        "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True, "axes.axisbelow": True,
        "grid.color": GRID, "grid.linewidth": 0.6, "xtick.color": MUTED, "ytick.color": MUTED,
        "lines.linewidth": 1.6, "lines.markersize": 5, "legend.frameon": False,
    })


def error_bars(levels, key):
    """Return the asymmetric error bars for a statistic and its interval at each level."""
    low = [levels[level][key] - levels[level][f"{key}_ci"][0] for level in levels]
    high = [levels[level][f"{key}_ci"][1] - levels[level][key] for level in levels]
    return [low, high]


def latency_figure(plt, levels, xlabel, title, path, log_x):
    """Plot median and 95th-percentile end-to-end time with bootstrap intervals against load."""
    levels = {level: levels[level] for level in sorted(levels) if "e2e_median" in levels[level]}
    x = list(levels)
    fig, ax = plt.subplots(figsize=(6.4, 3.4))
    for key, colour, label in (("e2e_median", BLUE, "Median"), ("e2e_p95", ORANGE, "95th percentile")):
        y = [levels[level][key] for level in x]
        ax.errorbar(x, y, yerr=error_bars(levels, key), color=colour, marker="o", capsize=3, label=label)
        ax.annotate(label, (x[-1], y[-1]), xytext=(6, 0), textcoords="offset points", color=INK, va="center")
    if log_x:
        ax.set_xscale("log")
        ax.minorticks_off()
    ax.set_xticks(x, [str(level) for level in x])  # Only the levels that were tested
    ax.set(xlabel=xlabel, ylabel="End-to-end time (s)", title=title, ylim=(0, None))
    ax.legend(loc="lower right")
    fig.savefig(path)
    plt.close(fig)


def load_panels_figure(plt, levels, xlabel, title, path, log_x):
    """Plot throughput and hosts as two panels, rather than one chart with two y-axes."""
    x = sorted(level for level in levels if "throughput" in levels[level])
    fig, (top, bottom) = plt.subplots(2, 1, figsize=(6.4, 4.6), sharex=True)
    for ax, key, colour, ylabel in ((top, "throughput", BLUE, "Results per second"), (bottom, "hosts", AQUA, "Hosts used")):
        y = [levels[level][key] for level in x]
        spread = [[y[i] - levels[level][f"{key}_range"][0] for i, level in enumerate(x)],
                  [levels[level][f"{key}_range"][1] - y[i] for i, level in enumerate(x)]]
        ax.errorbar(x, y, yerr=spread, color=colour, marker="o", capsize=3)
        ax.set(ylabel=ylabel, ylim=(0, None))
    if log_x:
        bottom.set_xscale("log")
        bottom.minorticks_off()
    bottom.set_xticks(x, [str(level) for level in x])  # Only the levels that were tested
    top.set_title(title)
    bottom.set_xlabel(xlabel + "\nPoints are medians of the repeats, and bars show their range")
    fig.savefig(path)
    plt.close(fig)


def stage_figure(plt, levels, xlabel, title, path):
    """Stack the median send, queue and analysis times, so the dominant stage stands out."""
    x = [level for level in sorted(levels) if levels[level].get("runtime_median") is not None]
    labels = [str(level) for level in x]
    fig, ax = plt.subplots(figsize=(6.4, 3.4))
    bottom = [0.0] * len(x)
    for key, colour, label in (("send_median", BLUE, "Upload and Function 1"), ("queue_median", ORANGE, "Queue delay"),
                               ("runtime_median", AQUA, "Function 2 runtime")):
        heights = [levels[level][key] or 0 for level in x]
        # White edges leave a small gap between stacked segments
        ax.bar(labels, heights, bottom=bottom, color=colour, edgecolor="white", linewidth=1.5, width=0.6, label=label)
        bottom = [b + h for b, h in zip(bottom, heights)]
    ax.set(xlabel=xlabel, ylabel="Median time (s)", title=title)
    # Legend above the plot, so it never covers a bar
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, 1.0), ncols=3)
    ax.set_title(title, pad=24)
    ax.set_ylim(0, max(bottom) * 1.1)
    fig.savefig(path)
    plt.close(fig)


def timeline_figure(plt, runs, name, path):
    """Show hosts starting and results finishing over time for one run, in two panels on a shared time axis."""
    rows, _ = load_run(runs, name)
    done = [row for row in rows if row["finished_at"] and row["host_name"]]
    zero = min(parse_time(row["sent_at"]) for row in rows)
    first_start = defaultdict(lambda: math.inf)
    for row in done:
        moment = (parse_time(row["started_at"]) - zero).total_seconds()
        first_start[row["host_name"]] = min(first_start[row["host_name"]], moment)
    host_times = sorted(first_start.values())
    finish_times = sorted((parse_time(row["finished_at"]) - zero).total_seconds() for row in done)
    sends = sorted((parse_time(row["sent_at"]) - zero).total_seconds() for row in rows)
    fig, (top, bottom) = plt.subplots(2, 1, figsize=(6.4, 4.6), sharex=True)
    top.step(host_times, range(1, len(host_times) + 1), where="post", color=AQUA)
    top.set(ylabel="Hosts started", title=f"Scale-out during {name}", ylim=(0, None))
    bottom.step(sends, range(1, len(sends) + 1), where="post", color=BLUE, label="Sent")
    bottom.step(finish_times, range(1, len(finish_times) + 1), where="post", color=ORANGE, label="Analysed")
    bottom.set(xlabel="Seconds after the first batch was sent", ylabel="Batches (cumulative)", ylim=(0, None))
    bottom.legend(loc="lower right")
    fig.savefig(path)
    plt.close(fig)


def length_figure(plt, levels, path):
    """Plot Function 2's median runtime against batch length, with predictions per batch as a second panel."""
    x = sorted(level for level in levels if levels[level].get("runtime_median") is not None)
    labels = [f"{level} min" for level in x]
    fig, (left, right) = plt.subplots(1, 2, figsize=(6.4, 3.0))
    left.bar(labels, [levels[level]["runtime_median"] for level in x], color=BLUE, width=0.6)
    left.set(title="Function 2 runtime", ylabel="Median (s)")
    right.bar(labels, [levels[level].get("predictions_median", 0) / 1000 for level in x], color=AQUA, width=0.6)
    right.set(title="Batch size", ylabel="Median predictions (thousands)")
    for ax in (left, right):
        ax.set_xlabel("Batch length")
        ax.grid(axis="x", visible=False)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def speedup_figure(plt, burst_runs, path):
    """Plot processing throughput against hosts, next to an ideal line from the smallest bursts' runtimes."""
    points = [(run["hosts"], run["processing_throughput"]) for runs in burst_runs.values() for run in runs
              if run.get("processing_throughput") and run["hosts"]]
    smallest = burst_runs[min(burst_runs)]
    # One host analyses one batch at a time on its single core, so its rate is about 1 / runtime
    single = 1 / statistics.median([value for run in smallest for value in run["runtime"]])
    fig, ax = plt.subplots(figsize=(6.4, 3.4))
    ax.scatter([p[0] for p in points], [p[1] for p in points], color=BLUE, s=28, zorder=3, label="Burst runs")
    top = max(p[0] for p in points) * 1.05
    ax.plot([0, top], [0, top * single], color=MUTED, linestyle="--", linewidth=1,
            label=f"If analysis were the limit: hosts x {single:.2f} batches/s")
    # The rate batches actually got in, from the bursts large enough to reach a steady upload
    intake = statistics.median(run["intake_rate"] for size, runs in burst_runs.items() if size >= 250 for run in runs)
    ax.axhline(intake, color=ORANGE, linewidth=1.6, label=f"Measured intake: {intake:.1f} batches/s")
    ax.set(xlabel="Hosts used", ylabel="Batches analysed per second", title="Throughput against hosts: limited by intake",
           xlim=(0, None), ylim=(0, None))
    ax.legend(loc="upper left")
    fig.savefig(path)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, default=DATA / "runs")
    parser.add_argument("--out", type=Path, default=DATA / "analysis")
    args = parser.parse_args()
    import matplotlib
    matplotlib.use("Agg")  # Writes files without needing a display
    import matplotlib.pyplot as plt

    args.out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)
    grouped = load_experiments(args.runs)
    levels = {experiment: {level: summarise_level(runs, rng) for level, runs in by_level.items()}
              for experiment, by_level in grouped.items()}
    write_tables(levels, args.out)
    cold_warm = write_run_table(grouped, args.out, rng)
    with (args.out / "tables.md").open("a", encoding="utf-8") as tables:
        tables.write("\n" + cold_warm)

    style(plt)
    if "burst" in levels:
        latency_figure(plt, levels["burst"], "Burst size (batches)", "End-to-end time against burst size",
                       args.out / "burst_latency.png", log_x=True)
        load_panels_figure(plt, levels["burst"], "Burst size (batches)", "Throughput and hosts against burst size",
                           args.out / "burst_throughput_hosts.png", log_x=True)
        stage_figure(plt, levels["burst"], "Burst size (batches)", "Where the time goes", args.out / "burst_stages.png")
        speedup_figure(plt, grouped["burst"], args.out / "speedup.png")
        # The repeat with the middle makespan, so one straggling request doesn't stretch the picture
        largest = sorted(grouped["burst"][max(grouped["burst"])], key=lambda run: run["makespan_seconds"])
        timeline_figure(plt, args.runs, largest[len(largest) // 2]["run"], args.out / "burst_timeline.png")
    if "rate" in levels:
        latency_figure(plt, levels["rate"], "Steady rate (batches per second)", "End-to-end time against steady rate",
                       args.out / "rate_latency.png", log_x=False)
        load_panels_figure(plt, levels["rate"], "Steady rate (batches per second)", "Throughput and hosts against steady rate",
                           args.out / "rate_throughput_hosts.png", log_x=False)
    if "length" in levels:
        length_figure(plt, levels["length"], args.out / "length_runtime.png")
    print(f"Wrote tables and figures to {args.out}")


if __name__ == "__main__":
    main()
