#!/usr/bin/env python3
"""Stand-in for bootbench.py, for testing the agent protocol without hardware.

Accepts the real argv the agent composes, emits plausible stdout line by line,
writes a --json-status document in the same shape the real script will, and
builds a fake Boot-Logs tree so the artifact manifest has something to
enumerate. --fail / --slow / --hang control the failure mode under test.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("stage", nargs="+")
p.add_argument("--target")
p.add_argument("--com-port")
p.add_argument("--tac-port")
p.add_argument("--adb-serial")
p.add_argument("--num-boots", type=int, default=3)
p.add_argument("--boot-timeout", type=int, default=480)
p.add_argument("--share-root")
p.add_argument("--build-path")
p.add_argument("--boot-charts-dir")
p.add_argument("--boot-logs-dir")
p.add_argument("--json-status")
p.add_argument("--cancel-file")
p.add_argument("--lock-file")
p.add_argument("--json", action="store_true")
p.add_argument("--yes", action="store_true")
p.add_argument("--non-interactive", action="store_true")
p.add_argument("--resume-pull", action="store_true")
p.add_argument("--revert-debug-cmdline", action="store_true")
p.add_argument("--recover", action="store_true")
# test knobs
p.add_argument("--stub-fail", type=int, default=0)
p.add_argument("--stub-slow", type=float, default=0.0)
args = p.parse_args()

# The agent composes argv from its own config and never passes test knobs, so
# tests that need a slow or failing run set these in the environment instead.
args.stub_slow = args.stub_slow or float(os.environ.get("STUB_SLOW", 0) or 0)
args.stub_fail = args.stub_fail or int(os.environ.get("STUB_FAIL", 0) or 0)

BUILD = 2471
FOLDER = f"qcom-multimedia-proprietary-image_Nightly_Build_master_{BUILD}"

if args.stage == ["latest-build"]:
    print(json.dumps({
        "share_root": args.share_root,
        "build_number": BUILD,
        "build_folder": FOLDER,
        "build_path": f"{args.share_root}\\{FOLDER}",
        "performance_path": f"{args.share_root}\\{FOLDER}\\performance",
        "target": args.target,
        "build_dir": f"{args.share_root}\\{FOLDER}\\performance\\x-{args.target}",
        "target_image_ready": True,
    }))
    sys.exit(0)

charts = Path(args.boot_charts_dir)
logs_run = Path(args.boot_logs_dir) / args.target / FOLDER
charts.mkdir(parents=True, exist_ok=True)
logs_run.mkdir(parents=True, exist_ok=True)

slug = args.target.replace("-", "").replace("_", "")
(charts / f"bootchart-data-{slug}.json").write_text(
    json.dumps({"runs": [{"timestamp": "2026-10-06 01:04", "boots": []}]}), encoding="utf-8"
)
(charts / f"bootchart-overview-{slug}.html").write_text(
    "<html><body>stub report</body></html>", encoding="utf-8"
)

boots = []
for phase in ("default", "debug"):
    for i in range(1, args.num_boots + 1):
        name = f"{phase}-{i}"
        folder = logs_run / f"Logs-{name}"
        folder.mkdir(exist_ok=True)
        for fname in ("systemd_analyze.txt", "systemd-analyze_critical_chain.txt",
                      "systemd-analyze_critical_chain_sysinit.txt", "blame_systemd.txt",
                      "dmesg.txt", "journalctl.log", "kernel_cmdline.txt",
                      "plot_systemd.svg", "proc_config.txt", "Boot_Trace.txt",
                      "lsmod.txt"):
            (folder / fname).write_text(f"stub {name} {fname}\n", encoding="utf-8")
        (logs_run / f"{name}_2026-10-06_0104.txt").write_text("stub backup\n", encoding="utf-8")
        print(f"Boot {name}: collecting logs...", flush=True)
        if args.stub_slow:
            time.sleep(args.stub_slow)
        if args.cancel_file and Path(args.cancel_file).exists():
            print("Cancellation requested; stopping at safe point.", flush=True)
            break
        boots.append({
            "timestamp": "2026-10-06 01:04", "phase": phase, "log_name": name,
            "boot_index": i, "recorded": True, "parse_error": None,
            "kernel_cmdline": "console=ttyMSM0" + (
                " initcall_debug log_buf_len=4M systemd.log_level=debug"
                if phase == "debug" else ""),
            "seconds": {
                "nhlos": 2.104, "kernel": 5.912, "initramfs": 1.433,
                "sysinit_svc": 4.201, "total_sysinit": 9.449,
                "total_multiuser": 12.481 + i * 0.01, "grand_total": 14.585,
            },
            "overall_overheads": [
                {"name": "libvirtd.service", "time": "2.362 s", "note": "slow service"}
            ],
            "critical_chain": ["multi-user.target", "libvirtd.service"],
        })
    else:
        continue
    break

status = {
    "schema_version": 1,
    "host": os.environ.get("COMPUTERNAME", "stub"),
    "argv": sys.argv,
    "stages_requested": args.stage,
    "stages_run": args.stage,
    "current_stage": None,
    "target": args.target,
    "adb_serial": args.adb_serial,
    "share_root": args.share_root,
    "build_path": f"{args.share_root}\\{FOLDER}\\performance",
    "build_folder": FOLDER,
    "build_number": BUILD,
    "boots_expected": 2 * args.num_boots,
    "boots_recorded": len(boots),
    "partial": len(boots) < 2 * args.num_boots,
    "started_utc": datetime.now(timezone.utc).isoformat(),
    "ended_utc": datetime.now(timezone.utc).isoformat(),
    "exit_code": args.stub_fail,
    "failure_stage": "flash" if args.stub_fail else None,
    "error_class": "BootbenchError" if args.stub_fail else None,
    "error_message": "stub failure" if args.stub_fail else None,
    "output": {
        "data_json": str(charts / f"bootchart-data-{slug}.json"),
        "html": str(charts / f"bootchart-overview-{slug}.html"),
        "boot_logs_run_dir": str(logs_run),
    },
    "boots": boots,
}
Path(args.json_status).write_text(json.dumps(status, indent=2), encoding="utf-8")
print(f"Recorded {len(boots)} boots; exiting {args.stub_fail}", flush=True)
sys.exit(args.stub_fail)
