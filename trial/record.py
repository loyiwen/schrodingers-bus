#!/usr/bin/env python3
"""Record TfL bus arrival predictions."""
import json
import urllib.request
from datetime import datetime, timezone

API = "https://api.tfl.gov.uk/Line/{}/Arrivals"
# Only keep the fields the analysis needs
FIELDS = (
    "vehicleId", "tripId", "naptanId", "stationName", "lineId", "direction",
    "destinationName", "timestamp", "timeToStation", "expectedArrival", "timeToLive",
)


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


if __name__ == "__main__":
    result = poll("25")
    print(f"ok={result['ok']} error={result['error']} predictions={len(result['arrivals'])}")
    if result["arrivals"]:
        print(json.dumps(result["arrivals"][0], indent=2))