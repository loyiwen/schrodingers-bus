import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from analyse import analyse_line, parse_time

START = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def stamp(seconds):
    return (START + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def prediction(vehicle, expected, stop="S1"):
    return {"vehicleId": vehicle, "tripId": 1, "naptanId": stop, "direction": "inbound",
            "expectedArrival": stamp(expected)}


def poll(seconds, *arrivals, ok=True):
    return {"polled_at": stamp(seconds), "line": "25", "ok": ok, "arrivals": list(arrivals)}


# Another bus keeps the feed non-empty, so a poll counts as usable
OTHER = prediction("OTHER", 9000, stop="S9")


class ParseTime(unittest.TestCase):
    def test_handles_tfl_seven_digit_fractions(self):
        self.assertEqual(parse_time("2026-11-06T12:00:00.1234567Z"), START.timestamp())


class Classification(unittest.TestCase):
    def test_leaving_around_due_time_is_an_arrival(self):
        r = analyse_line([poll(0, OTHER, prediction("A", 40)), poll(30, OTHER, prediction("A", 40)), poll(60, OTHER)])
        self.assertEqual(len(r["arrivals"]), 1)
        self.assertEqual(r["arrivals"][0][2], (START + timedelta(seconds=40)).timestamp())
        self.assertEqual(r["vanished"], [])

    def test_leaving_well_before_due_time_is_vanished(self):
        r = analyse_line([poll(0, OTHER, prediction("A", 900)), poll(30, OTHER, prediction("A", 900)), poll(60, OTHER)])
        self.assertEqual(len(r["vanished"]), 1)
        self.assertEqual(r["vanished"][0]["seconds_early"], 840)
        self.assertEqual(r["arrivals"], [])

    def test_still_visible_at_the_end_is_not_classified(self):
        r = analyse_line([poll(0, OTHER, prediction("A", 900)), poll(30, OTHER, prediction("A", 900))])
        self.assertEqual((r["vanished"], r["arrivals"]), ([], []))

    def test_failed_and_empty_polls_are_skipped(self):
        r = analyse_line([poll(0, OTHER, prediction("A", 900)), poll(30, ok=False), poll(60),
                          poll(90, OTHER, prediction("A", 900))])
        self.assertEqual(r["vanished"], [])
        self.assertEqual((r["failed_polls"], r["empty_polls"]), (1, 1))


if __name__ == "__main__":
    unittest.main()