import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from send import choose_batches, replay, summarise


class FakeFunction(BaseHTTPRequestHandler):
    """Stands in for Function 1, accepting everything except batches named reject."""

    def do_PUT(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.received.append((self.path, self.headers.get("x-functions-key"), body))
        self.send_response(400 if "reject" in self.path else 202)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # Keeps the test output readable
        pass


class Replay(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for name in ("a", "b", "c"):
            (self.root / f"{name}.jsonl").write_text(f"body-{name}", encoding="utf-8")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeFunction)
        self.server.received = []
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_same_seed_gives_the_same_order(self):
        self.assertEqual(choose_batches([self.root], None, 7), choose_batches([self.root], None, 7))

    def test_smaller_run_is_the_start_of_a_bigger_one(self):
        self.assertEqual(choose_batches([self.root], 2, 7), choose_batches([self.root], 3, 7)[:2])

    def test_asking_for_extra_reuses_files(self):
        self.assertEqual(len(choose_batches([self.root], 7, 7)), 7)

    def test_empty_folder_is_an_error(self):
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaises(ValueError):
            choose_batches([empty], None, 7)

    def test_every_batch_arrives_with_a_unique_id_and_the_key(self):
        paths = choose_batches([self.root], 7, 7)
        results, _ = replay(self.url, "secret", "run1", paths, None, 4)
        self.assertEqual({result["status"] for result in results}, {202})
        self.assertEqual(len({path for path, _, _ in self.server.received}), 7)
        self.assertTrue(all(key == "secret" and b"body-" in body for _, key, body in self.server.received))

    def test_key_is_not_in_the_log(self):
        results, _ = replay(self.url, "secret", "run1", choose_batches([self.root], 3, 7), None, 2)
        self.assertNotIn("secret", str(results))

    def test_refused_batch_is_recorded(self):
        path = self.root / "reject.jsonl"
        path.write_text("x", encoding="utf-8")
        results, elapsed = replay(self.url, None, "run1", [path], None, 1)
        self.assertEqual(results[0]["status"], 400)
        self.assertEqual(summarise(results, elapsed)["failed"], 1)

    def test_unreachable_server_is_recorded(self):
        results, _ = replay("http://127.0.0.1:1", None, "run1", choose_batches([self.root], 1, 7), None, 1)
        self.assertIsNone(results[0]["status"])
        self.assertEqual(results[0]["error"], "URLError")

    def test_rate_spreads_the_requests_out(self):
        _, elapsed = replay(self.url, None, "run1", choose_batches([self.root], 3, 7), 10, 3)
        self.assertGreaterEqual(elapsed, 0.2)


class Summarise(unittest.TestCase):
    def test_figures(self):
        results = [
            {"status": 202, "seconds": number / 10, "bytes": 1_000_000, "late_seconds": 0} for number in range(1, 21)
        ]
        results.append({"status": None, "seconds": 9, "bytes": 1_000_000, "late_seconds": 0.5})
        summary = summarise(results, 10)
        self.assertEqual(summary["accepted"], 20)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["batches_per_second"], 2)
        self.assertEqual(summary["megabytes_per_second"], 2)
        self.assertEqual(summary["median_seconds"], 1.05)
        self.assertEqual(summary["p95_seconds"], 1.9)
        self.assertEqual(summary["max_late_seconds"], 0.5)


if __name__ == "__main__":
    unittest.main()