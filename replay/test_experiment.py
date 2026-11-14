import json
import re
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from collect import APP, code_version
from experiment import plan, run_plan
from test_collect import FakeContainer


VERSION = code_version(APP)  # The fake reports the local version, as a matching deployment would


class FakeAzure(FakeContainer):
    """Accepts batches like Function 1, and writes a result for each straight away, like Function 2."""

    def do_PUT(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        batch_id = self.path.rsplit("/", 1)[-1]
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        self.server.blobs[f"{batch_id}.json"] = json.dumps({
            "batch_id": batch_id, "code_version": VERSION, "host_name": "h1", "instance_id": "i1",
            "instance_run": 1, "queue_delay_seconds": 0.1, "runtime_seconds": 0.1, "dequeue_count": 1,
            "queued_at": now, "started_at": now, "finished_at": now,
        }).encode()
        self.reply(202, json.dumps({"batch_id": batch_id, "code_version": VERSION}).encode())


class Plan(unittest.TestCase):
    def test_has_every_run_once_in_the_same_order_each_time(self):
        runs = plan()
        self.assertEqual(len(runs), 3 * (6 + 4 + 3))
        self.assertEqual(len({item["run"] for item in runs}), len(runs))
        self.assertEqual([item["run"] for item in runs], [item["run"] for item in plan()])

    def test_run_names_are_valid_and_cannot_prefix_each_other(self):
        for item in plan():
            self.assertRegex(item["run"], r"^[A-Za-z0-9_]{1,40}$")
        # collect.py lists results by "<run>-", so burst_10_r1 must not pick up burst_100_r1
        names = [item["run"] for item in plan()]
        self.assertFalse(any(a != b and b.startswith(a + "-") for a in names for b in names))

    def test_rate_runs_send_for_two_minutes(self):
        for item in plan():
            if item["experiment"] == "rate":
                self.assertEqual(item["count"], item["rate"] * 120)


class RunPlan(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.batches = self.root / "batches"
        for name in ("part1-60m", "part2-60m"):
            (self.batches / name).mkdir(parents=True)
            for number in range(3):
                (self.batches / name / f"25-{name}-{number}.jsonl").write_text("{}")
        self.out = self.root / "runs"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAzure)
        self.server.blobs = {}
        port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{port}"
        self.container = f"http://127.0.0.1:{port}/results?sig=ok"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def runs(self):
        folders = ("part1-60m", "part2-60m")
        return [{"run": "burst_4_r1", "experiment": "burst", "level": 4, "repeat": 1, "folders": folders, "count": 4, "rate": None},
                {"run": "rate_20_r1", "experiment": "rate", "level": 20, "repeat": 1, "folders": folders, "count": 5, "rate": 20}]

    def test_sends_collects_and_records_every_run(self):
        run_plan(self.runs(), self.url, "key", self.container, self.batches, self.out, 4, 0, 5)
        records = [json.loads(line) for line in (self.out / "experiments.jsonl").read_text().splitlines()]
        self.assertEqual([record["run"] for record in records], ["burst_4_r1", "rate_20_r1"])
        self.assertEqual([record["summary"]["results"] for record in records], [4, 5])
        self.assertEqual(records[0]["summary"]["code_versions"], [VERSION])
        self.assertTrue(re.match(r"\d{4}-", records[0]["started_at"]))

    def test_a_restart_skips_finished_runs_and_only_collects_half_done_ones(self):
        runs = self.runs()
        run_plan(runs[:1], self.url, "key", self.container, self.batches, self.out, 4, 0, 5)
        # Pretend the second run was sent but the plan stopped before collecting it
        run_plan(runs[1:], self.url, "key", self.container, self.batches, self.out, 4, 0, 5)
        (self.out / "rate_20_r1-summary.json").unlink()
        sent_before = len(self.server.blobs)
        run_plan(runs, self.url, "key", self.container, self.batches, self.out, 4, 0, 5)
        self.assertEqual(len(self.server.blobs), sent_before)  # Nothing was sent again
        records = [json.loads(line) for line in (self.out / "experiments.jsonl").read_text().splitlines()]
        self.assertEqual([record["run"] for record in records], ["burst_4_r1", "rate_20_r1", "rate_20_r1"])
        self.assertIsNone(records[-1]["send"])


if __name__ == "__main__":
    unittest.main()
