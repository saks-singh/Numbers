# Data Model

## Source of truth

`Boot-Charts/bootchart-data-<slug>.json` (`<slug>` = target name with
hyphens/underscores stripped) is normally only ever changed via the
`capture` stage or `report --report-cmd add-run` — never hand-edit it
directly, with exactly one sanctioned exception: an agent filling in a
boot's `optimization_possibilities` field per the Workflow in `SKILL.md`.
No other field should be hand-edited.

`Boot-Charts/bootchart-overview-<slug>.html` is **entirely** generated from
that JSON by `render()`. Never hand-edit it — re-run `report
--report-cmd render` after any JSON change to regenerate it exactly.

## Run entry schema

Each element of the JSON's `runs` array is either:

- a **legacy flat entry** — a single boot's fields directly at the top
  level (`timestamp`, `metrics`, etc.), from before multi-boot capture
  existed, or
- a **multi-boot entry** (current format, produced by `capture`):
  ```json
  {
    "timestamp": "...",
    "build_path": "...",
    "boots": [ <default boot>, <debug boot> ]
  }
  ```

As of this version, a freshly captured multi-boot entry's `boots` list
always has exactly **2** items — the first default-phase boot and the
first debug-phase boot — even if `--num-boots` captured more per phase.
Older entries already on disk (written before this change) may still have
`2N` boots; they render as repeated columns and are left as-is — no
migration is performed.

Each boot object has:

- `timestamp` — `YYYY-MM-DD HH:MM`
- `build_path` — the nightly build path used
- `phase` — `"default"` or `"debug"` (absent on legacy entries)
- `log_name` — e.g. `"default-1"`, `"debug-1"` (absent on legacy entries;
  used to derive the simplified `Default`/`Debug` column header)
- `metrics` — object, fixed keys: `nhlos`, `kernel`, `initramfs`,
  `sysinit_svc`, `total_sysinit`, `total_multiuser`. Each metric has
  `value`, `note`, and optionally `hitters` (see below).
- `critical_chain` — ordered list of unit names (optional)
- `overall_overheads` — list of up to 3 hitter dicts (new, see below)
- `optimization_possibilities` — `null`, or a placeholder string (new, see below)
- `source` — free-text provenance string (optional)

`nhlos`, `initramfs`, and `total_sysinit` never carry `hitters` — there's
no per-component timing source for them today. `kernel` only carries
`hitters` on a debug-phase boot whose `dmesg` actually had
`initcall_debug` timing output (see below); it's absent otherwise,
including on every default-phase boot.

## Numeric metrics — the `seconds` key (new)

Every boot object also carries a `seconds` object: the same numbers the
`metrics` table shows, as plain JSON floats rather than `"12.481 s"`
strings, plus the stage sub-components that previously existed *only* inside
the prose `note`/`source` text.

```json
"seconds": {
  "nhlos": 2.104, "kernel": 5.912, "initramfs": 1.433,
  "sysinit_svc": 9.449, "total_sysinit": 11.553, "total_multiuser": 12.481,
  "grand_total": 19.091,
  "firmware": 4.318, "loader": 0.717, "userspace": 12.482, "sat_total": 19.091,
  "init_exec": 1.433, "epoch_advanced": 1.891, "systemd_running": 1.902,
  "sysinit_target": 2.962, "cc_multiuser": 12.481
}
```

Three properties worth relying on:

- **Additive and advisory.** Nothing in the rendering path reads it. The
  HTML, the `.txt` backups, and `_validate_boot`'s required-key check are
  unaffected by its presence or absence, and a boot object without it (one
  recorded by an older copy of the script) still renders and validates
  normally. It exists for consumers that want arithmetic — trend charts, a
  database, a build-over-build delta — without re-parsing display strings.
- **The displayed number, exactly.** Each value is rounded to the same 3
  decimals the formatted string uses, so `seconds.total_multiuser` and
  `metrics.total_multiuser.value` can never disagree.
- **Absent, never zero.** A metric that couldn't be derived is omitted from
  the object rather than written as `0`. A missing boot time and a 0.000 s
  boot time are very different claims.

`run_metrics_seconds(run)` is the accessor to use rather than reading the
key directly: it prefers `seconds` and falls back to `parse_seconds()` over
the formatted `metrics` values for boots recorded before the key existed.
The six `METRIC_ROWS` metrics are recoverable that way; the sub-components
are not, and come back absent.


## Kernel-row debug hitters (new)

On a debug-phase boot, if that boot's `dmesg.txt` contains
`initcall_debug`-style timing/failure lines, `metrics.kernel.hitters` is
populated with up to 6 entries:

1. Up to 3 entries: the slowest probes/initcalls by usecs, descending.
   `{"name": "<probe/initcall>", "time": "<seconds> s", "note": "slowest probe/initcall"}`
2. Up to 3 entries: probe/initcall failures (deduped by name).
   `{"name": "<probe/initcall>", "time": "rc=<n>", "note": "probe/initcall failure"}` or
   `{"name": "<driver/device>", "time": "error <n>", "note": "probe failure"}`

