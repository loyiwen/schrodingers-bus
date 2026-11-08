#!/usr/bin/env python3
"""Record TfL bus arrival predictions."""
import json
import urllib.request

API = "https://api.tfl.gov.uk/Line/{}/Arrivals"


def fetch(line):
    """Return TfL's current arrival predictions for one route."""
    request = urllib.request.Request(API.format(line), headers={"User-Agent": "schrodingers-bus-trial"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


if __name__ == "__main__":
    arrivals = fetch("25")
    print(f"{len(arrivals)} predictions")
    if arrivals:  # The feed can be empty, as it was overnight
        print(json.dumps(arrivals[0], indent=2))