"""The mirrored-artifact routes: listing, serving, and path safety.

Three properties, all of which have bitten real dashboards:

  * the listing is walked from disk, so a run whose directory `gc` has
    pruned reports no files rather than offering links that 404;
  * `..` and an absolute path cannot read anything outside the run's own
    mirror directory -- the relpath arrives from a bench host, so it is
    untrusted input however well-behaved the agent is;
  * only text artifacts are served inline. HTML and SVG from a bench host
    would otherwise run script on the dashboard's origin, where the trigger
    token lives in localStorage.
"""

import contextlib
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from src.db import pool, queries  # noqa: E402
from src.inventory import load_inventory  # noqa: E402
from src.settings import Settings  # noqa: E402


class _Conn:
    def rollback(self):
        pass

    def commit(self):
        pass


@contextlib.contextmanager
def _fake_connection(*args, **kwargs):
    yield _Conn()


RUN = {
    "run_id": 9, "device_id": "iq-9075-evk-01", "target": "iq-9075-evk",
    "agent_url": "http://bench-win-01:8765", "host": "bench-win-01",
    "status": "success", "exit_code": 0, "failure_stage": None,
    "error_class": None, "error_message": None,
    "build_number": 2471, "build_folder": "Build_master_2471",
    "build_path": "//share/Yocto/Build_master_2471/performance",
    "share_root": "//share/Yocto",
    "queued_at": None, "started_at": None, "finished_at": None,
    "trigger_source": "cron", "triggered_by": None, "reflashed": True,
    "boots_recorded": 6, "boots_expected": 6,
    "stages_run": ["flash", "capture"], "stages_requested": [],
    "agent_job_id": "9", "agent_boot_logs_dir": "C:/bench/Boot-Logs",
    "cancel_requested": False, "parent_run_id": None, "readopted": False,
    "acknowledged_at": None, "total_multiuser_s": 12.481,
    "artifact_dir": "", "mirror": {},
}


class ArtifactRouteTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="bootbench-web-"))
        self.mirror = self.root / "runs" / "9"
        (self.mirror / "Boot-Logs/t/b/Logs-default-1").mkdir(parents=True)
        (self.mirror / "runner.log").write_bytes(b"flashing\ndone\n")
        (self.mirror / "status.json").write_text('{"schema_version": 1}')
        (self.mirror / "Boot-Logs/t/b/Logs-default-1/dmesg.txt").write_text("x")
        (self.root / "secret.txt").write_text("not yours")

        self._saved = (pool.connection, queries.run_detail,
                       queries.boots_for_run, queries.all_runs,
                       queries.run_counts)
        pool.connection = _fake_connection
        queries.run_detail = lambda conn, run_id: dict(RUN) if run_id == 9 else None
        queries.boots_for_run = lambda conn, run_id: []
        queries.all_runs = lambda conn, **kwargs: [dict(RUN)]
        queries.run_counts = lambda conn: {"success": 1}

        from src.web.app import create_app
        settings = Settings(artifact_root=str(self.root))
        inventory = load_inventory(HERE.parent / "config" / "devices.yaml.example")
        self.client = create_app(settings=settings,
                                 inventory=inventory).test_client()

    def tearDown(self):
        (pool.connection, queries.run_detail, queries.boots_for_run,
         queries.all_runs, queries.run_counts) = self._saved

    def get(self, url):
        return self.client.get(url, headers={"Accept": "text/html"})

    # -- listing ---------------------------------------------------------
    def test_run_page_lists_every_mirrored_file(self):
        body = self.get("/runs/9").get_data(as_text=True)
        for name in ("runner.log", "status.json", "dmesg.txt"):
            self.assertIn(name, body)
        # And the provenance the dashboard exists to show.
        self.assertIn("2471", body)
        self.assertIn("//share/Yocto", body)

    def test_pruned_run_offers_no_links(self):
        """`gc` unlinks the directory but keeps the row. The page must say
        there are no files rather than link to them."""
        for path in sorted(self.mirror.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        self.mirror.rmdir()
        body = self.get("/runs/9").get_data(as_text=True)
        self.assertIn("Nothing mirrored for this run", body)

    def test_inventory_page_counts_files(self):
        body = self.get("/runs").get_data(as_text=True)
        self.assertIn("iq-9075-evk-01", body)
        self.assertIn("2471", body)

    # -- serving ---------------------------------------------------------
    def test_text_is_served_inline(self):
        response = self.client.get("/runs/9/files/runner.log")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"flashing\ndone\n")
        self.assertTrue(response.mimetype.startswith("text/plain"))

    def test_nested_relpath(self):
        response = self.client.get(
            "/runs/9/files/Boot-Logs/t/b/Logs-default-1/dmesg.txt")
        self.assertEqual(response.status_code, 200)

    def test_unknown_kind_downloads_rather_than_renders(self):
        (self.mirror / "bootchart-overview-x.html").write_text("<b>hi</b>")
        response = self.client.get("/runs/9/files/bootchart-overview-x.html")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment",
                      response.headers.get("Content-Disposition", ""))

    def test_traversal_is_refused(self):
        for relpath in ("../secret.txt", "Boot-Logs/../../secret.txt",
                        "..%2f..%2fsecret.txt"):
            with self.subTest(relpath=relpath):
                response = self.client.get(f"/runs/9/files/{relpath}")
                self.assertNotEqual(response.status_code, 200)
                self.assertNotIn(b"not yours", response.data)

    def test_missing_file_is_404(self):
        self.assertEqual(
            self.client.get("/runs/9/files/nope.txt").status_code, 404)

    def test_unmirrored_run_is_404(self):
        self.assertEqual(
            self.client.get("/runs/404/files/runner.log").status_code, 404)


if __name__ == "__main__":
    unittest.main()
