"""Inventory loading and validation. No hardware, no database."""

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from src.inventory import (  # noqa: E402
    Device, InventoryError, load_inventory, slugify,
)
from src.utils import config_loader  # noqa: E402

ONE_DEVICE = """
defaults:
  agent_url: "http://bench-win-01:8765"
  num_boots: 3
  boot_timeout: 480
  schedule: "0 1 * * *"
  stages: "all"
  fetch_full_logs: false
  max_retries: 1

devices:
  iq-9075-evk-01:
    enabled: true
    target: iq-9075-evk
    com_port: COM7
    tac_port: VTP8
    adb_serial: "1a2b3c4d5e"
    notes: "lab rack 3, slot 2"
"""

# The literal requirement: a second device is a YAML block and nothing else.
TWO_DEVICES = ONE_DEVICE + """
  iq-8275-evk-01:
    enabled: true
    target: iq-8275-evk
    agent_url: "http://bench-win-02:8765"
    num_boots: 1
"""


def write(content):
    path = Path(tempfile.mkdtemp(prefix="bootbench-inv-")) / "devices.yaml"
    path.write_text(content, encoding="utf-8")
    return path


class TestHappyPath(unittest.TestCase):
    def test_single_device_loads_with_defaults_applied(self):
        inv = load_inventory(write(ONE_DEVICE))
        self.assertEqual(len(inv), 1)
        device = inv["iq-9075-evk-01"]
        self.assertIsInstance(device, Device)
        self.assertEqual(device.target, "iq-9075-evk")
        self.assertEqual(device.agent_url, "http://bench-win-01:8765")
        self.assertEqual(device.num_boots, 3)
        self.assertEqual(device.boot_timeout, 480)
        self.assertEqual(device.schedule, "0 1 * * *")
        self.assertTrue(device.enabled)
        self.assertFalse(device.fetch_full_logs)

    def test_adding_a_device_requires_no_code_change(self):
        inv = load_inventory(write(TWO_DEVICES))
        self.assertEqual(len(inv), 2)
        self.assertEqual(
            sorted(d.device_id for d in inv), ["iq-8275-evk-01", "iq-9075-evk-01"]
        )

    def test_per_device_values_override_defaults(self):
        inv = load_inventory(write(TWO_DEVICES))
        self.assertEqual(inv["iq-8275-evk-01"].num_boots, 1)
        self.assertEqual(inv["iq-8275-evk-01"].agent_url, "http://bench-win-02:8765")
        self.assertEqual(inv["iq-9075-evk-01"].num_boots, 3)

    def test_derived_properties(self):
        device = load_inventory(write(ONE_DEVICE))["iq-9075-evk-01"]
        self.assertEqual(device.slug, "iq9075evk")
        self.assertEqual(device.agent_host, "bench-win-01:8765")
        # num_boots default-cmdline boots plus num_boots debug-cmdline boots.
        self.assertEqual(device.boots_expected, 6)
        self.assertTrue(device.is_fully_pinned)

    def test_slug_matches_the_skill(self):
        # The slug selects bootchart-data-<slug>.json, so it must agree with
        # bootbench.py's own _slugify or the coordinator reads the wrong file.
        self.assertEqual(slugify("iq-9075-evk"), "iq9075evk")
        self.assertEqual(slugify("some_target-name"), "sometargetname")

    def test_disabled_devices_load_but_are_not_enabled(self):
        inv = load_inventory(write(ONE_DEVICE.replace("enabled: true", "enabled: false")))
        self.assertEqual(len(inv), 1)
        self.assertEqual(inv.enabled, [])

    def test_trailing_slash_is_stripped_from_agent_url(self):
        inv = load_inventory(write(
            ONE_DEVICE.replace('"http://bench-win-01:8765"', '"http://bench-win-01:8765/"')
        ))
        self.assertEqual(inv["iq-9075-evk-01"].agent_url, "http://bench-win-01:8765")


