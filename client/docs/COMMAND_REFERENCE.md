# Command Reference

## Invocation

```
bootbench.py {flash,capture,report,all} [{flash,capture,report,all} ...] [options]
bootbench.py latest-build [--json] [--share-root PATH] [--target TARGET]
```

Stages can be listed in any order on the command line, but always execute
in `flash` → `capture` → `report` order. `capture` records and renders its
own report at the end, so a separate `report` invocation is only needed
when `capture` isn't part of the same command.

`latest-build` is not part of that pipeline — it is a standalone query that
touches no hardware and cannot be combined with other stages.

## Common flags (apply to `flash` and/or `capture`)

- `--target TARGET` — override/declare target name (e.g. `iq-9075-evk`).
  Auto-detected via serial otherwise, except `report` alone or `capture
  --resume-pull`, which require it explicitly.
- `--com-port COM_PORT` — serial console COM port (auto-detected if omitted)
- `--boot-timeout SECONDS` — login-prompt wait timeout (default: 480)

## `flash` flags

- `--tac-port TAC_PORT` — Alpaca TAC COM port (auto-detected if only one TAC device)
- `--dry-run` — resolve paths and print the plan, then exit before touching the device
- `--yes` — skip the y/N confirmation prompt before flashing
- `--skip-edl` — device is already in EDL mode; skip the TAC BootToEDL step
- `--recover` — power-cycle the device via TAC out of EDL/Sahara, then exit (requires the `flash` stage)

## `capture` flags

- `--build-path PATH` — nightly build path used for this boot (required if `capture` runs without `flash` in the same command; supplied automatically otherwise)
- `--num-boots N` — number of consecutive boots to capture **per phase**
  (default: 3 → 3 default-cmdline + 3 debug-cmdline = 6 boots total).
  **Values below 1 fall back to 3, with a printed warning** — this is
  validated once, in `_run()`, before any device interaction. All N boots
  per phase are still captured and permanently backed up under
  `Boot-Logs/`; only the first boot of each phase is recorded into the
  JSON/HTML report. With `--resume-pull`, omitting it discovers the boots
  actually present on the device instead of assuming 3.
- `--resume-pull` — skip login/boot-loop entirely: adb is already enabled
  and all boots' `/data/Logs-<n>` dirs are already on the device (a prior
  run reached `adb pull` and failed partway) — just pull logs and
  (re)build the report. Requires `--target` and `--build-path`. Does
  **not** call the debug-cmdline-editing step, so whether that boot's
  debug prints were actually active is inferred from its pulled `dmesg`
  content and `kernel_cmdline.txt`, not assumed.

## `report` flags

- `--report-cmd {render,add-run}` — `render` (default) just regenerates
  the HTML from the existing JSON; `add-run` appends `--run-json` first
- `--run-json PATH` — run JSON to append (or `-` for stdin), used with `--report-cmd add-run`

## `automation` flags

Every flag in this group is optional and defaults to off. They exist for
running the script from a scheduler rather than a keyboard; **omit them and
every command above behaves exactly as it always has.**

- `--json` — with `latest-build`, print *only* a JSON object to stdout and
  no prose, so a caller can parse stdout directly.