If `dmesg` carries no such output at all (Type #2 bootloader, or any other
reason the debug cmdline params weren't active for that boot),
`metrics.kernel` has **no** `hitters` key — the same shape as a
default-phase boot. This absence is exactly what drives the rendered "No
debug prints enabled" subtitle under that boot's column header — it's a
content check, not a separately stored flag, so it's correct even for
runs produced via `--resume-pull` (which never touches the cmdline-editing
step at all).

## Overall Suspected Overheads (new)

`overall_overheads` is computed for **every** boot (default and debug
alike). It pools the *individual* hitters already present in that boot's
`metrics.kernel.hitters` (debug boots only), `metrics.sysinit_svc.hitters`,
and `metrics.total_multiuser.hitters`, filters out any hitter whose `time`
isn't a plain parseable duration (this excludes the Kernel row's
failure-kind entries, e.g. `"rc=-19"` or `"error -5"`, which don't compete
in a duration ranking), sorts the remaining pool by actual seconds
descending, and keeps the top 3.

This is **not** a comparison of stage totals (NHLOS vs. kernel vs.
initramfs vs. sysinit vs. userspace) — it's a re-ranking of the specific
units already flagged as high-hitters *within* those stages, so the top 3
are whichever individual costs (a specific slow probe, a specific slow
service, ...) are the biggest, regardless of which stage they happened to
occur in. On a default-phase boot (no kernel hitters to contribute), it's
still populated from that boot's sysinit + multi-user hitters.

## Optimization Possibilities (new)

`optimization_possibilities` is `null` by default. `bootbench.py` sets it
to a fixed placeholder string on a debug-phase boot whose `metrics.kernel`
has a non-empty `hitters` list (i.e., debug prints were actually active):

> "Not yet analyzed — ask an agent to review the overheads above and suggest fixes."

`bootbench.py` itself performs no analysis — this field is the one part of
the schema an agent is expected to hand-edit. Per the skill's Workflow
(see `SKILL.md`), after a capture completes an agent must read that boot's
`overall_overheads` (and, on the Debug boot, the Kernel row's
probe/initcall hitters), write concrete optimization suggestions naming
the actual unit/probe involved, replace the placeholder (or `null`) in
this field with that text, then re-render the HTML
(`report --report-cmd render`) so the report shows it. This is a sanctioned
exception to the "never hand-edit the JSON" rule — no other field should
be touched by hand. Default-phase boots, and debug-phase boots with no
active debug prints, keep it `null` (renders as `–`) unless there's a real
finding worth writing up from their own `overall_overheads`.

## Rank, not identity

Every `hitters` list (Kernel, sysinit, multi-user, and the pooled
`overall_overheads`) is ranked by time for that specific boot — it is not
a stable identity list. The same unit can appear at a different rank, or
not appear at all, on a different boot. This is expected, not a bug.

## Legacy flat hitters

Entries captured before hitters were merged into the metrics table may
carry a flat top-level `hitters` list instead of a nested
`metrics.total_multiuser.hitters`. Rendering treats a flat list as
belonging to `total_multiuser`, for backward compatibility.

## Retention

- JSON/HTML keep only the most recent 30 run entries (trimmed from the
  front on each `add_run`).
- Full history survives separately as per-boot `.txt` backups under
  `Boot-Logs/<target>/<build-folder>/`, unbounded, one per pulled boot
  (all `2N` of them, not just the 2 shown in the JSON/HTML). Numeric
  suffixes (`_2`, `_3`, ...) avoid overwriting when multiple boots finish
  within the same minute-granularity timestamp.

## Manually appending a run

Use `report --report-cmd add-run --run-json <path or ->`. This validates
the entry against the required schema (raises on missing keys) before
appending and triggering a re-render. Never bypass this by editing the
JSON file directly.

## Status document (automation)

`--json-status PATH` writes a second, entirely separate artifact: a report
on *the job*, not on the boot. The report JSON above answers "how fast did
this board boot"; the status document answers "did this run do what it was
asked to, and if not, where did it stop". Nothing reads it back — it is
write-only output for a scheduler, and the `capture`/`report` stages behave
identically whether or not the flag is passed.

**It is always written.** `write_status()` is called from a `finally` in
`main()`, so the file exists on success, on a tagged failure, on Ctrl-C,
on a usage error, and on an unexpected crash. The write is atomic (temp
file in the destination directory, `fsync`, `os.replace`), so a reader
polling the path never sees a half-written document.

```json
{
  "schema_version": 1,
  "host": "BENCH-WIN-01",
  "argv": ["all", "--yes", "--non-interactive", "--target", "iq-9075-evk"],
  "started_utc": "2026-10-06T01:04:11+00:00",
  "ended_utc":   "2026-10-06T01:46:22+00:00",
  "stage": "adb",
  "stage_history": ["build_discovery", "tac", "edl", "flash", "serial_login",
                    "boot_collect", "adb", "record"],
  "stages_requested": ["flash", "capture", "report"],
  "stages_run": ["flash", "capture"],
  "target": "iq-9075-evk",
  "adb_serial": "1a2b3c4d5e",
  "share_root": "\\\\swayam\\QLI_Builds\\Yocto",
  "build_path": "\\\\swayam\\...\\master_2471\\performance",
  "build_folder": "..._Nightly_Build_master_2471",
  "build_number": 2471,
  "boots_expected": 6,
  "boots_recorded": 5,
  "partial": true,
  "exit_code": 0,
  "failure_stage": null,
  "error_class": null,
  "error_message": null,
  "outputs": {
    "data_json": "C:\\bench\\...\\bootchart-data-iq9075evk.json",
    "report_html": "C:\\bench\\...\\bootchart-overview-iq9075evk.html",
    "boot_logs_dir": "C:\\bench\\...\\Boot-Logs\\iq-9075-evk\\..."
  },
  "boots": [ ... ]
}
```

