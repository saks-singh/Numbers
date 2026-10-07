"""The fake Runner must honor the same contract as the real agent.

A fake that is easier to satisfy than the thing it stands in for is worse
than no fake: every test above it passes while the real path breaks. These
tests assert the four properties the coordinator actually depends on --
idempotent submit, busy rejection, manifest-as-allowlist, and contiguous
byte-offset tailing -- against the same assertions `test_agent.py` makes
of the genuine HTTP agent.
"""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from src.runner.agent_client import AgentError, assess_health  # noqa: E402
from src.runner.base import Runner, is_terminal, job_timeout_seconds  # noqa: E402
from src.runner.local_fake import LocalFakeRunner, classify  # noqa: E402

STATUS = {
    "schema_version": 1, "exit_code": 0, "target": "iq-9075-evk",
    "boots_expected": 2, "boots_recorded": 2, "partial": False,
    "boots": [
        {"phase": "default", "boot_index": 1, "seconds": {"total_multiuser": 12.481}},
        {"phase": "debug", "boot_index": 1, "seconds": {"total_multiuser": 13.902}},
    ],
}


def make_tree() -> Path:
    root = Path(tempfile.mkdtemp(prefix="bootbench-fake-"))
    (root / "status.json").write_text(json.dumps(STATUS), encoding="utf-8")
    (root / "runner.log").write_text(
        "flashing\nboot 1 of 2\nboot 2 of 2\ndone\n", encoding="utf-8"
    )
    charts = root / "Boot-Charts"
    charts.mkdir()
    (charts / "bootchart-data-iq9075evk.json").write_text("{}", encoding="utf-8")
    (charts / "bootchart-overview-iq9075evk.html").write_text("<html>", encoding="utf-8")
    logs = root / "Boot-Logs" / "iq-9075-evk" / "build_2471" / "Logs-default-1"
    logs.mkdir(parents=True)
    for name in ("dmesg.txt", "journalctl.log", "kernel_cmdline.txt",
                 "plot_systemd.svg", "lsmod.txt"):
        (logs / name).write_text("x", encoding="utf-8")
    return root


class TestProtocolConformance(unittest.TestCase):
    def test_fake_satisfies_the_runner_protocol(self):
        self.assertIsInstance(LocalFakeRunner(make_tree()), Runner)

    def test_real_client_satisfies_the_runner_protocol(self):
        from src.runner.agent_client import AgentClient

        self.assertIsInstance(AgentClient("http://h:1", None), Runner)


class TestFakeContract(unittest.TestCase):
    def setUp(self):
        self.root = make_tree()
        self.runner = LocalFakeRunner(self.root)

    def test_submit_is_idempotent_on_job_id(self):
        first = self.runner.submit_job(7, "iq-9075-evk-01", stages="all")
        second = self.runner.submit_job(7, "iq-9075-evk-01", stages="all")
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(second["job"]["started_utc"], first["job"]["started_utc"])

    def test_busy_device_is_refused_with_409_naming_the_job(self):
        slow = LocalFakeRunner(self.root, duration=30)
        slow.submit_job(1, "iq-9075-evk-01")
        with self.assertRaises(AgentError) as ctx:
            slow.submit_job(2, "iq-9075-evk-01")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.payload["job_id"], "1")

    def test_a_terminal_job_releases_the_device(self):
        self.runner.submit_job(1, "iq-9075-evk-01")
        self.assertTrue(is_terminal(self.runner.get_job(1)["job"]["state"]))
        # No 409: the device must be free the instant the job reads terminal,
        # or a coordinator that polls and immediately re-submits gets a
        # spurious conflict.
        self.runner.submit_job(2, "iq-9075-evk-01")

    def test_unknown_device_is_refused(self):
        with self.assertRaises(AgentError) as ctx:
            self.runner.submit_job(1, "not-mine")
        self.assertEqual(ctx.exception.status, 404)

    def test_status_doc_appears_only_once_terminal(self):
        slow = LocalFakeRunner(self.root, duration=30)
        slow.submit_job(1, "iq-9075-evk-01")
        self.assertNotIn("status", slow.get_job(1)["job"])
        slow.cancel(1)
        self.assertEqual(slow.get_job(1)["job"]["status"]["boots_recorded"], 2)

    def test_manifest_checksums_are_real(self):
        self.runner.submit_job(1, "iq-9075-evk-01")
        for entry in self.runner.manifest(1)["artifacts"]:
            blob = self.runner.fetch_artifact(1, entry["relpath"])
            self.assertEqual(hashlib.sha256(blob).hexdigest(), entry["sha256"])
            self.assertEqual(len(blob), entry["size"])

    def test_manifest_is_an_allowlist_not_a_filter(self):
        self.runner.submit_job(1, "iq-9075-evk-01")
        for attack in ("../status.json", "..%2fstatus.json", "/etc/passwd",
                       "Boot-Charts/../../status.json", "nope.txt"):
            with self.assertRaises(AgentError, msg=attack) as ctx:
                self.runner.fetch_artifact(1, attack)
            self.assertEqual(ctx.exception.status, 404)

    def test_tail_offsets_are_contiguous(self):
        self.runner.submit_job(1, "iq-9075-evk-01")
        collected, offset = "", 0
        while True:
            page = self.runner.tail_log(1, offset)
            collected += page["chunk"]
            offset = page["size"]
            if page["eof"] and not page["chunk"]:
                break
        # Compared against the raw bytes, not read_text(): the tail is a byte
        # offset into the file, so newline translation would make this pass
        # while the offsets were wrong.
        expected = (self.root / "runner.log").read_bytes().decode()
        self.assertEqual(collected, expected)

    def test_tail_offset_past_eof_is_clamped(self):
        self.runner.submit_job(1, "iq-9075-evk-01")
        page = self.runner.tail_log(1, 10 ** 9)
        self.assertEqual(page["chunk"], "")
        self.assertEqual(page["offset"], page["size"])


