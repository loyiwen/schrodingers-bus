import json
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from collect import blob_url, combine, download, summarise

MODIFIED = "Fri, 13 Nov 2026 08:00:05 GMT"


class FakeContainer(BaseHTTPRequestHandler):
    """Stands in for a blob container, listing two names per page and refusing requests without the SAS."""

    def do_GET(self):
        path, _, query = self.path.partition("?")
        params = urllib.parse.parse_qs(query)
        if params.get("sig") != ["ok"]:
            return self.reply(403, b"")
        if params.get("comp") == ["list"]:
            names = sorted(name for name in self.server.blobs if name.startswith(params.get("prefix", [""])[0]))
            start = int(params.get("marker", ["0"])[0])
            page = "".join(
                f"<Blob><Name>{name}</Name><Properties><Last-Modified>{MODIFIED}</Last-Modified></Properties></Blob>"
                for name in names[start:start + 2]
            )
            more = f"<NextMarker>{start + 2}</NextMarker>" if start + 2 < len(names) else "<NextMarker />"
            return self.reply(200, f"<EnumerationResults><Blobs>{page}</Blobs>{more}</EnumerationResults>".encode())
        name = urllib.parse.unquote(path.rsplit("/", 1)[-1])
        self.reply(200, self.server.blobs[name]) if name in self.server.blobs else self.reply(404, b"")

    def reply(self, status, body):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # Keeps the test output readable
        pass


def result(batch_id, instance, run, finished="2026-11-13T08:00:04.000Z", **extra):
    """Build a result file's contents, as Function 2 writes them."""
    return {"batch_id": batch_id, "predictions": 100, "instance_id": instance, "instance_run": run,
            "queue_delay_seconds": 1.5, "runtime_seconds": 0.5, "dequeue_count": 1,
            "queued_at": "2026-11-13T08:00:01.000Z", "started_at": "2026-11-13T08:00:03.000Z",
            "finished_at": finished, **extra}


def sent(batch_id, status=202, at="2026-11-13T08:00:00.000Z"):
    """Build one line of a send log, as send.py writes it."""
    return json.dumps({"batch_id": batch_id, "sent_at": at, "late_seconds": 0, "status": status,
                       "error": None, "bytes": 10, "seconds": 0.8})


class Download(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeContainer)
        self.server.blobs = {f"r1-0000{n}-25.json": json.dumps({"n": n}).encode() for n in range(5)}
        self.server.blobs["r10-00000-25.json"] = b"{}"  # Another run, whose name starts the same way
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/results?sv=1&sig=ok"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_blob_url_keeps_the_sas(self):
        self.assertEqual(blob_url("https://a/results?sig=ok", "r1-1.json"), "https://a/results/r1-1.json?sig=ok")
        self.assertEqual(blob_url("https://a/results?sig=ok", comp="list"), "https://a/results?sig=ok&comp=list")

    def test_downloads_every_page_of_one_run_only(self):
        written = download(self.url, "r1", self.root / "r1")
        self.assertEqual(len(written), 5)
        self.assertEqual(written["r1-00000-25.json"], "2026-11-13T08:00:05.000Z")
        self.assertEqual(json.loads((self.root / "r1" / "r1-00004-25.json").read_text())["n"], 4)
        self.assertFalse((self.root / "r1" / "r10-00000-25.json").exists())

    def test_files_already_downloaded_are_kept(self):
        (self.root / "r1").mkdir()
        (self.root / "r1" / "r1-00000-25.json").write_text("kept")
        download(self.url, "r1", self.root / "r1")
        self.assertEqual((self.root / "r1" / "r1-00000-25.json").read_text(), "kept")


class Combine(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.results = self.root / "r1"
        self.results.mkdir()

    def run_with(self, log_lines, results):
        log = self.root / "r1.jsonl"
        log.write_text("\n".join(log_lines) + "\n")
        for item in results:
            (self.results / f"{item['batch_id']}.json").write_text(json.dumps(item))
        (self.results / "written_at.json").write_text(json.dumps({f"{item['batch_id']}.json": "w" for item in results}))
        return combine(log, self.results)

    def test_joins_each_batch_to_its_result(self):
        rows = self.run_with([sent("a"), sent("b")], [result("a", "i1", 1), result("b", "i1", 2)])
        self.assertEqual([row["end_to_end_seconds"] for row in rows], [4.0, 4.0])
        self.assertEqual(rows[0]["written_at"], "w")

    def test_summary_counts_instances_cold_starts_and_gaps(self):
        rows = self.run_with(
            [sent("a"), sent("b"), sent("c"), sent("d", status=401)],
            [result("a", "i1", 1), result("b", "i2", 1, finished="2026-11-13T08:00:10.000Z", dequeue_count=2)],
        )
        summary = summarise(rows)
        self.assertEqual((summary["sent"], summary["accepted"], summary["results"]), (4, 3, 2))
        self.assertEqual(summary["missing"], 1)
        self.assertEqual((summary["instances"], summary["cold_starts"], summary["retried"]), (2, 2, 1))
        self.assertEqual(summary["makespan_seconds"], 10)
        self.assertEqual(summary["p95_end_to_end_seconds"], 10)

    def test_older_results_without_times_still_count(self):
        old = result("a", "i1", 1)
        del old["finished_at"]
        rows = self.run_with([sent("a")], [old])
        summary = summarise(rows)
        self.assertEqual(summary["results"], 1)
        self.assertIsNone(summary["median_end_to_end_seconds"])
        self.assertNotIn("makespan_seconds", summary)


if __name__ == "__main__":
    unittest.main()
