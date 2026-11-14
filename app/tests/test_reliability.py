import json
import random
import statistics
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
from reliability import (bootstrap_median_interval, group_convoys, group_vanished, merge_repeat_arrivals, report,
                         stop_intervals)


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


class MergeRepeatArrivals(unittest.TestCase):
    def test_same_bus_soon_after_is_one_arrival_keeping_the_last(self):
        arrivals = [("S1", "in", 100, "A"), ("S1", "in", 160, "A"), ("S1", "in", 3000, "A"), ("S1", "in", 130, "B")]
        merged, repeats = merge_repeat_arrivals(arrivals)
        self.assertEqual(repeats, 1)
        self.assertEqual(merged, [("S1", "in", 130, "B"), ("S1", "in", 160, "A"), ("S1", "in", 3000, "A")])

    def test_other_stops_and_directions_are_not_merged(self):
        arrivals = [("S1", "in", 100, "A"), ("S2", "in", 110, "A"), ("S1", "out", 120, "A")]
        self.assertEqual(merge_repeat_arrivals(arrivals)[1], 0)


def detection(stop, at, vehicles, direction="inbound"):
    return {"stop": stop, "direction": direction, "at": at, "vehicles": sorted(vehicles)}


class GroupConvoys(unittest.TestCase):
    def test_one_bunch_along_the_route_is_one_convoy(self):
        detections = [detection("S1", 0, "ABC"), detection("S2", 120, "ABC"), detection("S3", 240, "BCD")]
        convoys = group_convoys(detections)
        self.assertEqual(len(convoys), 1)
        self.assertEqual((convoys[0]["stops"], convoys[0]["vehicles"]), (3, ["A", "B", "C", "D"]))

    def test_different_buses_directions_or_times_stay_apart(self):
        detections = [detection("S1", 0, "ABC"), detection("S1", 60, "XYZ"),
                      detection("S2", 120, "ABC", "outbound"), detection("S9", 4000, "ABC")]
        self.assertEqual(len(group_convoys(detections)), 4)


def vanished_row(vehicle, missing_at, trip=1, stop="S1"):
    return {"stop": stop, "direction": "inbound", "vehicle": vehicle, "trip": trip,
            "missing_at": missing_at, "seconds_early": 300}


class GroupVanished(unittest.TestCase):
    def test_one_event_per_vehicle_and_trip(self):
        rows = [vanished_row("A", 100, stop="S1"), vanished_row("A", 100, stop="S2"), vanished_row("A", 130, stop="S3")]
        events = group_vanished(rows, {"A": [0, 30, 60]}, 2000)
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["stops"], events[0]["missing_at"], events[0]["kind"]), (3, 100, "vanished"))

    def test_vehicle_back_within_the_wait_returned(self):
        events = group_vanished([vanished_row("A", 100)], {"A": [0, 400]}, 2000)
        self.assertEqual(events[0]["kind"], "returned")

    def test_vehicle_back_after_the_wait_still_vanished(self):
        events = group_vanished([vanished_row("A", 100)], {"A": [0, 900]}, 2000)
        self.assertEqual(events[0]["kind"], "vanished")

    def test_too_close_to_the_end_is_unconfirmed(self):
        events = group_vanished([vanished_row("A", 100)], {"A": [0]}, 400)
        self.assertEqual(events[0]["kind"], "unconfirmed")


class ReportOnASample(unittest.TestCase):
    def test_counts_convoys_and_vanished_vehicles(self):
        # Three buses arrive at S1 within two minutes, and bus D leaves the feed 10 minutes early for good
        def p(vehicle, expected, stop="S1"):
            return {**prediction(vehicle, expected), "naptanId": stop}
        later = "2026-11-06T13:00:00Z"
        polls = [poll("2026-11-06T12:00:00Z", p("A", "2026-11-06T12:01:00Z"), p("B", "2026-11-06T12:01:30Z"),
                      p("C", "2026-11-06T12:02:00Z"), p("D", "2026-11-06T12:20:00Z"), p("E", later))]
        for minute in range(1, 20):
            polls.append(poll(f"2026-11-06T12:{minute:02}:30Z", p("E", later)))
        result = report(polls, 50, "seed")
        self.assertEqual((result["convoys"], result["convoy_groups"]), (1, 1))
        self.assertEqual((result["vanished_vehicles"], result["vanished_returned"]), (1, 0))


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