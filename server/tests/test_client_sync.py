"""The vendored client must match the skill bundle it was copied from.

`client/bootbench.py` is a copy of `boot-skills/skills/bootbench/scripts/
bootbench.py`. The copy is deliberate -- `client/` is deployed by copying
it to a Windows bench host with nothing installed -- but a duplicated
3000-line parser drifts, and when it does, the coordinator parses boot
times with one vintage of the regexes while the bench host produced them
with another. Nothing raises. A number on the dashboard is simply wrong by
a plausible-looking amount, which is the hardest kind of wrong to notice.

Skipped when the skill bundle is absent, which is the normal state of an
extracted standalone checkout: there, `client/` is the only copy and there
is nothing to compare it against.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SYNC = REPO / "tools" / "sync_client.py"
SKILL_ROOT = REPO.parent / "boot-skills" / "skills" / "bootbench"


@unittest.skipUnless(SKILL_ROOT.is_dir(),
                     f"no skill bundle at {SKILL_ROOT} (standalone checkout)")
class ClientSync(unittest.TestCase):

    def run_sync(self, *args):
        return subprocess.run(
            [sys.executable, str(SYNC), *args],
            capture_output=True, text=True, cwd=str(REPO))

    def test_the_vendored_client_matches_the_skill(self):
        result = self.run_sync("--check", "--diff")
        self.assertEqual(
            result.returncode, 0,
            "client/ is out of step with the skill bundle.\n"
            "Run: python tools/sync_client.py\n\n"
            + result.stdout + result.stderr)

    def test_drift_is_actually_detected(self):
        # The check is only worth having if it fails when it should. A
        # single changed byte in a staged copy of the skill must be caught,
        # including a line-ending change: that is a real diff to git and to
        # the golden-file tests, so it has to be one here.
        import shutil
        import tempfile

        sys.path.insert(0, str(REPO))
        from tools import sync_client

        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / "bootbench"
            shutil.copytree(SKILL_ROOT, staged)
            script = staged / "scripts" / "bootbench.py"
            script.write_bytes(script.read_bytes() + b"\n# drift\n")

            states = {dst.name: state
                      for _src, dst, state in sync_client.compare(staged)}

        self.assertEqual(states["bootbench.py"], "differs")
        self.assertEqual(states["DATA_MODEL.md"], "same")

    def test_every_vendored_file_exists_on_both_sides(self):
        sys.path.insert(0, str(REPO))
        from tools import sync_client

        for rel_src, rel_dst in sync_client.VENDORED:
            with self.subTest(file=rel_dst):
                self.assertTrue((SKILL_ROOT / rel_src).is_file(),
                                f"missing in the skill bundle: {rel_src}")
                self.assertTrue((REPO / rel_dst).is_file(),
                                f"not vendored: {rel_dst}")

    def test_the_coordinator_resolves_the_vendored_copy(self):
        # Not the skill bundle. The point of vendoring is that the server
        # parses with the same file the bench host runs; resolving the
        # sibling bundle instead would make the sync check meaningless.
        sys.path.insert(0, str(HERE.parent))
        from src.utils import bootbench_api

        resolved = bootbench_api.find_bootbench()
        self.assertEqual(resolved, (REPO / "client" / "bootbench.py").resolve())


if __name__ == "__main__":
    unittest.main(verbosity=2)
