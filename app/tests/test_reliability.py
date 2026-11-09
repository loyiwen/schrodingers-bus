import json
import random
import statistics
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
from reliability import bootstrap_median_interval, report, stop_intervals


def poll(time, *arrivals):
    return {"polled_at": time, "ok": True, "arrivals": list(arrivals)}


def prediction(vehicle, expected):
    return {"vehicleId": vehicle, "tripId": 1, "naptanId": "S1", "direction": "inbound", "expectedArrival": expected}


class Report(unittest.TestCase):
    def test_one_arrival_has_no_gap(self):
        other = prediction("OTHER", "2026-11-06T14:00:00Z")
        result = report([
            poll("2026-11-06T12:00:00Z", other, prediction("A", "2026-11-06T12:00:40Z")),
            poll("2026-11-06T12:00:30Z", other, prediction("A", "2026-11-06T12:00:40Z")),
            poll("2026-11-06T12:01:00Z", other),
        ])
        self.assertEqual(result["estimated_arrivals"], 1)
        self.assertEqual(result["gaps"], 0)
        self.assertIsNone(result["median_gap_minutes"])

    def test_can_be_saved_as_json(self):
        polls = [poll("2026-11-06T12:00:00Z", prediction("A", "2026-11-06T13:00:00Z"))]
        self.assertEqual(json.loads(json.dumps(report(polls)))["vehicles"], 1)

    def test_no_usable_polls(self):
        result = report([{"polled_at": "2026-11-06T12:00:00Z", "ok": False, "arrivals": []}])
        self.assertEqual((result["polls"], result["failed_polls"]), (0, 1))


class BootstrapIntervals(unittest.TestCase):
    def test_interval_surrounds_the_median(self):
        values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        low, high = bootstrap_median_interval(values, 500, random.Random("test"))
        self.assertLessEqual(low, statistics.median(values))
        self.assertGreaterEqual(high, statistics.median(values))

    def test_identical_values_have_no_spread(self):
        self.assertEqual(bootstrap_median_interval([7, 7, 7, 7, 7], 100, random.Random("test")), (7, 7))

    def test_same_seed_gives_the_same_result(self):
        gaps = {("S1", "inbound"): [60, 120, 180, 240, 300, 360], ("S2", "inbound"): [600, 660, 720, 780, 840]}
        self.assertEqual(stop_intervals(gaps, 200, "batch-1"), stop_intervals(gaps, 200, "batch-1"))

    def test_stops_with_too_few_gaps_are_skipped(self):
        gaps = {("S1", "inbound"): [60, 120], ("S2", "inbound"): [60, 120, 180, 240, 300]}
        self.assertEqual([s["stop"] for s in stop_intervals(gaps, 50, "seed")], ["S2"])


if __name__ == "__main__":
    unittest.main()