class TestValidation(unittest.TestCase):
    def assertRejects(self, content, needle):
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(write(content))
        joined = " ".join(ctx.exception.problems).lower()
        self.assertIn(needle.lower(), joined, ctx.exception.problems)

    def test_rule1_target_is_required(self):
        self.assertRejects(
            ONE_DEVICE.replace("    target: iq-9075-evk\n", ""), "'target' is required"
        )

    def test_rule1_agent_url_is_required(self):
        self.assertRejects(
            ONE_DEVICE.replace('  agent_url: "http://bench-win-01:8765"\n', ""),
            "'agent_url' is required",
        )

    def test_rule1_device_id_must_be_a_slug(self):
        for bad in ("IQ-9075-EVK", "-leading", "has space", "has/slash"):
            self.assertRejects(
                ONE_DEVICE.replace("  iq-9075-evk-01:", f"  {bad}:"), "must match"
            )

    def test_rule2_schedule_must_be_valid_cron(self):
        try:
            import croniter  # noqa: F401
        except ImportError:
            self.skipTest("croniter not installed")
        self.assertRejects(
            ONE_DEVICE.replace('"0 1 * * *"', '"every night please"'),
            "not a valid cron expression",
        )

    def test_rule3_agent_url_must_be_http(self):
        for bad in ("ssh://bench-win-01", "bench-win-01:8765", "http://"):
            self.assertRejects(
                ONE_DEVICE.replace('"http://bench-win-01:8765"', f'"{bad}"'),
                "must be http",
            )

    def test_rule4_shared_agent_requires_pinned_ports(self):
        # Two boards on one bench host: without pinned ports, bootbench's COM
        # auto-probe opens the other board's live console mid-capture.
        content = ONE_DEVICE + """
  iq-8275-evk-01:
    enabled: true
    target: iq-8275-evk
"""
        self.assertRejects(content, "must pin")

    def test_rule4_satisfied_when_both_are_pinned(self):
        content = ONE_DEVICE + """
  iq-8275-evk-01:
    enabled: true
    target: iq-8275-evk
    com_port: COM9
    tac_port: VTP9
    adb_serial: "aabbccdd"
"""
        self.assertEqual(len(load_inventory(write(content))), 2)

    def test_rule4_ignores_disabled_devices(self):
        content = ONE_DEVICE + """
  iq-8275-evk-01:
    enabled: false
    target: iq-8275-evk
"""
        self.assertEqual(len(load_inventory(write(content))), 2)

    def test_rule4_does_not_fire_for_separate_agents(self):
        self.assertEqual(len(load_inventory(write(TWO_DEVICES))), 2)

    def test_rule5_same_slug_on_one_agent_is_refused(self):
        # Both would write to bootchart-data-iq9075evk.json on the same host.
        content = ONE_DEVICE + """
  iq-9075-evk-02:
    enabled: true
    target: iq_9075_evk
    com_port: COM9
    tac_port: VTP9
    adb_serial: "aabbccdd"
"""
        self.assertRejects(content, "overwrite one another")

    def test_rule5_same_slug_on_different_agents_is_fine(self):
        content = ONE_DEVICE + """
  iq-9075-evk-02:
    enabled: true
    target: iq-9075-evk
    agent_url: "http://bench-win-02:8765"
"""
        self.assertEqual(len(load_inventory(write(content))), 2)

    def test_rule6_control_characters_are_refused(self):
        path = write(ONE_DEVICE)
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "com_port: COM7", 'com_port: "COM7\x00"'
            ),
            encoding="utf-8",
        )
        # PyYAML rejects NUL itself, before validation runs; the stdlib
        # fallback parser happily passes it through, so our own check is the
        # real defense there. Either way the inventory must not load.
        real = config_loader.HAVE_YAML
        try:
            for have_yaml in (True, False):
                if have_yaml and not real:
                    continue
                config_loader.HAVE_YAML = have_yaml
                with self.assertRaises(InventoryError, msg=f"HAVE_YAML={have_yaml}"):
                    load_inventory(path)
        finally:
            config_loader.HAVE_YAML = real

    def test_rule6_nul_is_named_on_the_fallback_path(self):
        path = write(ONE_DEVICE)
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "com_port: COM7", 'com_port: "COM7\x00"'
            ),
            encoding="utf-8",
        )
        real = config_loader.HAVE_YAML
        try:
            config_loader.HAVE_YAML = False
            with self.assertRaises(InventoryError) as ctx:
                load_inventory(path)
        finally:
            config_loader.HAVE_YAML = real
        self.assertIn("NUL", " ".join(ctx.exception.problems))

    def test_rule6_hash_is_refused_because_the_fallback_parser_truncates_it(self):
        # Under the stdlib parser "COM7 # note" silently becomes "COM7",
        # which would be a different COM port on another host.
        self.assertRejects(
            ONE_DEVICE.replace("adb_serial: \"1a2b3c4d5e\"", "adb_serial: \"1a2b#3c\""),
            "truncated",
        )

    def test_num_boots_must_be_positive(self):
        self.assertRejects(ONE_DEVICE.replace("num_boots: 3", "num_boots: 0"), ">= 1")

    def test_non_integer_num_boots_is_refused(self):
        self.assertRejects(
            ONE_DEVICE.replace("num_boots: 3", 'num_boots: "three"'), "must be an integer"
        )

    def test_a_list_of_devices_is_refused_with_an_explanation(self):
        content = """
defaults:
  agent_url: "http://h:1"
devices:
  - device_id: a
    target: t
"""
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(write(content))
        self.assertIn("mapping keyed by device id", " ".join(ctx.exception.problems))

    def test_empty_devices_block_is_refused(self):
        self.assertRejects("defaults:\n  agent_url: \"http://h:1\"\ndevices:\n", "at least one")

    def test_missing_file_is_refused(self):
        with self.assertRaises(InventoryError):
            load_inventory(Path("/nonexistent/devices.yaml"))

    def test_all_problems_are_reported_not_just_the_first(self):
        content = """
defaults: {}
devices:
  ok-device:
    target: t1
  another:
    target: t2
"""
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(write(content))
        self.assertGreaterEqual(len(ctx.exception.problems), 2)