- `--json-status PATH` — write a machine-readable status document to `PATH`
  (see [DATA_MODEL.md](DATA_MODEL.md#status-document-automation)). Written
  from a `finally` block, so the file exists on success, on failure, on
  Ctrl-C, and on an unexpected crash.
- `--adb-serial SERIAL` — route every adb call as `adb -s SERIAL`. Required
  when more than one device is attached to the host; without it `adb` picks
  one arbitrarily and the capture can silently run against the wrong board.
- `--boot-charts-dir DIR` — override where the report JSON/HTML are written
- `--boot-logs-dir DIR` — override where pulled boot logs are stored
- `--share-root PATH` — override the nightly build share
- `--non-interactive` — never prompt. A prompt that would have been shown
  becomes a usage error (exit 2) instead of blocking forever or raising
  `EOFError` on a closed stdin. Implied automatically when stdin is not a
  TTY.
- `--lock-file PATH` — hold an exclusive lock while reading/writing the
  report, so a hand-run invocation and a scheduled one cannot interleave
  and lose a run. A lock whose recorded PID is gone is broken automatically.
- `--cancel-file PATH` — poll `PATH` between boots and before flashing; if
  it exists, stop cleanly with exit 130. **A cancel arriving mid-flash is
  refused by design** — interrupting PCAT mid-write to a boot partition is
  damage, not a cancellation.
- `--revert-debug-cmdline` — remove `initcall_debug`, `log_buf_len=4M`, and
  `systemd.log_level=debug` from the bootloader entry after the debug
  phase. Off by default: `ensure_debug_cmdline_params` never reverted them,
  so a board stays debug-y until reflashed.

## Exit codes

`0` on success. Previously every failure exited `1`; the specific codes
below are new, and `1` is still what an unrecognized failure returns.

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | unknown / unhandled error (prints a traceback) |
| 2 | usage error |
| 10 | build discovery (share unreachable, no `master_<n>` build, missing image) |
| 11 | EDL entry / enumeration |
| 12 | PCAT flash — **not safe to retry blindly**, a partition may be mid-write |
| 13 | serial login |
| 14 | boot collection |
| 15 | `adb` — logs are already on the device, retry with `capture --resume-pull` |
| 16 | parse (no boot could be parsed) |
| 17 | recording/rendering the report |
| 18 | Alpaca TAC (COM failure, ambiguous device count) |
| 130 | interrupted (Ctrl-C, or `--cancel-file` observed) |

A run in which *some* boots parsed and some did not exits `0` and reports
`partial: true` in the status document — one bad log file no longer
discards an hour of booting.

## Worked examples

```
# Flash only -- waits for a manual y/N confirmation right before flashing
py -3 bootbench.py flash

# Flash, then capture boot-time logs (skip the y/N prompt with --yes)
py -3 bootbench.py flash capture --yes

# Flash, capture, and report -- the full pipeline in one command
py -3 bootbench.py all --yes

# Capture only, on a device that's already flashed with this build
py -3 bootbench.py capture --build-path "\\swayam\...\performance"

# Capture with more boots per phase (still only 1 default + 1 debug shown in the report)
py -3 bootbench.py capture --build-path "\\swayam\...\performance" --num-boots 5

# Resume a capture that flashed/booted fine but failed during the adb-pull/report step
py -3 bootbench.py capture --build-path "\\swayam\...\performance" --target iq-9075-evk --resume-pull

# Recover a device stuck in EDL/Sahara mode
py -3 bootbench.py flash --recover

# Re-render the HTML report from the existing JSON (no device involved)
py -3 bootbench.py report --report-cmd render --target iq-9075-evk

# Manually append a run JSON to the report (no device involved)
py -3 bootbench.py report --report-cmd add-run --run-json new_run.json --target iq-9075-evk

# What is the newest nightly on the share? (no device involved, no hardware)
py -3 bootbench.py latest-build
py -3 bootbench.py latest-build --json --target iq-9075-evk

# Unattended: separate exit codes, a status document, an explicit device
py -3 bootbench.py all --yes --non-interactive ^
    --target iq-9075-evk --com-port COM7 --tac-port VTP8 ^
    --adb-serial 1a2b3c4d5e ^
    --boot-charts-dir C:\bench\artifacts\dev01\Boot-Charts ^
    --boot-logs-dir   C:\bench\artifacts\dev01\Boot-Logs ^
    --json-status     C:\bench\artifacts\dev01\jobs\42\status.json ^
    --lock-file       C:\bench\artifacts\dev01\.bootbench.lock
```

## Recovery / resume options

- `flash --recover`: device stuck in EDL/Sahara after an aborted flash →
  power-cycles it back to normal boot and exits.
- `capture --resume-pull` (requires `--target` and `--build-path`):
  flashing and all boots already succeeded but the `adb pull`/report step
  failed partway → re-runs just the pull + report step without repeating
  the flash or any boots. **Exit code 15 is the signal to use this** — the
  logs are already sitting in `/data/Logs-<n>` on the device, so reflashing
  and re-booting six times would throw away an hour of work that already
  succeeded.
- **Ctrl+C during a flash**: kills the PCAT process and automatically
  power-cycles the device back to normal boot.
- A run that exits `0` with `partial: true` in the status document needs no
  recovery: some boots parsed, some did not, and the ones that did are in
  the report. Check each failed boot's `parse_error`.

## Prerequisites

```
py -3 -m pip install pyserial comtypes
```

- `pyserial` — required for both `flash` and `capture` (serial console
  login, command execution, port auto-detection)
- `comtypes` — required for `flash` only (Alpaca TAC COM automation for
  power-cycle / EDL entry)
- `adb` must also be on `PATH` — used by `capture` to pull logs off the
  device after boot capture

Run with either the ARM64 Python launcher (`py -3 bootbench.py ...`) or a
regular Windows Python install (`python3 bootbench.py ...`).
