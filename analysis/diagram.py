#!/usr/bin/env python3
"""Draw the architecture diagram for the report."""
import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

OUT = Path(__file__).parent.parent / "trial" / "data" / "analysis" / "architecture.png"
INK, MUTED = "#0b0b0b", "#52514e"
# Fills are light tints, with the series colour as the outline, so text stays dark and readable
KINDS = {
    "function": ("#dce9f9", "#2a78d6"),
    "storage": ("#d6f2e7", "#1baf7a"),
    "external": ("#f1f0ec", "#52514e"),
}


def box(ax, x, y, w, h, title, detail, kind):
    """Draw a rounded box with a bold title and a smaller description, and return its edges for arrows."""
    fill, edge = KINDS[kind]
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.12",
                                facecolor=fill, edgecolor=edge, linewidth=1.4))
    ax.text(x + w / 2, y + h - 0.22, title, ha="center", va="top", fontsize=9.5, fontweight="bold", color=INK)
    ax.text(x + w / 2, y + h - 0.55, detail, ha="center", va="top", fontsize=7.8, color=MUTED, linespacing=1.35)
    return {"left": (x, y + h / 2), "right": (x + w, y + h / 2), "top": (x + w / 2, y + h),
            "bottom": (x + w / 2, y), "box": (x, y, w, h)}


def arrow(ax, start, end, label="", dashed=False, offset=(0, 0.12), rad=0.0, ha="center"):
    """Draw an arrow between two points, with an optional label near its middle."""
    ax.add_patch(FancyArrowPatch(start, end, arrowstyle="-|>", mutation_scale=11, linewidth=1.2, color=INK,
                                 linestyle=(0, (4, 3)) if dashed else "-", shrinkA=3, shrinkB=3,
                                 connectionstyle=f"arc3,rad={rad}"))
    if label:
        mid = ((start[0] + end[0]) / 2 + offset[0], (start[1] + end[1]) / 2 + offset[1])
        ax.text(*mid, label, ha=ha, va="bottom", fontsize=7.8, color=INK, linespacing=1.3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()

    fig, ax = plt.subplots(figsize=(10, 5.8))
    ax.set(xlim=(0, 13), ylim=(-0.8, 6.6))
    ax.axis("off")

    # Azure boundary, drawn first so everything sits on top of it
    ax.add_patch(FancyBboxPatch((3.3, -0.2), 9.65, 6.65, boxstyle="round,pad=0,rounding_size=0.2",
                                facecolor="none", edgecolor=MUTED, linewidth=1, linestyle=(0, (2, 2))))
    ax.text(3.45, 6.3, "Azure, UK South: Function App on the Flex Consumption plan (Python 3.12)",
            fontsize=8.5, color=MUTED, va="top")

    tfl = box(ax, 0.05, 4.95, 2.95, 1.1, "TfL Unified API", "Line/{id}/Arrivals\nlive predictions", "external")
    prep = box(ax, 0.05, 3.0, 2.95, 1.5, "Recorder and splitter", "record.py: every 30 s, 10 routes\nsplit.py: overlapping batches,\none route and window each", "external")
    vm = box(ax, 0.05, 0.85, 2.95, 1.7, "Load generator VM", "send.py: bursts or steady rate\ncollect.py: results, timings\nexperiment.py: 39-run plan", "external")

    f1 = box(ax, 3.8, 1.6, 2.45, 1.5, "Function 1", "receive_batch\nHTTP trigger (PUT)\nvalidates, then saves", "function")
    batches = box(ax, 6.85, 3.7, 2.3, 1.2, "Blob: batches", "{batch ID}.jsonl\nabout 12 MB each", "storage")
    queue = box(ax, 6.85, 1.6, 2.3, 1.5, "Queue", "batches-to-analyse\nmessage = batch ID", "storage")
    poison = box(ax, 6.85, 0.0, 2.3, 1.0, "Poison queue", "after 5 failed attempts", "storage")
    f2 = box(ax, 9.85, 1.6, 2.9, 1.5, "Function 2", "analyse_batch\nqueue trigger, at most\n3 at once per instance", "function")
    results = box(ax, 9.85, -0.05, 2.9, 1.15, "Blob: results", "{batch ID}.json\nmeasures and timings", "storage")
    insights = box(ax, 9.85, 4.75, 2.9, 0.95, "Application Insights", "logs, traces, exceptions", "external")

    arrow(ax, tfl["bottom"], prep["top"], "poll", offset=(0.1, -0.12), ha="left")
    arrow(ax, prep["bottom"], vm["top"], "copy batches", offset=(0.1, -0.12), ha="left")
    arrow(ax, (vm["right"][0], 2.35), (f1["left"][0], 2.35), "1  PUT")
    arrow(ax, f1["top"], batches["left"], "2  upload, finished\nbefore returning", offset=(-0.75, 0.2), rad=-0.25)
    arrow(ax, f1["right"], queue["left"], "3\nmessage", offset=(0, 0.1))
    arrow(ax, queue["right"], f2["left"], "4\ntrigger", offset=(0, 0.1))
    arrow(ax, batches["right"], (f2["top"][0] - 0.7, f2["top"][1]), "5  read batch", offset=(0.25, 0.25), rad=-0.2, ha="left")
    arrow(ax, f2["bottom"], results["top"], "6  write result", offset=(0.1, -0.15), ha="left")
    arrow(ax, queue["bottom"], poison["top"], "repeated failure", offset=(0.1, -0.15), ha="left")
    arrow(ax, (f2["top"][0] + 0.8, f2["top"][1]), (f2["top"][0] + 0.8, insights["bottom"][1]), "telemetry", dashed=True,
          offset=(0.12, -0.15), ha="left")

    # Step 7 runs along the bottom, outside Azure, back to the load generator
    path_x, path_y = [results["bottom"][0], results["bottom"][0], 1.5], [0.0, -0.55, -0.55]
    ax.plot(path_x, path_y, color=INK, linewidth=1.2, linestyle=(0, (4, 3)))
    arrow(ax, (1.5, -0.55), (1.5, vm["bottom"][1]), dashed=True)
    ax.text(6.3, -0.5, "7  collect results with a read-only SAS link", ha="center", va="bottom", fontsize=7.8, color=INK)

    fig.savefig(args.out, dpi=200, bbox_inches="tight", facecolor="white")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
