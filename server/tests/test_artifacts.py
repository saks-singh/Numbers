"""Artifact selection, path safety, and checksum verification.

Three properties matter here and each one has bitten a real system:

  * the manifest is an allowlist, not a filter -- a relpath the agent did
    not declare cannot be fetched, and a relpath containing `..` cannot
    land outside the run directory even if the agent declares it;
  * a corrupt file is skipped, not fatal -- losing one boot `.txt` must not
    cost the database the status document beside it;
  * `boot_extra` is only fetched when asked for, because six boots of trace
    dumps and decompressed kernel configs is tens of megabytes a night.
"""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from src.runner import artifacts  # noqa: E402
from src.runner.agent_client import AgentError  # noqa: E402


class StubRunner:
    """Serves bytes from a dict, counting fetches."""

    def __init__(self, blobs, corrupt=()):
        self.blobs = dict(blobs)
        self.corrupt = set(corrupt)
        self.fetched = []

    def fetch_artifact(self, job_id, relpath):
        self.fetched.append(relpath)
        if relpath not in self.blobs:
            raise AgentError(f"not in manifest: {relpath}", status=404)
        data = self.blobs[relpath]
        if relpath in self.corrupt:
            return data + b"tampered"
        return data


def entry(relpath, data):
    return {
        "relpath": relpath, "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "kind": artifacts.classify(relpath),
    }


class TestClassify(unittest.TestCase):
    def test_known_kinds(self):
        cases = {
            "status.json": "status",
            "runner.log": "runner_log",
            "Boot-Charts/bootchart-data-iq9075evk.json": "chart_json",
            "Boot-Charts/bootchart-overview-iq9075evk.html": "chart_html",
            "Boot-Logs/iq-9075-evk/build/2026-10-06 01-04.txt": "boot_txt",
            "Boot-Logs/iq-9075-evk/build/Logs-default-1/dmesg.txt": "boot_parse",
            "Boot-Logs/iq-9075-evk/build/Logs-default-1/Boot_Trace.txt": "boot_extra",
            "Boot-Logs/iq-9075-evk/build/Logs-default-1/plot_systemd.svg": "boot_extra",
        }
        for relpath, kind in cases.items():
            with self.subTest(relpath=relpath):
                self.assertEqual(artifacts.classify(relpath), kind)

    def test_every_kind_is_declared(self):
        """A `kind` the selector does not know about would be silently
        dropped, so the two sets must stay in step."""
        for relpath in ("status.json", "runner.log",
                        "Boot-Charts/bootchart-data-x.json",
                        "Logs-default-1/dmesg.txt",
                        "Logs-default-1/Boot_Trace.txt"):
            self.assertIn(artifacts.classify(relpath), artifacts.ALL_KINDS)


class TestSelection(unittest.TestCase):
    def setUp(self):
        self.entries = [
            entry("status.json", b"{}"),
            entry("runner.log", b"log"),
            entry("Boot-Logs/t/b/Logs-default-1/dmesg.txt", b"dmesg"),
            entry("Boot-Logs/t/b/Logs-default-1/Boot_Trace.txt", b"x" * 900),
            entry("Boot-Logs/t/b/Logs-default-1/plot_systemd.svg", b"y" * 900),
        ]

    def test_extras_excluded_by_default(self):
        chosen = {e["relpath"] for e in artifacts.select(self.entries)}
        self.assertIn("status.json", chosen)
        self.assertIn("Boot-Logs/t/b/Logs-default-1/dmesg.txt", chosen)
        self.assertNotIn("Boot-Logs/t/b/Logs-default-1/Boot_Trace.txt", chosen)

    def test_extras_included_on_request(self):
        chosen = {e["relpath"]
                  for e in artifacts.select(self.entries, fetch_full_logs=True)}
        self.assertIn("Boot-Logs/t/b/Logs-default-1/Boot_Trace.txt", chosen)
        self.assertEqual(len(chosen), len(self.entries))

    def test_smallest_first(self):
        """status.json must arrive before a 40 MB trace: a run whose fetch is
        interrupted should still be ingestable."""
        sizes = [e["size"] for e in artifacts.select(self.entries,
                                                     fetch_full_logs=True)]
        self.assertEqual(sizes, sorted(sizes))


class TestSafeDestination(unittest.TestCase):
    def setUp(self):
        self.dest = Path(tempfile.mkdtemp(prefix="bootbench-art-"))

    def test_nested_relpath_is_allowed(self):
        path = artifacts.safe_destination(self.dest, "a/b/c.txt")
        self.assertEqual(path, self.dest / "a" / "b" / "c.txt")

    def test_traversal_is_refused(self):
        for relpath in ("../escape.txt", "a/../../escape.txt",
                        "/etc/passwd", "C:/Windows/system32/x.dll",
                        "", ".", "a/../.."):
            with self.subTest(relpath=relpath):
                with self.assertRaises(ValueError):
                    artifacts.safe_destination(self.dest, relpath)


class TestMirror(unittest.TestCase):
    def setUp(self):
        self.dest = Path(tempfile.mkdtemp(prefix="bootbench-mirror-"))
        self.status = json.dumps({"schema_version": 1}).encode()
        self.blobs = {
            "status.json": self.status,
            "runner.log": b"flashing\ndone\n",
            "Boot-Logs/t/b/Logs-default-1/dmesg.txt": b"dmesg bytes",
        }
        self.entries = [entry(k, v) for k, v in self.blobs.items()]

    def test_writes_and_verifies(self):
        runner = StubRunner(self.blobs)
        result = artifacts.mirror(runner, 42, self.entries, self.dest)

        self.assertEqual(len(result["written"]), 3)
        self.assertEqual(result["corrupt"], [])
        self.assertEqual(result["bytes"], sum(len(v) for v in self.blobs.values()))
        self.assertEqual((self.dest / "status.json").read_bytes(), self.status)
        self.assertTrue(
            (self.dest / "Boot-Logs/t/b/Logs-default-1/dmesg.txt").is_file())

    def test_checksum_mismatch_is_skipped_not_fatal(self):
        runner = StubRunner(self.blobs, corrupt={"runner.log"})
        result = artifacts.mirror(runner, 42, self.entries, self.dest)

        self.assertEqual(result["corrupt"], ["runner.log"])
        self.assertEqual(len(result["written"]), 2)
        # The status document -- the thing ingestion depends on -- still
        # landed. That is the whole point of not raising.
        self.assertTrue((self.dest / "status.json").is_file())

    def test_missing_artifact_is_skipped(self):
        entries = self.entries + [entry("gone.txt", b"never served")]
        runner = StubRunner(self.blobs)
        result = artifacts.mirror(runner, 42, entries, self.dest)

        self.assertEqual(len(result["written"]), 3)
        self.assertIn("gone.txt", result["skipped"])

    def test_traversal_entry_cannot_escape(self):
        evil = entry("../escaped.txt", b"nope")
        runner = StubRunner({"../escaped.txt": b"nope"})
        result = artifacts.mirror(runner, 42, [evil], self.dest)

        self.assertEqual(result["written"], [])
        self.assertFalse((self.dest.parent / "escaped.txt").exists())

    def test_find_recovers_the_status_document(self):
        runner = StubRunner(self.blobs)
        artifacts.mirror(runner, 42, self.entries, self.dest)
        found = artifacts.find(self.dest, "status.json")
        self.assertIsNotNone(found)
        self.assertEqual(json.loads(found.read_text())["schema_version"], 1)

    def test_find_returns_none_when_absent(self):
        self.assertIsNone(artifacts.find(self.dest, "status.json"))


if __name__ == "__main__":
    unittest.main()