class TestArtifactClassification(unittest.TestCase):
    def test_kinds_match_the_agents_classifier(self):
        cases = {
            "status.json": "status",
            "runner.log": "runner_log",
            "Boot-Charts/bootchart-data-iq9075evk.json": "chart_json",
            "Boot-Charts/bootchart-overview-iq9075evk.html": "chart_html",
            "Boot-Logs/t/b/Logs-default-1/dmesg.txt": "boot_parse",
            "Boot-Logs/t/b/Logs-default-1/kernel_cmdline.txt": "boot_parse",
            "Boot-Logs/t/b/Logs-default-1/plot_systemd.svg": "boot_extra",
            "Boot-Logs/t/b/Logs-default-1/lsmod.txt": "boot_extra",
            "iq-9075-evk_default-1_2026-10-06_0104.txt": "boot_txt",
        }
        for relpath, kind in cases.items():
            self.assertEqual(classify(relpath), kind, relpath)


class TestHealthAssessment(unittest.TestCase):
    """assess_health is the scheduler's step-5 gate. Each reason it can give
    must be reachable, or a real bench-host fault reads as healthy."""

    BASE = {
        "session_interactive": True, "session_id": 1, "tac_device_count": 1,
        "pcat_present": True, "artifact_root_free_gb": 500.0,
    }

    def assertUnhealthy(self, override, needle):
        ok, reasons = assess_health({**self.BASE, **override})
        self.assertFalse(ok)
        self.assertIn(needle, " ".join(reasons).lower())

    def test_a_good_payload_is_healthy(self):
        ok, reasons = assess_health(dict(self.BASE))
        self.assertTrue(ok, reasons)
        self.assertEqual(reasons, [])

    def test_session_zero_is_unhealthy(self):
        # The whole reason the agent is not a Windows Service.
        self.assertUnhealthy(
            {"session_interactive": False, "session_id": 0}, "window station"
        )

    def test_failed_tac_probe_names_the_error(self):
        self.assertUnhealthy(
            {"tac_device_count": None, "tac_error": "class not registered"},
            "class not registered",
        )

    def test_no_board_attached_is_distinct_from_a_failed_probe(self):
        # count 0 with no error means COM works and there is no board. That
        # distinction is what made the Phase 0 probe bug diagnosable.
        self.assertUnhealthy({"tac_device_count": 0}, "usb connection")

    def test_missing_pcat_is_unhealthy(self):
        self.assertUnhealthy({"pcat_present": False}, "pcat.exe")

    def test_low_disk_is_unhealthy(self):
        self.assertUnhealthy({"artifact_root_free_gb": 2.5}, "2.5 gb free")

    def test_an_empty_payload_is_not_silently_healthy(self):
        ok, reasons = assess_health({})
        self.assertFalse(ok)
        self.assertTrue(reasons)


class TestTimeout(unittest.TestCase):
    def test_default_timeout_is_generous_but_bounded(self):
        # A timeout should mean "wedged", not "slow night".
        seconds = job_timeout_seconds(3, 480)
        self.assertEqual(seconds, 2 * 3 * (480 + 240) + 1200 + 600)
        self.assertGreater(seconds / 60, 90)
        self.assertLess(seconds / 3600, 3)

    def test_timeout_scales_with_boots(self):
        self.assertGreater(
            job_timeout_seconds(6, 480), job_timeout_seconds(3, 480)
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
