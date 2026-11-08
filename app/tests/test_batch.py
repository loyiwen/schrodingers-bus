import sys
import unittest
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
from batch import is_valid_batch_id, parse_batch, summarise


class BatchIds(unittest.TestCase):
    def test_accepts_safe_names(self):
        self.assertTrue(is_valid_batch_id("route-25_20261106T1900"))

    def test_rejects_unsafe_names(self):
        for bad in ("", None, "../secret", "a/b", "a.b", "-leading", "x" * 129):
            self.assertFalse(is_valid_batch_id(bad), bad)


class ParseBatch(unittest.TestCase):
    def test_reads_the_sample_batch(self):
        polls = parse_batch((HERE / "sample_batch.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(summarise(polls), {
            "polls": 2,
            "predictions": 3,
            "first_poll": "2026-11-06T19:00:00Z",
            "last_poll": "2026-11-06T19:00:30Z",
        })

    def test_ignores_blank_lines(self):
        self.assertEqual(len(parse_batch('\n{"polled_at":"t","arrivals":[]}\n\n')), 1)

    def test_rejects_bad_batches(self):
        for bad in ("", "\n", "not json", "[1]", '{"polled_at":"t"}', '{"arrivals":[]}', '{"polled_at":"t","arrivals":1}'):
            with self.assertRaises(ValueError, msg=bad):
                parse_batch(bad)


if __name__ == "__main__":
    unittest.main()