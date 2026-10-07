"""Generate boot-log fixtures and golden output snapshots.

Run this BEFORE editing bootbench.py to capture what the current code
produces, then again with --verify after editing to prove nothing moved.

The golden files are the contract: `bootbench.py` is run by hand by
engineers today, and every invocation documented in SKILL.md must behave
identically after the automation changes. A byte-identical HTML diff is the
cheapest possible proof of that.

    py -3 make_golden.py            # write fixtures + goldens
    py -3 make_golden.py --verify   # fail if anything changed
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "boot_logs"
GOLDEN = HERE / "golden"

# One locator for the skill, shared with the ingest path. A hardcoded
# relative tree here would be a second place to fix whenever the layout
# moves -- and the copy that `validate-config` does not exercise is the one
# that silently rots.
sys.path.insert(0, str(HERE.parents[1]))
from src.utils import bootbench_api  # noqa: E402

SKILL = bootbench_api.find_bootbench().parent
sys.path.insert(0, str(SKILL))

# --- fixture content -------------------------------------------------------
# Formats taken from the parsers themselves (parse_systemd_time,
# parse_critical_chain, parse_stage_hitters, parse_blame,
# parse_dmesg_milestones, parse_kernel_hitters, parse_journalctl_milestone).

SAT = """\
Startup finished in 4.318s (firmware) + 717ms (loader) + 1.573s (kernel) \
+ 12.482s (userspace) = 19.091s
multi-user.target reached after 12.481s in userspace.
graphical.target reached after 13.264s in userspace.
"""

CC = """\
The time when unit became active or started is printed after the "@" character.
The time the unit took to start is printed after the "+" character.

multi-user.target @12.481s
`-docker.service @8.112s +4.361s
  `-network-online.target @8.102s
    `-NetworkManager-wait-online.service @3.291s +4.810s
      `-NetworkManager.service @2.998s +0.290s
        `-dbus.socket @2.971s
          `-sysinit.target @2.962s
"""

CC_SYSINIT = """\
The time when unit became active or started is printed after the "@" character.
The time the unit took to start is printed after the "+" character.

sysinit.target @2.962s
`-systemd-tmpfiles-setup.service @2.901s +0.057s
  `-systemd-journal-flush.service @1.884s +1.012s
    `-systemd-remount-fs.service @1.702s +0.174s
      `-systemd-fsck-root.service @1.455s +0.241s
        `-systemd-udevd.service @1.102s +0.349s
          `-local-fs.target @1.088s
"""

BLAME = """\
10.416s android-tools-adbd.service
 4.810s NetworkManager-wait-online.service
 4.361s docker.service
 1.012s systemd-journal-flush.service
  349ms systemd-udevd.service
  290ms NetworkManager.service
  241ms systemd-fsck-root.service
