#!/usr/bin/env python3
"""Record TfL bus arrival predictions."""
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.tfl.gov.uk/Line/{}/Arrivals"
# Only keep the fields the analysis needs
FIELDS = (
    "vehicleId", "tripId", "naptanId", "stationName", "lineId", "direction",
    "destinationName", "timestamp", "timeToStation", "expectedArrival", "timeToLive",
)
LINES = ("25", "W5")
INTERVAL_SECONDS = 30
RECORD_MINUTES = 2
OUT_DIR = Path(__file__).parent / "data"


def utc_now():
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch(line):
    """Return TfL's current arrival predictions for one route."""
    request = urllib.request.Request(API.format(line), headers={"User-Agent": "schrodingers-bus-trial"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def poll(line):
    """Poll one route once, and return a record whether or not it worked."""
    record = {"polled_at": utc_now(), "line": line, "ok": True, "error": None, "arrivals": []}
    try:
        record["arrivals"] = [{f: a.get(f) for f in FIELDS} for a in fetch(line)]
    except Exception as error:  # A failed poll is data too, so keep recording
        record["ok"] = False
        # Save only the error type, as the message could include a URL with a key
        record["error"] = type(error).__name__
    return record


def record(lines, interval, minutes, out_dir):
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
                    result = poll(line)
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


if __name__ == "__main__":
    record(LINES, INTERVAL_SECONDS, RECORD_MINUTES, OUT_DIR)