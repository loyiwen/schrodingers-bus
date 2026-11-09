import sys
import unittest
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
from batch import is_valid_batch_id, parse_batch


class BatchIds(unittest.TestCase):
    def test_accepts_safe_names(self):
        self.assertTrue(is_valid_batch_id("route-25_20261106T1900"))

    def test_rejects_unsafe_names(self):
        for bad in ("", "\n", "not json", "[1]",
                    '{"polled_at":"t"}', '{"arrivals":[]}',
                    '{"polled_at":"t","arrivals":[]}',
                    '{"polled_at":"t","ok":true,"arrivals":1}'):
            self.assertFalse(is_valid_batch_id(bad), bad)


class ParseBatch(unittest.TestCase):
    def test_reads_the_sample_batch(self):
        polls = parse_batch((HERE / "sample_batch.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(len(polls), 2)
        self.assertEqual(sum(len(poll["arrivals"]) for poll in polls), 3)

    def test_ignores_blank_lines(self):
        self.assertEqual(len(parse_batch('\n{"polled_at":"t","ok":true,"arrivals":[]}\n\n')), 1)

    def test_rejects_bad_batches(self):
        for bad in ("", "\n", "not json", "[1]", '{"polled_at":"t"}', '{"arrivals":[]}', '{"polled_at":"t","arrivals":1}'):
            with self.assertRaises(ValueError, msg=bad):
                parse_batch(bad)


if __name__ == "__main__":
    unittest.main()