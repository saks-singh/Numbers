"""Agent protocol tests. No hardware, no Windows required.

The HTTP and job-bookkeeping layers are OS-agnostic; only the TAC and
session probes are Windows-specific, and those are exercised separately.
Every test here drives the real agent.py over a real socket against a stub
bootbench, so what passes is the production code path.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
# ../../client: the agent ships from the same directory as the copy of
# bootbench.py it drives. Imported as a loose file rather than a package
# because that is how it is deployed -- copy the directory, run the script,
# install nothing.
AGENT_DIR = HERE.parents[1] / "client"
STUB = HERE / "fixtures" / "stub_bootbench.py"

sys.path.insert(0, str(AGENT_DIR))
import agent as ag  # noqa: E402

TOKEN = "unit-test-token"


def wait_until(predicate, timeout=60.0, interval=0.25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class AgentTestCase(unittest.TestCase):
    """Boots a real agent on an ephemeral port against a stub bootbench."""

    stub_env = {}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bootbench-agent-test-"))
        self.artifacts = self.tmp / "artifacts"
        self.config_path = self.tmp / "agentconfig.json"
        self.config_path.write_text(json.dumps({
            "bind": "127.0.0.1",
            "port": 0,
            "python": json.dumps(sys.executable)[1:-1],
            "bootbench": str(STUB),
            "share_root": r"\\share\Yocto",
            "artifact_root": str(self.artifacts),
            "devices": {
                "dev-01": {
                    "target": "iq-9075-evk",
                    "com_port": "COM7",
                    "tac_port": "VTP8",
                    "adb_serial": "serial-a",
                },
                "dev-02": {"target": "other-evk"},
            },
        }), encoding="utf-8")

        os.environ["BOOTBENCH_AGENT_TOKEN"] = TOKEN
        for key, value in self.stub_env.items():
            os.environ[key] = str(value)

        self.config = ag.AgentConfig.load(self.config_path)
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.store = ag.JobStore(self.config)
        self.store.adopt_existing()

        ag.AgentHandler.config = self.config
        ag.AgentHandler.store = self.store
        ag.AgentHandler.health_cache = {}

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), ag.AgentHandler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        for key in self.stub_env:
            os.environ.pop(key, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers --------------------------------------------------------
    def call(self, path, method="GET", body=None, token=TOKEN, raw=False):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if token is not None:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = resp.read()
                if raw:
                    return resp.status, payload
                return resp.status, json.loads(payload)
        except urllib.error.HTTPError as e:
            payload = e.read()
            if raw:
                return e.code, payload
            try:
                return e.code, json.loads(payload)
            except json.JSONDecodeError:
                return e.code, {"raw": payload.decode(errors="replace")}

    def submit(self, job_id="1", device_id="dev-01", **kwargs):
        body = {"job_id": job_id, "device_id": device_id,
                "stages": "all", "num_boots": 2, **kwargs}
        return self.call("/jobs", "POST", body)

    def await_terminal(self, job_id, timeout=90):
        def done():
            _, job = self.call(f"/jobs/{job_id}")
            return job.get("terminal")
        self.assertTrue(
            wait_until(done, timeout), f"job {job_id} never reached a terminal state"
        )
        return self.call(f"/jobs/{job_id}")[1]


class TestAuth(AgentTestCase):
    def test_missing_header_is_401(self):
        status, body = self.call("/healthz", token=None)
        self.assertEqual(status, 401)
        self.assertIn("token", body["error"])

    def test_wrong_token_is_401(self):
        self.assertEqual(self.call("/healthz", token="nope")[0], 401)

    def test_token_prefix_is_not_accepted(self):
        # A constant-time compare must still reject a correct prefix.
        self.assertEqual(self.call("/healthz", token=TOKEN[:-1])[0], 401)

    def test_good_token_is_200(self):
        self.assertEqual(self.call("/healthz")[0], 200)

    def test_unknown_route_is_404(self):
        self.assertEqual(self.call("/nope")[0], 404)


class TestHealthz(AgentTestCase):
    def test_reports_capabilities(self):
        _, body = self.call("/healthz")
        self.assertEqual(body["agent_version"], ag.AGENT_VERSION)
        self.assertEqual(sorted(body["devices"]), ["dev-01", "dev-02"])
        self.assertIsNone(body["busy"])
        self.assertEqual(body["pid"], os.getpid())
        for key in ("session_id", "session_interactive", "tac_device_count",
                    "tac_error", "pcat_present", "artifact_root_free_gb"):
            self.assertIn(key, body)

    def test_free_space_resolves_for_a_missing_directory(self):
        # disk_usage raises on a path that does not exist yet; "unknown" would
        # be a misleading answer when the volume is perfectly readable.
        missing = self.artifacts / "not" / "created" / "yet"
        self.assertIsNotNone(ag._free_gb(missing))

    def test_tac_probe_is_skipped_while_busy(self):
        self.submit(job_id="busy-probe", resume_pull=False)
        _, body = self.call("/healthz")
        if body["busy"]:
            self.assertTrue(body["tac_probe_skipped_busy"])
        self.await_terminal("busy-probe")


class TestLatestBuild(AgentTestCase):
    def test_returns_build_metadata(self):
        status, body = self.call("/latest-build?device_id=dev-01")
        self.assertEqual(status, 200)
        self.assertEqual(body["build_number"], 2471)
        self.assertTrue(body["target_image_ready"])
        self.assertEqual(body["target"], "iq-9075-evk")

    def test_unknown_device_is_404(self):
        status, body = self.call("/latest-build?device_id=ghost")
        self.assertEqual(status, 404)
        self.assertIn("dev-01", body["devices"])


class TestJobs(AgentTestCase):
    def test_submit_returns_202_and_runs(self):
        status, body = self.submit(job_id="j1")
        self.assertEqual(status, 202)
        self.assertTrue(body["created"])
        job = self.await_terminal("j1")
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["exit_code"], 0)
        self.assertEqual(job["status"]["boots_recorded"], 4)
        self.assertFalse(job["status"]["partial"])

    def test_resubmitting_a_known_job_id_does_not_start_a_second_run(self):
        # This is what makes a coordinator-side retry-on-timeout safe.
        _, first = self.submit(job_id="j2")
        status, second = self.submit(job_id="j2")
        self.assertEqual(status, 200)
        self.assertFalse(second["created"])
        self.assertEqual(first["started_utc"], second["started_utc"])
        self.await_terminal("j2")

    def test_busy_device_is_409_naming_the_inflight_job(self):
        self.submit(job_id="j3")
        status, body = self.submit(job_id="j4")
        self.assertEqual(status, 409)
        self.assertEqual(body["job_id"], "j3")
        self.await_terminal("j3")

    def test_a_second_device_is_not_blocked(self):
        self.submit(job_id="j5", device_id="dev-01")
        status, _ = self.submit(job_id="j6", device_id="dev-02")
        self.assertIn(status, (200, 202))
        self.await_terminal("j5")
        self.await_terminal("j6")

    def test_unknown_device_is_404(self):
        self.assertEqual(self.submit(job_id="j7", device_id="ghost")[0], 404)

    def test_bad_job_id_is_400(self):
        for bad in ("", "has space", "../escape", "x" * 80):
            self.assertEqual(
                self.submit(job_id=bad)[0], 400, f"accepted job_id {bad!r}"
            )

    def test_unknown_stage_is_400(self):
        self.assertEqual(self.submit(job_id="j8", stages="teleport")[0], 400)

    def test_job_list_is_newest_first(self):
        self.submit(job_id="a1")
        self.await_terminal("a1")
        self.submit(job_id="a2")
        self.await_terminal("a2")
        _, body = self.call("/jobs")
        ids = [j["job_id"] for j in body["jobs"]]
        self.assertEqual(ids[:2], ["a2", "a1"])


class TestLogTail(AgentTestCase):
    def test_byte_offsets_are_contiguous(self):
        self.submit(job_id="t1")
        self.await_terminal("t1")

        _, first = self.call("/jobs/t1/log?offset=0")
        self.assertGreater(first["size"], 0)
        self.assertEqual(first["offset"], len(first["chunk"].encode()))
        self.assertTrue(first["eof"])

        # Resuming mid-stream must neither repeat nor skip bytes.
        _, second = self.call("/jobs/t1/log?offset=40")
        self.assertEqual(second["offset"], first["offset"])
        self.assertTrue(first["chunk"].endswith(second["chunk"]))

    def test_offset_past_eof_is_clamped(self):
        self.submit(job_id="t2")
        self.await_terminal("t2")
        _, body = self.call("/jobs/t2/log?offset=99999999")
        self.assertEqual(body["chunk"], "")
        self.assertEqual(body["offset"], body["size"])

    def test_non_integer_offset_is_400(self):
        self.submit(job_id="t3")
        self.await_terminal("t3")
        self.assertEqual(self.call("/jobs/t3/log?offset=abc")[0], 400)

    def test_log_records_the_argv(self):
        self.submit(job_id="t4")
        self.await_terminal("t4")
        _, body = self.call("/jobs/t4/log?offset=0")
        self.assertIn("# argv:", body["chunk"])


class TestArtifacts(AgentTestCase):
    def setUp(self):
        super().setUp()
        self.submit(job_id="art")
        self.await_terminal("art")
        _, self.manifest = self.call("/jobs/art/artifacts")

    def test_manifest_classifies_every_file(self):
        kinds = {}
        for entry in self.manifest["entries"]:
            kinds.setdefault(entry["kind"], []).append(entry["relpath"])
        self.assertEqual(len(kinds["status"]), 1)
        self.assertEqual(len(kinds["runner_log"]), 1)
        self.assertEqual(len(kinds["chart_json"]), 1)
        self.assertEqual(len(kinds["chart_html"]), 1)
        # 4 boots (2 phases x 2) x 7 parse-relevant files
        self.assertEqual(len(kinds["boot_parse"]), 28)
        # 4 boots x 4 extras (svg, proc_config, Boot_Trace, lsmod)
        self.assertEqual(len(kinds["boot_extra"]), 16)
        self.assertEqual(len(kinds["boot_txt"]), 4)

    def test_only_parse_relevant_files_are_boot_parse(self):
        parse = {
            Path(e["relpath"]).name
            for e in self.manifest["entries"] if e["kind"] == "boot_parse"
        }
        self.assertEqual(parse, set(ag.PARSE_FILES))

    def test_every_artifact_downloads_and_matches_its_checksum(self):
        import hashlib
        for entry in self.manifest["entries"]:
            quoted = urllib.parse.quote(entry["relpath"])
            status, data = self.call(f"/jobs/art/artifacts/{quoted}", raw=True)
            self.assertEqual(status, 200, entry["relpath"])
            self.assertEqual(len(data), entry["size"], entry["relpath"])
            self.assertEqual(
                hashlib.sha256(data).hexdigest(), entry["sha256"], entry["relpath"]
            )

    def test_path_traversal_is_refused(self):
        # Only manifest-listed relpaths are servable, which makes traversal
        # structurally impossible rather than filtered.
        for attack in (
            "../../../../etc/passwd",
            "..%2f..%2f..%2fetc%2fpasswd",
            "Boot-Logs/../../../.bootbench.lock",
            "Boot-Charts/..%2f..%2fjobs%2fart%2fjob.json",
            "status.json/../../job.json",
        ):
            status, _ = self.call(f"/jobs/art/artifacts/{attack}", raw=False)
            self.assertEqual(status, 404, f"served {attack!r}")

    def test_unknown_job_is_404(self):
        self.assertEqual(self.call("/jobs/ghost/artifacts")[0], 404)


class TestCancel(AgentTestCase):
    stub_env = {"STUB_SLOW": "1.5"}

    def test_cancel_stops_at_a_safe_point_and_records_partial(self):
        self.submit(job_id="c1", num_boots=3)
        self.assertTrue(wait_until(
            lambda: self.call("/jobs/c1")[1]["state"] == "running", timeout=30
        ))
        status, body = self.call("/jobs/c1/cancel", "POST")
        self.assertEqual(status, 200)
        self.assertTrue(body["cancelled"])

        job = self.await_terminal("c1")
        self.assertTrue(job["cancel_requested"])
        # Cancelling is not an error: boots already collected are kept, and
        # the run is marked partial rather than failed.
        self.assertEqual(job["exit_code"], 0)
        self.assertLess(job["status"]["boots_recorded"], 6)
        self.assertTrue(job["status"]["partial"])

    def test_cancel_on_a_terminal_job_is_a_noop(self):
        self.submit(job_id="c2", num_boots=1)
        self.await_terminal("c2")
        _, body = self.call("/jobs/c2/cancel", "POST")
        self.assertFalse(body["cancelled"])


class TestFailureIsStillReported(AgentTestCase):
    stub_env = {"STUB_FAIL": "12"}

    def test_failed_job_still_yields_status_and_artifacts(self):
        # A flash failure's log and status document are exactly what you need
        # at 9 a.m., so artifacts must survive a non-zero exit.
        self.submit(job_id="f1")
        job = self.await_terminal("f1")
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["exit_code"], 12)
        self.assertEqual(job["status"]["failure_stage"], "flash")

        _, manifest = self.call("/jobs/f1/artifacts")
        kinds = {e["kind"] for e in manifest["entries"]}
        self.assertIn("status", kinds)
        self.assertIn("runner_log", kinds)


class TestArgvComposition(AgentTestCase):
    def _argv(self, **request):
        job = ag.Job("x", "dev-01", request, self.config.job_dir("dev-01", "x"))
        return ag.build_argv(self.config, "dev-01", request, job)

    def test_no_element_contains_a_shell_operator(self):
        # The no-shell property, pinned by a test rather than by a comment.
        argv = self._argv(stages="all", num_boots=3, boot_timeout=480)
        for element in argv:
            for operator in ("&&", "||", "|", ";", ">", "<", "&", "$(", "`"):
                self.assertNotIn(operator, element, f"{element!r}")

    def test_no_element_contains_a_control_character(self):
        for element in self._argv(stages="all"):
            self.assertFalse(
                any(c in element for c in "\r\n\x00"), repr(element)
            )

    def test_always_non_interactive(self):
        argv = self._argv(stages="all")
        self.assertIn("--yes", argv)
        self.assertIn("--non-interactive", argv)

    def test_device_identity_is_always_pinned(self):
        # Unpinned COM/TAC/adb is how one bench host's capture reaches into
        # another board's live console.
        argv = self._argv(stages="capture")
        for flag, value in (("--target", "iq-9075-evk"), ("--com-port", "COM7"),
                            ("--tac-port", "VTP8"), ("--adb-serial", "serial-a")):
            self.assertIn(flag, argv)
            self.assertEqual(argv[argv.index(flag) + 1], value)

    def test_chart_and_log_dirs_are_per_device_not_per_job(self):
        # Per-job chart dirs would give every run a fresh one-element history
        # and collapse the skill's 30-run report to a single column.
        argv = self._argv(stages="all")
        charts = Path(argv[argv.index("--boot-charts-dir") + 1])
        logs = Path(argv[argv.index("--boot-logs-dir") + 1])
        self.assertEqual(charts, self.config.device_dir("dev-01") / "Boot-Charts")
        self.assertEqual(logs, self.config.device_dir("dev-01") / "Boot-Logs")
        self.assertNotIn("jobs", charts.parts)
        self.assertNotIn("jobs", logs.parts)

    def test_status_cancel_and_lock_paths_are_passed(self):
        argv = self._argv(stages="all")
        for flag in ("--json-status", "--cancel-file", "--lock-file"):
            self.assertIn(flag, argv)

    def test_latest_build_is_device_free(self):
        argv = self._argv(stages="latest-build")
        self.assertIn("--json", argv)
        for flag in ("--com-port", "--tac-port", "--adb-serial", "--yes"):
            self.assertNotIn(flag, argv)

    def test_optional_flags_are_omitted_when_unset(self):
        argv = self._argv(stages="capture")
        for flag in ("--resume-pull", "--revert-debug-cmdline", "--recover",
                     "--build-path", "--num-boots"):
            self.assertNotIn(flag, argv)

    def test_optional_flags_appear_when_set(self):
        argv = self._argv(stages="capture", resume_pull=True,
                          revert_debug_cmdline=True, num_boots=5)
        self.assertIn("--resume-pull", argv)
        self.assertIn("--revert-debug-cmdline", argv)
        self.assertEqual(argv[argv.index("--num-boots") + 1], "5")

    def test_unknown_stage_raises(self):
        with self.assertRaises(ValueError):
            self._argv(stages="rm -rf /")


class TestConfigValidation(unittest.TestCase):
    def _load(self, **overrides):
        base = {
            "bootbench": "/tmp/bootbench.py",
            "share_root": r"\\share\Yocto",
            "artifact_root": "/tmp/artifacts",
            "devices": {"d1": {"target": "t1"}},
        }
        base.update(overrides)
        tmp = Path(tempfile.mkdtemp()) / "c.json"
        tmp.write_text(json.dumps(base), encoding="utf-8")
        return ag.AgentConfig.load(tmp)

    def test_minimal_config_loads(self):
        self.assertEqual(sorted(self._load().devices), ["d1"])

    def test_onedrive_artifact_root_is_refused(self):
        # Sync storms and file locking make adb pull fail mid-write in a way
        # that looks like an adb bug. Fail at startup, not at 1 a.m.
        with self.assertRaises(ag.ConfigError) as ctx:
            self._load(artifact_root="C:/Users/x/OneDrive - Corp/artifacts")
        self.assertIn("OneDrive", str(ctx.exception))

    def test_missing_required_keys_are_refused(self):
        for key in ("bootbench", "share_root", "artifact_root"):
            with self.assertRaises(ag.ConfigError, msg=key):
                self._load(**{key: None})

    def test_no_devices_is_refused(self):
        with self.assertRaises(ag.ConfigError):
            self._load(devices={})

    def test_device_without_target_is_refused(self):
        with self.assertRaises(ag.ConfigError):
            self._load(devices={"d1": {"com_port": "COM7"}})

    def test_bad_device_id_is_refused(self):
        for bad in ("Dev-01", "-dev", "dev 01", "dev/01"):
            with self.assertRaises(ag.ConfigError, msg=bad):
                self._load(devices={bad: {"target": "t1"}})

    def test_control_characters_in_argv_values_are_refused(self):
        # There is no shell, so metacharacters are harmless -- but a newline
        # would corrupt the argv itself, and that is always a config bug.
        with self.assertRaises(ag.ConfigError):
            self._load(devices={"d1": {"target": "t1\nmalicious"}})
        with self.assertRaises(ag.ConfigError):
            self._load(share_root="\\\\share\\Yocto\x00")

    def test_missing_file_is_a_clean_error(self):
        with self.assertRaises(ag.ConfigError) as ctx:
            ag.AgentConfig.load(Path("/nonexistent/c.json"))
        self.assertIn("not found", str(ctx.exception))

    def test_malformed_json_is_a_clean_error(self):
        tmp = Path(tempfile.mkdtemp()) / "c.json"
        tmp.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ag.ConfigError) as ctx:
            ag.AgentConfig.load(tmp)
        self.assertIn("valid JSON", str(ctx.exception))


class TestProcessIdentity(unittest.TestCase):
    def test_self_is_alive_and_identical(self):
        me = os.getpid()
        self.assertTrue(ag._pid_alive(me))
        self.assertTrue(ag._is_same_process(me, ag._proc_start_time(me)))

    def test_pid_reuse_is_detected(self):
        # A pid alone is not an identity: without this check, an agent that
        # was down long enough could adopt an unrelated process, wait on it
        # forever, and hold the device busy with a job that is really dead.
        me = os.getpid()
        if ag._proc_start_time(me) is None:
            self.skipTest("process start time unavailable on this platform")
        self.assertFalse(ag._is_same_process(me, 1))

    def test_dead_pid_is_not_alive(self):
        self.assertFalse(ag._pid_alive(10 ** 9))
        self.assertFalse(ag._is_same_process(10 ** 9, None))

    def test_missing_start_time_falls_back_to_liveness(self):
        # job.json from an older agent has no pid_start; refusing to adopt a
        # genuinely running flash would be the worse error.
        self.assertTrue(ag._is_same_process(os.getpid(), None))


class TestRestartAdoption(AgentTestCase):
    """The three adoption branches, driven through JobStore directly because
    they need a job record that outlives an agent process."""

    def _write_orphan_record(self, job_id, pid, with_status, pid_start=None):
        job_dir = self.config.job_dir("dev-01", job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "job.json").write_text(json.dumps({
            "job_id": job_id, "device_id": "dev-01", "request": {"stages": "all"},
            "state": "running", "pid": pid, "pid_start": pid_start,
            "argv": [], "exit_code": None,
            "started_utc": "2026-10-06T01:00:00+00:00",
        }), encoding="utf-8")
        if with_status:
            (job_dir / "status.json").write_text(json.dumps({
                "schema_version": 1, "exit_code": 0, "boots_recorded": 4,
                "boots_expected": 4, "partial": False,
                "ended_utc": "2026-10-06T01:40:00+00:00", "output": {},
            }), encoding="utf-8")
        return job_dir

    def _restart(self):
        store = ag.JobStore(self.config)
        store.adopt_existing()
        return store

    def test_dead_pid_with_status_is_finalized_from_the_status_doc(self):
        self._write_orphan_record("adopt-a", pid=10 ** 9, with_status=True)
        job = self._restart().get("adopt-a")
        self.assertEqual(job.state, "done")
        self.assertEqual(job.exit_code, 0)
        self.assertEqual(job.ended_utc, "2026-10-06T01:40:00+00:00")

    def test_dead_pid_without_status_is_orphaned_with_a_reason(self):
        self._write_orphan_record("adopt-b", pid=10 ** 9, with_status=False)
        job = self._restart().get("adopt-b")
        self.assertEqual(job.state, "orphaned")
        self.assertIn("status document", job.error)

    def test_orphaned_job_does_not_hold_the_device_busy(self):
        self._write_orphan_record("adopt-c", pid=10 ** 9, with_status=False)
        store = self._restart()
        self.assertIsNone(store.busy_device("dev-01"))

    def test_live_pid_is_readopted_and_holds_the_device(self):
        # A long-lived child stands in for an in-flight bootbench.py, which is
        # the case that must survive an agent restart: a 40-minute flash
        # cannot be abandoned just because the coordinator's agent bounced.
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            self._write_orphan_record(
                "adopt-d", pid=proc.pid, with_status=False,
                pid_start=ag._proc_start_time(proc.pid),
            )
            store = self._restart()
            job = store.get("adopt-d")
            self.assertEqual(job.state, "running")
            self.assertEqual(store.busy_device("dev-01"), "adopt-d")
        finally:
            proc.kill()
            proc.wait(timeout=10)

    def test_readopted_job_finalizes_when_its_process_exits(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(1)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        job_dir = self._write_orphan_record(
            "adopt-e", pid=proc.pid, with_status=False,
            pid_start=ag._proc_start_time(proc.pid),
        )
        store = self._restart()
        proc.wait(timeout=20)
        # The watcher polls every 5s, so allow a couple of intervals.
        self.assertTrue(wait_until(
            lambda: store.get("adopt-e").state in ag.TERMINAL_STATES, timeout=40
        ))
        self.assertIsNone(store.busy_device("dev-01"))
        self.assertTrue((job_dir / "job.json").is_file())

    def test_a_terminal_record_is_restored_but_not_rerun(self):
        job_dir = self.config.job_dir("dev-01", "adopt-f")
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "job.json").write_text(json.dumps({
            "job_id": "adopt-f", "device_id": "dev-01", "request": {},
            "state": "done", "pid": 1234, "exit_code": 0,
            "started_utc": "2026-10-06T01:00:00+00:00",
        }), encoding="utf-8")
        store = self._restart()
        self.assertEqual(store.get("adopt-f").state, "done")
        self.assertIsNone(store.busy_device("dev-01"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
