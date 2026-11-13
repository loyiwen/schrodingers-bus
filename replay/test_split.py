import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "app"))
from batch import is_valid_batch_id, parse_batch
from split import split, window_numbers

START = datetime(2026, 11, 9, 6, 0, tzinfo=timezone.utc)


def poll_line(minute, line="25", empty=False):
    """Build one recording line for a poll this many minutes after START."""
    polled_at = (START + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return json.dumps({"polled_at": polled_at, "line": line, "ok": True, "error": None, "arrivals": [] if empty else [{"vehicleId": "LX1"}]}) + "\n"


class WindowNumbers(unittest.TestCase):
    def test_first_poll_is_only_in_the_first_window(self):
        self.assertEqual(list(window_numbers(0, 3600, 1800)), [0])

    def test_overlap_puts_a_poll_in_two_windows(self):
        self.assertEqual(list(window_numbers(1800, 3600, 1800)), [0, 1])

    def test_window_end_is_excluded(self):
        self.assertEqual(list(window_numbers(3600, 3600, 1800)), [1, 2])

    def test_no_overlap_means_one_window(self):
        self.assertEqual(list(window_numbers(3700, 3600, 3600)), [1])


class Split(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.recording = self.root / "recording.jsonl"
        self.out = self.root / "batches"

    def write(self, *lines):
        self.recording.write_text("".join(lines), encoding="utf-8")

    def polls_in(self, path):
        return [poll["polled_at"][11:16] for poll in parse_batch(path.read_text(encoding="utf-8"))]

    def test_overlapping_batches_share_polls(self):
        self.write(*[poll_line(minute) for minute in range(0, 105, 15)])
        paths, _, _ = split(self.recording, self.out, 60, 30)
        self.assertEqual(len(paths), 2)
        self.assertEqual(self.polls_in(paths[0]), ["06:00", "06:15", "06:30", "06:45"])
        self.assertEqual(self.polls_in(paths[1]), ["06:30", "06:45", "07:00", "07:15"])

    def test_cut_off_window_is_removed(self):
        self.write(*[poll_line(minute) for minute in range(0, 75, 15)])
        paths, _, _ = split(self.recording, self.out, 60, 30)
        self.assertEqual(len(paths), 1)
        self.assertEqual(len(list(self.out.iterdir())), 1)

    def test_routes_are_kept_apart_and_can_be_chosen(self):
        self.write(poll_line(0), poll_line(0, "W5"), poll_line(60), poll_line(60, "W5"))
        everything, _, _ = split(self.recording, self.out, 60, 60)
        self.assertEqual([path.name[:2] for path in everything], ["25", "W5"])
        chosen, _, _ = split(self.recording, self.root / "w5", 60, 60, {"W5"})
        self.assertEqual([path.name[:2] for path in chosen], ["W5"])

    def test_batch_names_are_valid_and_show_the_length(self):
        self.write(poll_line(0), poll_line(60))
        paths, _, _ = split(self.recording, self.out, 60, 30)
        self.assertEqual(paths[0].stem, "25-20261109T060000-60m")
        self.assertTrue(is_valid_batch_id(paths[0].stem))

    def test_half_written_last_line_is_counted_and_skipped(self):
        self.write(poll_line(0), poll_line(60), '{"polled_at":"2026')
        paths, bad_lines, _ = split(self.recording, self.out, 60, 30)
        self.assertEqual(bad_lines, 1)
        self.assertEqual(self.polls_in(paths[0]), ["06:00"])

    def test_mostly_empty_batch_is_skipped(self):
        # Half empty in the first window is kept, but three quarters in the second isn't
        empties = (0, 15, 60, 75, 90)
        self.write(*[poll_line(minute, empty=minute in empties) for minute in range(0, 135, 15)])
        paths, _, empty_batches = split(self.recording, self.out, 60, 60)
        self.assertEqual([path.stem for path in paths], ["25-20261109T060000-60m"])
        self.assertEqual(empty_batches, 1)
        self.assertEqual(len(list(self.out.iterdir())), 1)

    def test_refuses_a_folder_with_files_in_it(self):
        self.write(poll_line(0), poll_line(60))
        split(self.recording, self.out, 60, 30)
        with self.assertRaises(ValueError):
            split(self.recording, self.out, 60, 30)


if __name__ == "__main__":
    unittest.main()