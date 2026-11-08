#!/usr/bin/env python3
"""Record TfL bus arrival predictions."""
import argparse
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.tfl.gov.uk/Line/{}/Arrivals"
# Only keep the fields the analysis needs
FIELDS = (
    "vehicleId", "tripId", "naptanId", "stationName", "lineId", "direction",
    "destinationName", "timestamp", "timeToStation", "expectedArrival", "timeToLive",
)


def utc_now():
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch(line, key):
    """Return TfL's current arrival predictions for one route."""
    url = API.format(urllib.parse.quote(line))
    if key:
        url += "?" + urllib.parse.urlencode({"app_key": key})
    request = urllib.request.Request(url, headers={"User-Agent": "schrodingers-bus-trial"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def poll(line, key):
    """Poll one route once, and return a record whether or not it worked."""
    record = {"polled_at": utc_now(), "line": line, "ok": True, "error": None, "arrivals": []}
    try:
        record["arrivals"] = [{f: a.get(f) for f in FIELDS} for a in fetch(line, key)]
    except Exception as error:  # A failed poll is data too, so keep recording
        record["ok"] = False
        # Save only the error type, as the message could include a URL with a key
        record["error"] = type(error).__name__
    return record


def record(lines, interval, minutes, out_dir, key):
    """Poll each route on a fixed schedule, writing one JSON line per poll."""
    # Each run writes to its own timestamped file
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"tfl-{stamp}.jsonl"
    deadline = time.time() + minutes * 60
    next_poll = time.time()
    print(f"Recording {','.join(lines)} to {path}", flush=True)

    try:
        with path.open("a", encoding="utf-8") as output:
            while time.time() < deadline:
                for line in lines:
                    result = poll(line, key)
                    output.write(json.dumps(result, separators=(",", ":")) + "\n")
                    output.flush()  # Write each poll straight away, so a crash loses little
                    status = f"{len(result['arrivals'])} predictions" if result["ok"] else result["error"]
                    print(f"{result['polled_at']} {line}: {status}", flush=True)
                # Fixed slots stop delays adding up, and max avoids a burst of catch-up polls
                next_poll = max(next_poll + interval, time.time())
                wait = min(next_poll, deadline) - time.time()
                if wait > 0:
                    time.sleep(wait)
    except KeyboardInterrupt:
        print("Stopped early", flush=True)
    print(f"Saved {path} ({path.stat().st_size} bytes)", flush=True)


def route_list(text):
    """Parse a comma-separated list of route IDs, such as 25,W5."""
    lines = [part.strip() for part in text.split(",") if part.strip()]
    # Route IDs go into the URL, so only allow letters, digits and hyphens
    if not lines or not all(re.fullmatch(r"[A-Za-z0-9-]+", line) for line in lines):
        raise argparse.ArgumentTypeError("routes must look like 25,W5")
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lines", type=route_list, default=route_list("25,W5"))
    parser.add_argument("--interval", type=float, default=30, help="seconds between polls")
    parser.add_argument("--minutes", type=float, default=120, help="how long to record")
    parser.add_argument("--out", type=Path, default=Path(__file__).parent / "data")
    args = parser.parse_args()
    # The key comes from the environment, so it stays out of the code and shell history
    record(args.lines, args.interval, args.minutes, args.out, os.environ.get("TFL_APP_KEY"))


if __name__ == "__main__":
    main()