### Top-level fields

- `schema_version` — bump this, don't repurpose a field, if the shape ever
  changes. A consumer should refuse a version it doesn't know.
- `host`, `argv`, `started_utc`, `ended_utc` — provenance. Timestamps are
  **timezone-aware UTC, second granularity** (`datetime.now(timezone.utc)`),
  unlike a boot's `timestamp`, which is naive local time at minute
  granularity and is unsafe to order by.
- `stages_requested` / `stages_run` — the pipeline stages
  (`flash`/`capture`/`report`) asked for, and the ones actually reached.
  A run that failed in `flash` has `capture` in the first and not the
  second.
- `stage` / `stage_history` — the **fine-grained** labels that pair with the
  exit codes (`build_discovery`, `tac`, `edl`, `flash`, `serial_login`,
  `boot_collect`, `adb`, `parse`, `record`, `usage`). Deliberately a
  different axis from `stages_run`: collapsing the two would make
  `failure_stage` ambiguous, since "flash" is both a pipeline stage and a
  failure point inside it.
- `boots_expected` / `boots_recorded` / `partial` — `2 × --num-boots`,
  how many actually parsed, and whether that's fewer. **`partial: true`
  with `exit_code: 0` is a success**, not a failure: one unparseable log no
  longer discards the other five boots. Treat it as a quieter alert than a
  failure, because a silently degrading device looks exactly like this.
- `exit_code` / `failure_stage` / `error_class` / `error_message` — the
  code the process exited with (see the table in
  [COMMAND_REFERENCE.md](COMMAND_REFERENCE.md#exit-codes)), the stage label
  it came from, and the exception type and message verbatim. All four are
  `null`/`0` on a clean run.
- `outputs` — absolute paths to what this run produced. The report JSON and
  HTML live in per-device directories that are **stable across runs**, so
  these are how a consumer finds this run's slice of them.
- `build_path` is the shared `...\performance` directory, identical for
  every target built from the same nightly — **the target is not derivable
  from it**. Take `target` from this document's own field.

### Per-boot entries

`boots` has one entry for **every boot attempted** — all `2N` of them, not
just the 2 the report JSON displays — in the order they were pulled.

```json
{
  "phase": "default",
  "boot_index": 2,
  "log_name": "default-2",
  "timestamp": "2026-10-06 01:04",
  "parse_error": null,
  "kernel_cmdline": "console=ttyMSM0,115200n8 root=PARTUUID=abc rw",
  "cmdline_has_debug": false,
  "seconds": { ... },
  "hitters": { "sysinit_svc": [...], "total_multiuser": [...] },
  "critical_chain": ["multi-user.target", ..., "sysinit.target"],
  "overall_overheads": [...]
}
```

- `parse_error` — `null` on a boot that parsed, otherwise the exception type
  and message. A boot with a `parse_error` keeps only its identifying
  fields: `phase`, `boot_index` and `log_name` are real; `timestamp`,
  `kernel_cmdline` and `cmdline_has_debug` are present but `null` (parsing
  stopped before they could be read); and `seconds`, `hitters`,
  `critical_chain` and `overall_overheads` are **absent entirely** rather
  than empty. It does not count toward `boots_recorded`.
- `phase` + `boot_index` are first-class here for a reason that bites
  anyone trending these numbers naively: the collect script **disables and
  masks `rmtfs.service`**, persistently. Within one run, `default-1` boots
  with it enabled and `default-2`/`default-3` boot with it masked. Those
  are not the same system, and the step change looks exactly like a real
  regression. Compare like for like — `phase='default', boot_index=1` is
  the one boot per run that follows a fresh flash.
- `kernel_cmdline` is that boot's own `/proc/cmdline`, read from the
  `kernel_cmdline.txt` the collect script already writes. Read
  non-strictly: it may be absent.
- `cmdline_has_debug` is **three-valued**. `true`/`false` when the cmdline
  was readable, and **`null` when it wasn't** — "not known", which is not
  the same claim as "verified clean". A consumer excluding debug-polluted
  boots from a trend must distinguish them, because
  `systemd.log_level=debug` measurably slows boot and
  `ensure_debug_cmdline_params` never reverts it unless
  `--revert-debug-cmdline` is passed. This field is how a mislabelled
  "default" boot gets caught instead of silently poisoning the history.
- `seconds` is the same object documented above, via
  `run_metrics_seconds()`.
- `hitters` is keyed by metric name, and only metrics that actually have
  hitters appear — the same absence semantics as the report JSON.

