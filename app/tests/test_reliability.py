import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
from reliability import report


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


if __name__ == "__main__":
    unittest.main()