"""

DMESG_DEFAULT = """\
[    0.000000] Booting Linux on physical CPU 0x0000000000 [0x414fd0b1]
[    0.000000] Linux version 6.6.28-qcom (oe-user@oe-host) #1 SMP PREEMPT
[    0.000000] Kernel command line: console=ttyMSM0,115200n8 root=PARTUUID=abc rw
[    1.433117] Run /init as init process
[    1.891204] systemd[1]: System time advanced to built-in epoch
[    1.902551] systemd[1]: systemd 254.4+ running in system mode (+PAM +AUDIT)
[    2.962000] systemd[1]: Reached target sysinit.target
"""

# Debug phase: initcall_debug active, so initcall/probe timings are present.
DMESG_DEBUG = """\
[    0.000000] Booting Linux on physical CPU 0x0000000000 [0x414fd0b1]
[    0.000000] Kernel command line: console=ttyMSM0,115200n8 root=PARTUUID=abc rw \
initcall_debug log_buf_len=4M systemd.log_level=debug
[    0.104221] initcall pcibios_init+0x0/0x40 returned 0 after 412887 usecs
[    0.212004] probe of 1c00000.ufshc returned 0 after 198441 usecs
[    0.318882] initcall clk_disable_unused+0x0/0x100 returned 0 after 91204 usecs
[    0.402118] initcall regulator_init_complete+0x0/0x80 returned -19 after 8821 usecs
[    0.511904] soc:qcom,venus: probe with driver venus failed with error -517
[    1.498332] Run /init as init process
[    2.014882] systemd[1]: System time advanced to built-in epoch
[    2.028104] systemd[1]: systemd 254.4+ running in system mode (+PAM +AUDIT)
"""

JOURNAL = """\
-- Journal begins at Mon 2026-10-05 01:02:11 UTC --
[    1.902551] localhost systemd[1]: systemd 254.4+ running in system mode.
[    2.962118] localhost systemd[1]: Reached target System Initialization.
[   12.481002] localhost systemd[1]: Reached target Multi-User System.
"""

CMDLINE_DEFAULT = (
    "console=ttyMSM0,115200n8 root=PARTUUID=abc rw\n"
)
CMDLINE_DEBUG = (
    "console=ttyMSM0,115200n8 root=PARTUUID=abc rw initcall_debug "
    "log_buf_len=4M systemd.log_level=debug\n"
)

# The four files collect_boot_logs.sh writes that no parser reads. Present so
# artifact classification and the "boot_extra" kind have something to see.
EXTRA = {
    "plot_systemd.svg": "<svg xmlns='http://www.w3.org/2000/svg'></svg>\n",
    "proc_config.txt": "CONFIG_ARM64=y\nCONFIG_PREEMPT=y\n",
    "Boot_Trace.txt": "# tracer: nop\n",
    "lsmod.txt": "Module                  Size  Used by\nqcom_venus  294912  0\n",
    "blame_systemdsystemctl.txt": "UNIT LOAD ACTIVE SUB DESCRIPTION\n",
}

PHASES = {
    "default": (DMESG_DEFAULT, CMDLINE_DEFAULT),
    "debug": (DMESG_DEBUG, CMDLINE_DEBUG),
}

BUILD_PATH = r"\\swayam\QLI_Builds\Yocto\qcom-multimedia-proprietary-image_Nightly_Build_master_2471\performance"
TARGET = "iq-9075-evk"


def write_fixtures(num_boots=3) -> None:
    for phase, (dmesg, cmdline) in PHASES.items():
        for i in range(1, num_boots + 1):
            d = FIXTURES / f"Logs-{phase}-{i}"
            d.mkdir(parents=True, exist_ok=True)
            files = {
                "systemd_analyze.txt": SAT,
                "systemd-analyze_critical_chain.txt": CC,
                "systemd-analyze_critical_chain_sysinit.txt": CC_SYSINIT,
                "blame_systemd.txt": BLAME,
                "dmesg.txt": dmesg,
                "journalctl.log": JOURNAL,
                "kernel_cmdline.txt": cmdline,
                **EXTRA,
            }
            for name, content in files.items():
                # newline="" so the bytes on disk are exactly what is written,
                # on Windows as well. A golden file that depends on the host's
                # newline translation is not a golden file.
                (d / name).write_text(content, encoding="utf-8", newline="")


def build_boots():
    """Parse the fixtures into run dicts, exactly as pull_and_record does."""
    import bootbench as bb

    boots = []
    for phase in ("default", "debug"):
        for i in (1, 2, 3):
            log_name = f"{phase}-{i}"
            texts = bb.read_pulled_logs(FIXTURES, log_name)
            boot = bb.build_run(
                TARGET, BUILD_PATH,
                texts["sat_text"], texts["cc_text"], texts["cc_sysinit_text"],
                texts["blame_text"], texts["dmesg_text"], texts["journalctl_text"],
            )
            boot["phase"] = phase
            boot["log_name"] = log_name
            # build_run stamps datetime.now(); pin it so the golden is stable.
            boot["timestamp"] = "2026-10-06 01:04"
            boots.append(boot)
    return boots


def make_golden() -> dict:
    import bootbench as bb

    boots = build_boots()
    entry = {
        "timestamp": boots[0]["timestamp"],
        "build_path": BUILD_PATH,
        "boots": [boots[0], boots[3]],
    }
    data = {"device": TARGET, "runs": [entry]}
    return {
        "boots.json": json.dumps(boots, indent=2, ensure_ascii=False) + "\n",
        "report.html": bb.render_html(data),
        "run_txt.txt": bb._format_run_txt(TARGET, boots[0]),
    }


def _describe_diff(expected: str, actual: str, max_lines=12) -> str:
    """A unified diff of the first divergence.

    Byte counts are useless here -- the changes most likely to slip through
    are equal-length (one digit, one label), so show the lines themselves.
    """
    diff = difflib.unified_diff(
        expected.splitlines(), actual.splitlines(),
        fromfile="golden", tofile="produced", lineterm="", n=1,
    )
    lines = list(diff)
    head = lines[:max_lines]
    if len(lines) > max_lines:
        head.append(f"  ... {len(lines) - max_lines} more diff line(s)")
    return "\n".join("  " + line for line in head)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    write_fixtures()
    GOLDEN.mkdir(parents=True, exist_ok=True)
    produced = make_golden()

    if not args.verify:
        for name, content in produced.items():
            (GOLDEN / name).write_text(content, encoding="utf-8", newline="")
        print(f"wrote {len(produced)} golden file(s) to {GOLDEN}")
        return 0

    failures = []
    for name, content in produced.items():
        path = GOLDEN / name
        if not path.is_file():
            failures.append(f"{name}: no golden file to compare against")
            continue
        expected = path.read_text(encoding="utf-8", newline="")
        if expected != content:
            failures.append(f"{name}: CHANGED\n{_describe_diff(expected, content)}")

    for line in failures:
        print(line, file=sys.stderr)
    if failures:
        return 1
    print(f"all {len(produced)} golden file(s) unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