class TestFallbackParserAgreement(unittest.TestCase):
    """Both YAML paths must agree, or a host without PyYAML silently reads a
    different inventory than the one under test."""

    def _both(self, content):
        path = write(content)
        real = config_loader.HAVE_YAML
        try:
            config_loader.HAVE_YAML = True
            with_yaml = load_inventory(path) if real else None
            config_loader.HAVE_YAML = False
            without_yaml = load_inventory(path)
        finally:
            config_loader.HAVE_YAML = real
        return with_yaml, without_yaml

    def test_single_device_agrees(self):
        with_yaml, without_yaml = self._both(ONE_DEVICE)
        if with_yaml is None:
            self.skipTest("PyYAML not installed")
        self.assertEqual(with_yaml.devices, without_yaml.devices)

    def test_two_devices_agree(self):
        with_yaml, without_yaml = self._both(TWO_DEVICES)
        if with_yaml is None:
            self.skipTest("PyYAML not installed")
        self.assertEqual(with_yaml.devices, without_yaml.devices)

    def test_fallback_parses_nested_maps_and_scalars(self):
        parsed = config_loader.parse_simple_yaml(ONE_DEVICE)
        self.assertEqual(parsed["defaults"]["num_boots"], 3)
        self.assertIs(parsed["defaults"]["fetch_full_logs"], False)
        self.assertEqual(
            parsed["devices"]["iq-9075-evk-01"]["adb_serial"], "1a2b3c4d5e"
        )
        self.assertIs(parsed["devices"]["iq-9075-evk-01"]["enabled"], True)


class TestShippedExample(unittest.TestCase):
    def test_the_example_file_is_valid(self):
        # A broken example is worse than no example.
        example = HERE.parent / "config" / "devices.yaml.example"
        inv = load_inventory(example)
        self.assertGreaterEqual(len(inv), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
