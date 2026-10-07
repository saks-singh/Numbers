# Numbers

Nightly boot-time benchmarking for Qualcomm boards.

A **Linux coordinator** schedules runs, keeps history in Postgres, and serves a
dashboard. A **long-lived HTTP agent on each Windows bench host** owns the
hardware: the coordinator POSTs a job, the agent runs `bootbench.py` locally and
streams back status, logs, and artifacts.

**There is no AI in the loop.** Every number comes from the `bootbench` skill's
own regex parsing and arithmetic. The skill's one judgment field
(`optimization_possibilities`, free prose) is left `null` by automation and can
be filled in by hand afterwards.

```
                 Linux coordinator                     Windows bench host
    ┌──────────────────────────────────┐          ┌──────────────────────────┐
    │  cron ──▶ main.py tick           │          │  client/agent.py         │
    │             │                    │          │     (Session 1+)         │
    │             ▼                    │  HTTP    │        │                 │
    │          run table ◀── worker ───┼─────────▶│        ▼                 │
    │             │        (POST /jobs)│  :8765   │   bootbench.py           │
    │             ▼                    │          │        │                 │
    │          Postgres ◀── ingest ◀───┼──────────┤   TAC · PCAT · adb       │
    │             │        (artifacts) │          │        │                 │
    │             ▼                    │          └────────┼─────────────────┘
    │          Flask dashboard :8080   │                   ▼
    └──────────────────────────────────┘               IQ-9075 EVK
```

---

## Contents

- [Why two machines](#why-two-machines)
- [Why an agent and not SSH](#why-an-agent-and-not-ssh)
- [Repository layout](#repository-layout)
- [The client side — `client/agent.py`](#the-client-side--clientagentpy)
- [The server side — `server/`](#the-server-side--server)
- [Installing the client (Windows bench host)](#installing-the-client-windows-bench-host)
- [Installing the server (Linux coordinator)](#installing-the-server-linux-coordinator)
- [Running it](#running-it)
- [Which Windows machines the server triggers](#which-windows-machines-the-server-triggers)
- [Scheduling the daily run](#scheduling-the-daily-run)
- [Triggering a run by hand](#triggering-a-run-by-hand)
- [What the dashboard shows](#what-the-dashboard-shows)
- [Configuration reference](#configuration-reference)
- [Keeping `client/bootbench.py` in sync](#keeping-clientbootbenchpy-in-sync)
- [Testing](#testing)
- [Support caveats](#support-caveats)
- [Security posture](#security-posture)
- [Build status](#build-status)

---

## Why two machines

`bootbench.py` cannot run on Linux. Three hard Windows dependencies sit in the
hardware-control path:

| Where | What |
|---|---|
| `bootbench.py` `_open_tac()` | `comtypes.client.CreateObject("TACCOM.AlpacaTACServer")` — Alpaca TAC power and EDL control over **Windows COM**. No Linux equivalent. |
| `bootbench.py` `PCAT_EXE` | `PCAT.exe`, the EDL flashing tool |
| `bootbench.py` `_run_powershell()` | `powershell.exe`, for build discovery over SMB |

So the Linux box coordinates and the Windows host executes. The skill stays on
Windows and stays hand-runnable — nothing here takes that away.

The module still *imports* cleanly on Linux (its body defines only constants,
`main()` is `__main__`-guarded, and `serial`/`comtypes` are imported inside
functions). That is what lets the coordinator reuse the skill's **pure parser
functions** with no board attached — see
[`server/src/utils/bootbench_api.py`](server/src/utils/bootbench_api.py).

**There is exactly one implementation of boot-log parsing in this project.** A
second regex that is subtly wrong does not crash — it produces a *plausible*
number, and a plausible wrong number on a trend chart is worse than an
exception.

## Why an agent and not SSH

The agent exists to solve specific problems, not to be fashionable.

- **TAC COM needs an interactive session.** Out-of-process COM servers routinely
  refuse to start — or start in Session 0 with no USB/HID access — when the
  caller has no interactive desktop, which is exactly what `ssh exec` gives you.
  The agent is launched from the logged-on desktop, so `CreateObject` runs in the
  same session where the TAC demonstrably works when an engineer runs the skill
  by hand. `GET /healthz` asserts this rather than hoping: it reports
  `session_id` and a real `Get_Device_Count()`.
- **The job outlives the connection.** `bootbench.py` is a child of the
  long-lived agent, not of a network channel. A dropped request is a dropped
  *poll*, not a dropped job. Nothing can kill PCAT mid-write to a boot partition
  by closing a socket.
- **No shell, no quoting.** The agent builds an argv list and calls
  `subprocess.Popen(argv, shell=False)`. Metacharacter escaping, trailing
  backslashes, `%VAR%` expansion, and "which shell does Windows OpenSSH use" all
  stop being questions.
- **No path mapping.** Artifacts come back as HTTP GETs against a manifest, so
  there is no `C:\...` → POSIX translation anywhere.

Cost: one inbound port on the bench host, and the agent has to be running. Both
are visible on `/healthz` and in the dashboard.

> If lab policy forbids inbound connections to a bench host, the same agent flips
> to long-poll mode (`GET /jobs/next`, outbound only) with no change to the job,
> status, or artifact payloads. Not built; the protocol is kept
> direction-agnostic so it stays possible.

---

## Repository layout

```
bootbench-nightly/
  README.md                 this file
  .gitignore .gitattributes
  tools/
    sync_client.py          enforces client/bootbench.py == the skill's copy

  client/                   >>> THE ONLY DIRECTORY COPIED TO WINDOWS <<<
    agent.py                1289 lines, STANDARD LIBRARY ONLY
    bootbench.py            3110 lines, vendored from the skill bundle
    requirements.txt        pyserial + comtypes (for bootbench.py, not agent.py)
    agentconfig.json.example
    INSTALL.md              full bench-host procedure, firewall, troubleshooting
    docs/                   COMMAND_REFERENCE.md, DATA_MODEL.md (vendored)

  server/                   the Linux coordinator
    main.py                 one CLI for every mode
    requirements.txt
    config/                 server.yaml, devices.yaml, credentials.yaml
    deploy/                 systemd units, crontab, /etc/bootbench/env
    src/
      settings.py           server.yaml
      inventory.py          Device dataclass + the six validation rules
      db/                   schema.sql, migrations, pool, queries, ingest
      runner/               base.py (protocol), agent_client.py, local_fake.py
      utils/               config_loader, credential_manager, logger,
                            bootbench_api (the skill import shim)
      scheduler/            nightly.py decide(), retry.py, notify.py
      jobs/                 worker.py, gc.py
      web/                  app.py, views.py, charts.py, templates/
    tests/                  unittest only, 384 tests
```

**Hard rule: `client/` must never import from `server/`.** It is deployed
standalone to a machine that has only the skill's own dependencies. Adding a
bench host is "copy `client/`, write `agentconfig.json`, run
`install/setup-bench-host.ps1`" — no pip install for the agent itself, no
virtualenv.

The dependency asymmetry is deliberate:

| Side | Dependencies |
|---|---|
| `client/agent.py` | **standard library only** |
| `client/bootbench.py` | `pyserial`, `comtypes` |
| `server/` | `PyYAML`, `Jinja2`, `requests`, `psycopg[binary]`, `Flask`, `waitress`, `croniter` |

Python **3.10+** on both sides.

---

## The client side — `client/agent.py`

One stdlib-only `ThreadingHTTPServer`. Every request carries
`Authorization: Bearer <token>`, compared with `hmac.compare_digest`. Every
response is JSON except raw artifact bytes.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/healthz` | Liveness **and capability assertion** — `session_id`, `session_interactive`, `tac_device_count`, `pcat_present`, `adb_devices`, `artifact_root_free_gb`, `busy` |
| `GET` | `/latest-build?device_id=` | Shells `bootbench.py latest-build --json`. Touches the SMB share only — no serial port, no TAC, no adb. |
| `POST` | `/jobs` | Start a job. `202` + job record. **Idempotent on `job_id`** — a re-POST returns `200` and the existing record, never a second process. |
| `GET` | `/jobs` | All known jobs, newest first |
| `GET` | `/jobs/<id>` | State, pid, exit code, and the parsed `--json-status` document once present |
| `GET` | `/jobs/<id>/log?offset=N` | `{offset, chunk, size, eof}` — byte-offset tail |
| `GET` | `/jobs/<id>/artifacts` | Manifest: `[{relpath, size, sha256, kind}]` |
| `GET` | `/jobs/<id>/artifacts/<relpath>` | Raw bytes, path-traversal checked against the manifest |
| `POST` | `/jobs/<id>/cancel` | Request cancel — honored only at safe points |
| `POST` | `/recover` | `flash --recover` for a device stuck in EDL |

The coordinator sends **intent, never paths**:

```json
{"job_id": "42", "device_id": "iq-9075-evk-01", "stages": "all",
 "num_boots": 3, "boot_timeout": 480}
```

The agent composes the argv itself by merging that with its own config, so the
coordinator never learns a `C:` path:

```python
argv = [*shlex.split(cfg.python), str(cfg.bootbench), *stages,
        "--yes", "--non-interactive",
        "--target", dev["target"], "--com-port", dev["com_port"],
        "--tac-port", dev["tac_port"], "--adb-serial", dev["adb_serial"],
        "--num-boots", str(num_boots), "--boot-timeout", str(boot_timeout),
        "--share-root", cfg.share_root,
        "--boot-charts-dir", str(dev_dir / "Boot-Charts"),
        "--boot-logs-dir",   str(dev_dir / "Boot-Logs"),
        "--json-status",     str(job_dir / "status.json"),
        "--lock-file",       str(dev_dir / ".bootbench.lock"),
        "--cancel-file",     str(job_dir / "cancel")]
```

Other load-bearing behaviors: a **per-device lock** (`409` with the in-flight
`job_id` for a busy device); **restart adoption** (on startup it scans
`jobs/*/job.json`, re-adopts a live pid, finalizes a dead one from its
`status.json`, and marks it `orphaned` if there is none); and refusal of any
`device_id` not in its own `devices` map, so a misconfigured coordinator cannot
ask a bench host to drive a board it does not own.

### `client/bootbench.py`

The skill script, vendored. The automation surface added for this project is all
additive — every invocation documented in the skill's `SKILL.md` behaves
identically:

| Flag / stage | Purpose |
|---|---|
| `--json-status PATH` | Machine-readable status document, written from a `finally` block so it exists on success, on failure, and on Ctrl-C |
| `latest-build [--json]` | Device-free build probe — the whole reason a 15-minute scheduler tick is cheap |
| `--adb-serial`, `--com-port`, `--tac-port` | Per-device routing on a multi-board host |
| `--boot-charts-dir`, `--boot-logs-dir`, `--share-root` | Overridable output paths (this is how artifacts get out of OneDrive) |
| `--non-interactive` | A prompt with nobody to answer it becomes a clean usage error |
| `--lock-file PATH` | Stops a hand-run invocation and a nightly from interleaving and losing a run |
| `--cancel-file PATH` | Polled between boots and before flashing; mid-flash cancels are refused by design |
| `--revert-debug-cmdline` | Removes the debug kernel cmdline after the debug phase |

Plus a numeric `seconds` key on every recorded boot (the sub-component timings
used to exist only inside prose `note` strings) and an **exit-code taxonomy**:

```
0 OK   1 UNKNOWN   2 USAGE   10 BUILD_DISCOVERY   11 EDL   12 FLASH
13 SERIAL_LOGIN   14 BOOT_COLLECT   15 ADB   16 PARSE   17 RECORD
18 TAC   130 INTERRUPTED
```

The taxonomy is what makes retries safe to automate: `SERIAL_LOGIN`, `TAC`,
`EDL` and timeouts are retried, `ADB` is retried as `capture --resume-pull`
(the logs are already on the device — do not reflash and reboot six more
times), and `FLASH` is **never** retried because a partition may be mid-write.

---

## The server side — `server/`

| Module | Lines | What it is |
|---|---|---|
| [`main.py`](server/main.py) | 809 | The only entry point. Every mode is a subcommand. |
| [`src/inventory.py`](server/src/inventory.py) | 298 | `Device` dataclass, `load_inventory()`, and the six validation rules |
| [`src/settings.py`](server/src/settings.py) | 98 | `server.yaml`; every key has a default so a fresh checkout runs |
| [`src/db/schema.sql`](server/src/db/schema.sql) | 347 | `device`, `run`, `boot`, `scheduler_state`, `scheduler_decision`, two views |
| [`src/db/queries.py`](server/src/db/queries.py) | 776 | Every SQL statement, in one place |
| [`src/db/ingest.py`](server/src/db/ingest.py) | 540 | Status document → rows, plus backfill and log-reparse repair |
| [`src/db/migrate.py`](server/src/db/migrate.py) | 152 | Applies and records migrations, idempotently |
| [`src/db/pool.py`](server/src/db/pool.py) | 183 | Connections, transactions, the per-device advisory lock |
| [`src/runner/base.py`](server/src/runner/base.py) | 73 | The `Runner` protocol — the contract both backends honor |
| [`src/runner/agent_client.py`](server/src/runner/agent_client.py) | 198 | Real HTTP against a bench host; bounded retries on connection errors only |
| [`src/runner/local_fake.py`](server/src/runner/local_fake.py) | 227 | Same protocol, no hardware — selected by `runner_backend: local_fake` |
| [`src/runner/artifacts.py`](server/src/runner/artifacts.py) | 193 | Which artifacts to fetch, where they may land, sha256 on arrival |
| [`src/scheduler/nightly.py`](server/src/scheduler/nightly.py) | 454 | `decide()` — the ten-step, cheapest-first nightly decision |
| [`src/scheduler/retry.py`](server/src/scheduler/retry.py) | 144 | Which failures earn another forty minutes of bench time |
| [`src/scheduler/notify.py`](server/src/scheduler/notify.py) | 375 | SMTP and webhook alerts, deduplicated with a cooldown |
| [`src/jobs/worker.py`](server/src/jobs/worker.py) | 599 | Claim → submit → poll → mirror → fetch → ingest → retry |
| [`src/jobs/gc.py`](server/src/jobs/gc.py) | 170 | Retention, on both the rows and the mirrored files |
| [`src/web/app.py`](server/src/web/app.py) | 212 | The Flask factory, the template filters, the error pages |
| [`src/web/views.py`](server/src/web/views.py) | 619 | Every route. One query, one shaping call, one render |
| [`src/web/charts.py`](server/src/web/charts.py) | 291 | Deltas, series, SVG geometry, sparklines — all pure functions |
| [`src/utils/bootbench_api.py`](server/src/utils/bootbench_api.py) | 212 | The skill import shim, with a denylist of names that must never be reached |

### Data model, in one paragraph

A `run` row **is** the job queue entry: the web app INSERTs it `queued`, the
worker claims it with `FOR UPDATE SKIP LOCKED`, and `run_id == agent_job_id`, so
one number spans the dashboard URL, the agent's job directory, and the status
filename. A `boot` row exists for **every boot attempted** (all 2N, including
ones that failed to parse), not just the two the skill's own JSON displays.
Timings are `NUMERIC(10,3)` rather than `DOUBLE PRECISION` — boot times are
exact 3-decimal seconds, so averages, percentiles and build-over-build deltas
accumulate no float artifacts and a `-0.000` delta cannot appear. There is
deliberately **no** unique constraint on `(device_id, build_number)`: re-running
a build is a legitimate thing to want, so "already benchmarked?" is a query, not
a constraint.

### CLI

| Command | Does | Status |
|---|---|---|
| `validate-config` | Parse and print the resolved device table | done |
| `agent-ping` | `GET /healthz` per agent, with a verdict | done |
| `latest-build` | Ask an agent what the newest build is (no hardware) | done |
| `initdb` | Apply migrations, project `devices.yaml` into `device` | done |
| `ingest` | Load one `--json-status` document into a run | done |
| `backfill` | Load history from the skill's own `bootchart-data-<slug>.json` | done |
| `worker` | Claim queued runs and drive them | done |
| `serve` | The dashboard | done |
| `tick` / `run-nightly` | Evaluate each device's schedule, enqueue retries, send alerts | done |
| `gc` | Retention: prune run directories and rows | done |

Unbuilt commands are registered and exit `2` naming the phase they arrive in, so
`--help` describes the finished shape of the tool rather than growing one
command at a time.

Exit codes: `0` ok, `1` error, `2` usage, `3` bad config, `4` agent unreachable.
`3` and `4` are distinct on purpose — a cron wrapper should treat "the inventory
is wrong" and "the bench host is switched off" differently.

---

## Installing the client (Windows bench host)

Full procedure, troubleshooting table, and firewall rules:
**[`client/INSTALL.md`](client/INSTALL.md)**. The short version:

```bat
rem 1. Copy this one directory. Nothing else from the repo is needed.
xcopy /E /I client C:\bench\client

rem 2. Dependencies -- for bootbench.py only; agent.py needs none.
py -3 -m pip install -r C:\bench\client\requirements.txt

rem 3. Config. No secret goes in this file.
copy C:\bench\client\agentconfig.json.example C:\bench\client\agentconfig.json
notepad C:\bench\client\agentconfig.json

rem 4. The shared token, as a USER environment variable.
setx BOOTBENCH_AGENT_TOKEN "<token from the coordinator>"
rem  ...then log off and back on, or it will not be in the agent's environment.

rem 5. Verify WITHOUT opening a port.
py -3 C:\bench\client\agent.py --config C:\bench\client\agentconfig.json --check
```

`--check` prints the capability probe. What you want:

```json
{
  "bootbench_exists": true,
  "pcat_present": true,
  "session_id": 1,
  "session_interactive": true,
  "tac_device_count": 1,
  "adb_devices": ["1a2b3c4d5e"],
  "token_set": true
}
```

`tac_device_count: 1` is the assertion that matters — it means a real
`Get_Device_Count()` call succeeded, so out-of-process COM works from this
session and a flash can power-cycle the board.

Then make it start — and keep running — with one elevated script:

```bat
powershell -NoProfile -ExecutionPolicy Bypass ^
  -File C:\bench\client\install\setup-bench-host.ps1 -CoordinatorIp <coordinator-ip>
```

That registers a scheduled task which launches the agent at logon and
re-checks it every 5 minutes, stops the host sleeping, blocks unattended
Windows Update reboots, and adds the one inbound firewall rule scoped to the
coordinator. It leaves **automatic logon** to you, as a deliberate one-time
manual step — see [`client/INSTALL.md`](client/INSTALL.md) § *Surviving the
login screen*.

> ### Do not run the agent as a Windows Service
>
> A Windows Service runs in **Session 0**, which has no interactive window
> station — and that is precisely the condition under which out-of-process COM
> refuses to start. **The TAC will not work.** The scheduled task is registered
> with logon type *Interactive* ("run only when the user is logged on") because
> that runs in Session 1+, the same context in which the skill demonstrably
> works when an engineer runs it by hand. `/healthz` reports `session_id` on
> every call and the dashboard warns loudly when it is `0`, so if someone later
> "improves" this into a proper service, that is how you will find out.

> ### A locked screen is fine; a login screen is not
>
> These get conflated, and only one of them breaks anything:
>
> | | Session | Agent |
> |---|---|---|
> | Screen locked, idle timeout, fast user switch | survives | keeps running, COM intact |
> | Logged off or rebooted | gone | not running at all |
>
> Locking keeps the window station and the agent's COM access, so it needs no
> handling. The second row does, and the answer is automatic logon, so a reboot
> lands in a real interactive session. Use a dedicated low-privilege lab
> account, and then actually test a reboot — it is the step people skip.
>
> Without automatic logon the system degrades visibly rather than lying: the
> tick records `agent_unhealthy`, nothing is enqueued, the dashboard shows the
> device unreachable, and four consecutive such ticks alert. A reboot *during* a
> job comes back `orphaned`, which the coordinator fails fast and retries once
> rather than waiting out the job timeout.


## Installing the server (Linux coordinator)

```bash
sudo install -d -o bootbench -g bootbench /opt/bootbench
sudo -u bootbench git clone <this repo> /opt/bootbench
cd /opt/bootbench/server

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Config
cp config/devices.yaml.example config/devices.yaml   # then edit
cp config/credentials.yaml.example config/credentials.yaml
chmod 0600 config/credentials.yaml                   # or use /etc/bootbench/env

.venv/bin/python main.py validate-config             # resolved device table
```

Postgres — the preferred setup keeps the password out of every file this repo
controls:

```bash
sudo -u postgres createuser bootbench
sudo -u postgres createdb -O bootbench bootbench
# peer auth over the unix socket; leave BOOTBENCH_DB_DSN unset entirely
sudo -u bootbench PGDATABASE=bootbench /opt/bootbench/server/.venv/bin/python main.py initdb
```

Then install the units, the environment file, and the single crontab line from
[`server/deploy/`](server/deploy):

```bash
sudo install -d -m 0750 -o bootbench -g bootbench /etc/bootbench
sudo install -m 0600 -o bootbench -g bootbench deploy/env.example /etc/bootbench/env
sudo $EDITOR /etc/bootbench/env            # set the two tokens

sudo install -d -o bootbench -g bootbench /var/lib/bootbench /var/log/bootbench
sudo install -m 0644 deploy/bootbench-web.service deploy/bootbench-worker.service \
     /etc/systemd/system/
sudo install -m 0644 deploy/bootbench.cron /etc/cron.d/bootbench

sudo systemctl daemon-reload
sudo systemctl enable --now bootbench-worker bootbench-web
```

**The worker is a separate process from the web app, on purpose.** A run takes
roughly 40 minutes; restarting a dashboard must not interrupt one. The web app
only ever INSERTs `queued` rows, and `bootbench.cron` holds **one** `*/15` tick
line for *all* devices — each device's own `schedule:` is evaluated inside the
tick, so adding a board is a YAML block and nothing else.

Four checks say the coordinator is up, in increasing order of what they prove:

```bash
systemctl status bootbench-web bootbench-worker       # both active
curl -s localhost:8080/healthz                        # {"ok": true, ...}
.venv/bin/python main.py agent-ping                   # every bench host answers
.venv/bin/python main.py tick --dry-run               # what tonight would do
```

`/healthz` is the one to watch: it reports the database, the inventory parse,
*and* the worker's heartbeat. A `queued` run that never starts is almost always
a worker that is not running, and that is the field which says so.

### Requirements files

Two, because the two sides genuinely differ:

- **[`client/requirements.txt`](client/requirements.txt)** — `pyserial`,
  `comtypes`. Both are for `bootbench.py`. **`agent.py` needs nothing**, which
  is a property worth keeping: there is no virtualenv to go stale on a lab
  machine nobody logs into for three months. If you find yourself wanting a
  package in `agent.py`, that is the signal the logic belongs on the Linux side.
- **[`server/requirements.txt`](server/requirements.txt)** — seven packages,
  each with its rationale in the file. `psycopg[binary]` ships its own libpq, so
  there is no `libpq-dev` and no compiler in the install path.

---

## Running it

```bash
cd /opt/bootbench/server

# After every inventory edit. Reports EVERY problem, not just the first.
.venv/bin/python main.py validate-config

# Is the bench host alive and capable?
.venv/bin/python main.py agent-ping --device iq-9075-evk-01

# What is the newest build? (no hardware touched)
.venv/bin/python main.py latest-build --device iq-9075-evk-01

# Database
.venv/bin/python main.py initdb                      # idempotent

# Load history from runs an engineer already did by hand
.venv/bin/python main.py backfill --device iq-9075-evk-01 \
    --json "/mnt/bench/Boot-Charts/bootchart-data-iq9075evk.json"

# Load one finished run. --run-id ingests into an existing run; omit it and
# --device creates one, which is how you load a hand-run capture.
.venv/bin/python main.py ingest --status /path/to/status.json \
    --device iq-9075-evk-01
```

Every write command takes `--dry-run`, which parses, writes, and rolls back — so
a new status document can be checked against the real schema before it is kept.

Then `psql` works on real history before any orchestration exists:

```sql
SELECT build_number, phase, boot_index, total_multiuser_s
FROM boot_trend
WHERE device_id = 'iq-9075-evk-01'
  AND phase = 'default' AND boot_index = 1   -- the comparable series; see caveats
  AND reflashed IS NOT FALSE
  AND cmdline_has_debug IS NOT TRUE
ORDER BY started_at DESC LIMIT 10;
```

### Adding a device

A new block in `server/config/devices.yaml`. No code change, no crontab change,
no migration:

```yaml
devices:
  iq-8275-evk-01:
    enabled: true
    target: iq-8275-evk
    agent_url: "http://bench-win-02:8765"
    com_port: COM9
    tac_port: VTP9
    adb_serial: "aabbccdd"
```

...plus the same `device_id` in that bench host's `agentconfig.json`, since the
agent refuses any device it does not own.

If **two enabled devices share one `agent_url`**, all of them must pin
`com_port`, `tac_port`, and `adb_serial`. This is not cosmetic:
`find_console_port()` probes every COM port on the host and would open the other
board's live console mid-capture, the TAC call raises on an ambiguous device
count, and unqualified `adb` picks a board arbitrarily. `validate-config`
enforces it.

---

## Which Windows machines the server triggers

**`server/config/devices.yaml` is the whole answer.** Each device names the
bench host that owns it, by URL. There is no separate host list and no
discovery: a device the inventory does not mention is never contacted, and a
bench host no device points at is never spoken to.

```yaml
defaults:
  num_boots: 3
  boot_timeout: 480
  schedule: "0 1 * * *"
  stages: "all"
  fetch_full_logs: false
  max_retries: 1

devices:
  iq-9075-evk-01:                       # <- the primary key, everywhere
    enabled: true
    target: iq-9075-evk
    agent_url: "http://bench-win-01:8765"   # <- WHICH WINDOWS MACHINE
    com_port: COM7
    tac_port: VTP8
    adb_serial: "1a2b3c4d5e"

  iq-8275-evk-01:
    enabled: true
    target: iq-8275-evk
    agent_url: "http://bench-win-02:8765"   # a second bench host, same file
    schedule: "0 2 * * *"                   # staggered; nothing is shared
```

Two boards on **one** host is the same file with the same `agent_url` twice —
and then `com_port`, `tac_port`, and `adb_serial` become mandatory on both
(see [Adding a device](#adding-a-device)).

Every address must also hold up on the other side. The agent keeps its **own**
`devices` map in `agentconfig.json` and returns `404` for any `device_id` it
does not list, so a coordinator typo cannot make `bench-win-02` drive a board
it does not own. Both halves are checked without touching hardware:

```bash
.venv/bin/python main.py validate-config     # URLs parse, rules hold, no duplicates
.venv/bin/python main.py agent-ping           # every agent in the inventory
.venv/bin/python main.py agent-ping --device iq-9075-evk-01   # just one
```

`agent-ping` is the one to run after any inventory edit. It prints each host's
`session_id`, `tac_device_count`, `adb_devices`, free disk, and which
`device_id`s that host admits to owning — which is how a mismatch between the
two files surfaces as a line of output rather than as a failed flash at 01:00.

---

## Scheduling the daily run

Three things have to line up. Only the first one changes per device.

**1. The per-device cron expression, in `devices.yaml`.** Five standard fields,
in the coordinator's timezone:

```yaml
defaults:
  schedule: "0 1 * * *"        # 01:00 every day — the default for every device

devices:
  iq-8275-evk-01:
    schedule: "0 2 * * *"      # this one at 02:00; overrides the default
```

**2. One system crontab entry, for all devices**, from
[`server/deploy/bootbench.cron`](server/deploy/bootbench.cron):

```cron
TZ=Asia/Kolkata
MAILTO=""
PATH=/usr/bin:/bin

*/15 * * * * bootbench cd /opt/bootbench/server && .venv/bin/python main.py tick >> /var/log/bootbench/tick.log 2>&1
17 4  * * * bootbench cd /opt/bootbench/server && .venv/bin/python main.py gc   >> /var/log/bootbench/gc.log   2>&1
```

```bash
sudo install -m 0644 server/deploy/bootbench.cron /etc/cron.d/bootbench
```

**3. The worker, running.** `tick` only *enqueues*; `bootbench-worker.service`
is what actually dispatches to a bench host. A tick with no worker leaves rows
sitting in `queued` forever — the dashboard's `/healthz` reports a stale
heartbeat when that happens.

### Why a 15-minute tick rather than one cron line per device

`tick` does not ask "is it 01:00 right now". It asks croniter **"was there a
fire time between `last_tick_at` and now?"** — so a window missed because the
coordinator was rebooting, or because the build had not landed yet, is still
caught on a later tick that same day. A literal `0 1 * * *` invocation gets one
attempt per day and silently loses the night if anything is briefly wrong.

It is also why adding a board never touches the crontab: one tick iterates
every enabled device and evaluates each one's own `schedule:`.

The tick is cheap by construction — **no network until step 6** of ten:

| # | Condition | Outcome |
|---|---|---|
| 1 | not `enabled` | `skipped/disabled` |
| 2 | a `queued`/`running` run exists | `skipped/overlap` |
| 3 | last run `unreachable` and unacknowledged | **`blocked`** — a human must look |
| 4 | a retry is due | `enqueued/retry_<stage>` |
| 5 | no fire time in `(last_tick, now]` | `skipped/not_due` |
| 6 | `GET /healthz` fails, session 0, or TAC count 0 | `error/agent_unhealthy` |
| 7 | `GET /latest-build` fails | `error/discovery_failed` |
| 8 | the target's image has not landed | `skipped/image_not_ready` |
| 9 | build number unparseable (not a master nightly) | `skipped/unparseable_build` |
| 10 | build already benchmarked | `skipped/already_benchmarked` |
| — | otherwise | **`enqueued/new_build`** |

Every one of those writes a `scheduler_decision` row, which is what answers
*"why didn't it run last night?"* — on the **Schedule** page or in SQL.

### Verifying the schedule without waiting a day

```bash
# Decide for every device and print it. Writes nothing at all.
.venv/bin/python main.py tick --dry-run

# Decide for one device, verbosely.
.venv/bin/python main.py --log-level DEBUG tick --dry-run --device iq-9075-evk-01

# Pretend the build is new even if it has been benchmarked (still writes).
.venv/bin/python main.py tick --force --device iq-9075-evk-01
```

For a first day in production, the plan's advice is worth following: run the
tick at `*/20` with `--force`, read the decision log, then switch to
`0 1 * * *` and watch two real nights.

### Changing the time, and the timezone trap

Edit `schedule:` in `devices.yaml` — no restart, the inventory is re-read when
its mtime changes.

**Pin `TZ` in the crontab *and* in both systemd units.** They are already set to
`Asia/Kolkata` in [`server/deploy/`](server/deploy). If the coordinator runs in
UTC while the build share publishes on local time, a DST shift moves the
nightly relative to the build and the first few runs quietly benchmark
yesterday's build.

---

## Triggering a run by hand

Three ways in, all of which end as the same `queued` row in the same table. A
manual run is not a special path — it is the same worker, the same agent call,
the same ingestion.

**From the dashboard.** *Run now*, on a device card on `/` or on the device
page. The button POSTs, the server INSERTs one row inside one transaction, and
you land on `/runs/<id>` with a live log. It returns in milliseconds and never
talks to the bench host, so pressing it against a logged-out Windows box
cannot hang the page — the run just sits `queued` until the agent answers.

The first click prompts for the `trigger_token` and keeps it in `localStorage`.
With no token configured the dashboard is readable but every POST returns
`401`: fail-closed, deliberately.

**With `curl`**, which is what a CI job or a `git push` hook would use:

```bash
curl -X POST -H "X-Bootbench-Token: $BOOTBENCH_TRIGGER_TOKEN" \
     http://coordinator:8080/api/devices/iq-9075-evk-01/trigger
# -> 202 {"run_id": 57, "device_id": "iq-9075-evk-01", "status": "queued"}

# Only the capture stages, on a board that is already flashed:
curl -X POST -H "X-Bootbench-Token: $TOK" -d 'stages=capture' \
     http://coordinator:8080/api/devices/iq-9075-evk-01/trigger
```

A `409` means a run is already in flight for that device and names it. That is
the overlap guard, and it is enforced in three independent places: the INSERT
checks for an active run, the worker holds a Postgres advisory lock per device,
and the agent itself returns `409` for a busy board.

**From the CLI**, on the coordinator, when there is no browser:

```bash
.venv/bin/python main.py tick --force --device iq-9075-evk-01   # enqueue now
.venv/bin/python main.py worker --once                          # drive it in the foreground
```

`worker --once` is the one to use while setting a bench host up: it claims a
single run, streams the agent's log to your terminal, and exits — no service to
restart, nothing left running.

**Cancelling.** `POST /api/runs/<id>/cancel`, or the button on the run page. A
`queued` run ends immediately. A `running` one gets a flag the agent honours
**only at safe points** — between boots, or before the flash begins. It refuses
mid-flash, and the UI says why: interrupting PCAT while it writes a boot
partition is damage, not a cancellation.

---

## What the dashboard shows

```
http://coordinator:8080/
```

| Route | What is on it |
|---|---|
| `/` | One card per device: name, target, bench host, status, build number, timestamp, duration, `multi-user.target` seconds, the delta against the previous comparable build, and a sparkline. *Run now*. |
| `/devices/<id>` | Trend chart by build, run history, the scheduler's recent decisions for this device, and live agent health (session id, TAC count). |
| `/runs` | **Runs & logs** — every dispatch ever, filterable by device and status, with the mirrored file count and byte total per run. |
| `/runs/<id>` | One run in full: see below. |
| `/runs/<id>/files/<relpath>` | Any single mirrored artifact. |
| `/runs/<id>/log` | The whole `runner.log` as `text/plain`. |
| `/runs/<id>/report` | The skill's **own** HTML report, served untouched. |
| `/devices/<id>/report` | Redirect to the latest successful run's report. |
| `/schedule` | The full decision log across all devices — the "why didn't it run?" page. |
| `/healthz` | JSON: database, worker heartbeat, inventory parse. Renders no template, so it answers even when Jinja2 or Postgres is broken. |

### The run page

Everything recorded about one dispatch, which is the provenance the request
asked for:

- **device name**, target, and the bench host that ran it (`host`, `agent_url`,
  and the agent's own job id);
- **build id** (`build_number`), **build folder**, **build path**, and the
  `share_root` it was discovered under — stored per run, so a build moving on
  the share later cannot rewrite history;
- **timestamps**: `queued`, `started`, `finished`, each to the second, plus the
  duration and the trigger source (`cron`, `manual`, `retry`, `backfill`) and
  who triggered it;
- status, exit code, failure stage, error class and message;
- **every boot attempted** — all 2N, not just the two the skill's own report
  displays — with each one's metrics, `parse_error`, and whether its own
  `/proc/cmdline` carried the debug params;
- **the live log**, tailed by byte offset while the run is in flight;
- **Transferred files** — every artifact mirrored for this run, each a link.

### The log inventory

`/runs` is the inventory of everything that came back. Per run it reports the
file count and total bytes **walked from disk at request time**, not read from
a column: a run whose directory `main.py gc` has pruned keeps its row and its
numbers but reports no files. A stored count would keep advertising 71 files
and hand out links that 404.

On the coordinator the layout is flat and predictable:

```
<artifact_root>/runs/<run_id>/
    runner.log                 mirrored from the agent, byte for byte
    status.json                the --json-status document, verbatim
    bootchart-overview-*.html  the skill's own report
    bootchart-data-*.json      the skill's own 30-run history
    Boot-Logs/<target>/<build>/...
        <timestamp>.txt                      per-boot human-readable backup
        Logs-default-1/dmesg.txt, ...        the 7 files the parsers read
```

`<run_id>` is the same number as the dashboard URL, the agent's job directory
on the bench host, and `agent_job_id` in the database — one identifier end to
end, so `ls /var/lib/bootbench/runs/57` needs no lookup.

Everything is fetched **even when the run failed**: a flash failure's log and
status document are exactly what you want at 09:00. `.txt`, `.log` and `.json`
open in the browser; anything else downloads rather than rendering, because
HTML and SVG arrive from a bench host and must not run script on the
dashboard's origin.

The artifact set is selective by default — `fetch_full_logs: false` skips
`Boot_Trace.txt`, `proc_config.txt`, `plot_systemd.svg` and `lsmod.txt`, which
are tens of megabytes per night across six boots. Set it per device when you
are actually debugging that board.

In SQL, the same inventory:

```sql
SELECT run_id, device_id, build_number, build_folder, build_path,
       queued_at, started_at, finished_at, status, artifact_dir
FROM run
ORDER BY queued_at DESC
LIMIT 20;
```

---

## Configuration reference

Four files. The split is deliberate: **the coordinator declares *what* to run;
the agent knows *where* things live.** No `C:` path is ever server-side config.

### 1. `client/agentconfig.json` — this bench host's filesystem

| Key | Default | Changes what |
|---|---|---|
| `bind` | `127.0.0.1` | Listen address. Must be reachable from the coordinator, so in practice `0.0.0.0` plus the firewall rule. |
| `port` | `8765` | Listen port. Must match `agent_url`. |
| `token_env` | `BOOTBENCH_AGENT_TOKEN` | **Which environment variable holds the bearer token.** The token itself is never written to this file. |
| `python` | `py -3` | Interpreter used to launch `bootbench.py`. Split with `shlex`, so `py -3` and a full `python.exe` path both work. |
| `bootbench` | *required* | Path to `bootbench.py`. Point it at the copy in this directory — a separate skill checkout means the coordinator parses with one vintage of the regexes while this host produced the logs with another, and the symptom is a wrong-but-plausible number rather than an error. |
| `pcat_exe` | the standard install path | Only used by the `/healthz` `pcat_present` probe. |
| `share_root` | *required* | UNC path to the nightly build share. The only place it is configured. |
| `artifact_root` | *required* | Where `Boot-Charts`, `Boot-Logs`, and per-job directories live. **Rejected at startup if the path contains `OneDrive`.** |
| `keep_jobs` | `200` | How many per-job directories to retain. Does **not** trim `Boot-Logs`. |
| `devices` | *required* | Map of `device_id` → `{target, com_port, tac_port, adb_serial}`. **Any `device_id` not listed here is refused**, so a misconfigured coordinator cannot drive a board this host does not own. `com_port`/`tac_port`/`adb_serial` are optional on a single-board host and required once two boards share one. |

### 2. `server/config/devices.yaml` — the inventory

A **map keyed by device id**, not a list (the stdlib fallback YAML parser cannot
read a list of dicts, and the key becomes a natural primary key). `defaults:`
applies to every device; any key can be overridden per device.

| Key | Default | Changes what |
|---|---|---|
| `agent_url` | — | Which bench host runs this device. |
| `enabled` | `true` | `false` ⇒ the scheduler records `skipped/disabled` and the dashboard greys it out. History is kept. |
| `target` | *required* | The skill's `--target`. Also how `build_folder` is resolved — it is **not** derivable from `build_path`. |
| `num_boots` | `3` | Boots **per phase**, so `3` means six boots and roughly 40 minutes. |
| `boot_timeout` | `480` | Seconds to wait for one boot. Feeds the coordinator's overall job timeout. |
| `schedule` | `0 1 * * *` | Standard cron, evaluated **per device** with croniter against the last recorded tick — so a window missed because the coordinator was down is still caught later the same day instead of silently skipped. |
| `stages` | `all` | **Leave it.** See the `rmtfs` caveat below — a bare `capture` produces numbers that are not comparable with the run before them. |
| `fetch_full_logs` | `false` | Pull `Boot_Trace.txt`, `plot_systemd.svg`, `proc_config.txt`, `lsmod.txt` too. Tens of MB a night, and nothing parses them. |
| `max_retries` | `1` | Retries for *transient* outcomes only; `FLASH`, `PARSE`, `RECORD` and `USAGE` are never retried. |
| `com_port`, `tac_port`, `adb_serial` | — | Per-board routing. Optional on a single-board host, **required** once two boards share one `agent_url`. |
| `notes` | — | Free text, shown on the device page. Put the rack and slot here. |

### 3. `server/config/server.yaml` — coordinator behavior

Flat and scalar-only, because the stdlib fallback parser cannot read lists and
truncates anything after a `#`. Every key has a default, so a fresh checkout
runs.

| Key | Default | Changes what |
|---|---|---|
| `artifact_root` | `/var/lib/bootbench/artifacts` | Coordinator-side mirror of logs and artifacts, so `/runs/<id>/log` keeps working after the agent prunes its own job dirs. |
| `log_file`, `log_level` | `INFO` | Coordinator logging. |
| `runner_backend` | `agent` | `agent` talks real HTTP to a bench host; `local_fake` drives an in-process fake honoring the same `Runner` protocol — the whole pipeline, end to end, with no hardware. |
| `poll_interval` | `5` | Seconds between `GET /jobs/<id>` while a job runs. |
| `claim_interval` | `10` | Seconds between queue polls when idle. |
| `agent_backoff_max` | `120` | Cap on backoff while an agent is unreachable. An unreachable agent mid-job is **not** an emergency — the job on the bench host is unaffected. |
| `bind`, `port` | `127.0.0.1:8080` | The dashboard. **Not internet-facing.** |
| `keep_days` | `90` | `main.py gc` retention. Nothing else trims anything. |
| `keep_runs_per_device` | `400` | Same. |
| `timezone` | `Asia/Kolkata` | **Pin this here *and* in the crontab *and* in both systemd units.** croniter reads local time; disagreement moves the nightly relative to when the build share publishes, and shows up as "it ran before the build existed". |
| `retry_delay_minutes` | `30` | Delay before a retry. |
| `stale_success_days` | `3` | Alert if a device has had no successful run in this long. |
| `bootbench_path` | `""` (search) | Which `bootbench.py` the coordinator imports parsers from. Empty searches `client/bootbench.py` first. `validate-config` prints what it resolved — silently parsing with a different vintage than the bench host runs is hard to spot from the numbers alone. |

### 4. Secrets — `server/config/credentials.yaml` or the environment

Resolution order is `credentials.yaml` → environment → warn. The manager
**never raises**: a missing value is a warning plus `None`, and the consumer
decides whether that is fatal.

| Value | Env var | Notes |
|---|---|---|
| `db_dsn` | `BOOTBENCH_DB_DSN` | **Leaving it empty is the preferred setup, not a fallback.** An empty DSN makes libpq read `PGHOST`/`PGUSER`/`~/.pgpass`, which keeps the password out of this process entirely. |
| `trigger_token` | `BOOTBENCH_TRIGGER_TOKEN` | Required on every POST to the web app. Without it, GETs still work — the dashboard is readable but not actionable. |
| `agent_token` | `BOOTBENCH_AGENT_TOKEN` | Must match the agent's own environment. Per-host override: `agent_token__bench_win_01` / `BOOTBENCH_AGENT_TOKEN__BENCH_WIN_01`. |
| `smtp_*`, `notify_to`, `notify_webhook` | `BOOTBENCH_SMTP_*`, … | Optional. Absent means no email, which is not an error. |

Comma-separated strings, never YAML lists — credentials are the last file you
want failing to load for a subtle reason.

---

## Keeping `client/bootbench.py` in sync

`client/bootbench.py` is a **copy** of the skill's `scripts/bootbench.py`. That
duplication is a deliberate trade: it is what makes `client/` deployable by file
copy, with no skill checkout and no `sys.path` games on the bench host. The
alternatives both lose — a symlink is unreliable across Windows and git, and
keeping only the skill's copy breaks "copy the directory and go".

So the duplication is **enforced rather than hoped for**:

```bash
python tools/sync_client.py --check          # exit 1 on drift
python tools/sync_client.py --check --diff   # show what differs
python tools/sync_client.py                  # copy skill -> client/
```

**Direction of truth is the skill bundle.** Edit the skill, then sync.
`tests/test_client_sync.py` runs `--check` as part of the suite and skips
cleanly when the skill bundle is not present, so an extracted standalone
checkout still passes. The comparison is on **bytes** — a line-ending change is
a real difference to `git diff` and to the golden-file tests.

Vendored alongside it: `client/docs/COMMAND_REFERENCE.md` and
`client/docs/DATA_MODEL.md`.

---

## Testing

`unittest` only. No hardware needed for any of it.

```bash
cd server
python -m unittest discover tests -v        # 384 tests

python -m unittest tests.test_agent               # real agent, stub bootbench
python -m unittest tests.test_inventory           # all six validation rules
python -m unittest tests.test_ingest              # status doc -> rows
python -m unittest tests.test_bootbench_golden    # rendered HTML, byte-identical
python -m unittest tests.test_client_sync         # the vendored copy matches
```

`tests/test_agent.py` boots a genuine `agent.py` on an ephemeral port against
`tests/fixtures/stub_bootbench.py`, so the real HTTP protocol — auth, idempotent
submit, `409` on a busy device, byte-offset tailing, sha256 manifests, traversal
refusal, cancel, restart adoption — is covered on Linux with no device, no
Windows, and no TAC.

`tests/test_db_live.py` needs a real Postgres and **skips** unless
`BOOTBENCH_TEST_DSN` is set. Each test runs in a rolled-back transaction:

```bash
BOOTBENCH_TEST_DSN=postgresql:///bootbench_test python -m unittest tests.test_db_live
```

---

## Support caveats

Read this section before trusting a number.

**The nightly must always run `stages: all`.** The skill's embedded collect
script runs `systemctl disable rmtfs.service` **and** `systemctl mask
rmtfs.service` on every boot, and masking is **persistent**. Within one run,
`default-1` boots with `rmtfs` enabled while `default-2` and `default-3` boot
with it masked — those are not the same system, and the step change looks
exactly like a real regression. A bare `capture` with no reflash starts from an
already-masked system and is not comparable with the run before it. Hence
`boot.phase` and `boot.boot_index` are first-class columns, the default trend
series is `phase='default', boot_index=1` **only**, and `run.reflashed` is
recorded so non-reflashed runs can be badged and excluded. Someone will
eventually try to "optimize" the nightly by skipping the flash.

**The debug kernel cmdline is never reverted.** `ensure_debug_cmdline_params`
appends `initcall_debug`, `log_buf_len=4M`, and `systemd.log_level=debug`, and
nothing removes them — so after one run the board boots debug-y forever, and
`systemd.log_level=debug` measurably slows boot. Always flashing is what saves
us, but we do not rely on it: the collect script already writes
`/data/Logs/kernel_cmdline.txt` (that boot's own `/proc/cmdline`) and the skill
simply never read it. One extra read gives `boot.kernel_cmdline` and
`boot.cmdline_has_debug`, so a mislabelled "default" boot is excluded from the
trend automatically and the history cannot silently lie.

**Exit 0 does not mean a complete run.** `bootbench.py` exits `0` having
recorded *at least one* boot — that is the deliberate partial-results design,
and it is a large reliability win over losing forty minutes of work to one
unreadable file. The coordinator's `derive_status()` is what distinguishes
`success` from `partial`, and a `partial` run is reported on a quieter alert
channel rather than ignored. A silently degrading device is the exact failure
mode this system exists to catch.

**Only `master` nightlies are visible.** `BUILD_NAME_RE` requires
`_Nightly_Build_master_(\d+)$`, so branch and release builds are invisible.
`run.build_number` is nullable and the scheduler's `unparseable_build` reason
exists to make that explicit rather than silent.

**`target` cannot be derived from `build_path`.** `cmd_flash` hands off the
shared `...\performance` directory, not the per-target directory, so it is
identical for every target from the same nightly. `target` comes from the
inventory and the status document only.

**Cancel cannot interrupt a flash.** It is honored between boots or before the
flash begins, and refused mid-write. Interrupting PCAT mid-write to a boot
partition is not a cancellation, it is damage. The UI has to say so.

**Keep artifacts out of OneDrive.** The skill's `BOOT_LOGS_DIR` is
`__file__`-anchored and unbounded. Nightly multi-MB log trees in a synced folder
cause sync storms, and OneDrive's file locking can make `adb pull` fail
mid-write with what looks like an adb bug. The agent refuses an `artifact_root`
containing `OneDrive` at startup and says why.

**Disk grows on both sides and nothing trims it yet.** Twelve files × six boots
— including a trace dump and a decompressed kernel config — plus six `.txt`
backups, per device per night. The skill's `MAX_RUNS = 30` trims the chart JSON
and nothing trims `Boot-Logs/`. `/healthz` reports `artifact_root_free_gb` so
this is visible before it bites; `gc` arrives in Phase 9.

**Sub-component timings are NULL for history predating the `seconds` key.**
Backfill recovers the six headline `METRIC_ROWS` values through the skill's own
`parse_seconds()`, but the ten sub-components (`firmware`, `loader`,
`userspace`, `init_exec`, `epoch_advanced`, `systemd_running`, `sysinit_target`,
`cc_multiuser`, `grand_total`, `sat_total`) existed only inside prose `note`
strings before this project added `seconds`. Those NULLs are correct, not a
loading bug.

**`reflashed` and `cmdline_has_debug` are three-valued.** `NULL` means "not
known" — a different claim from a verified `false`. Backfilled runs have no
record of which stages ran, which is why the trend filters read
`reflashed IS NOT FALSE` rather than `= TRUE`: a guess of `false` would silently
drop every backfilled run off the chart.

**Timestamps from the skill are naive and minute-granular.**
`build_run` uses `datetime.now().strftime("%Y-%m-%d %H:%M")` in the bench host's
local zone. It is stored only to cross-reference `.txt` filenames and is never a
key or an ordering — `started_at TIMESTAMPTZ` orders runs.

**Decimal is not JSON-serializable.** `NUMERIC` comes back from psycopg as
`Decimal`, which `json.dumps` rejects. The chart endpoints cast `::float8`; the
exactness that matters is in the storage and the aggregates.

**Not yet exercised against a real database.** The DDL in
[`server/src/db/schema.sql`](server/src/db/schema.sql) has never been executed
and no ingest has touched a live Postgres. It is cross-checked structurally
(`tests/test_schema_consistency.py` parses the DDL and verifies every column
name the Python side writes), and the 46-test `tests/test_db_live.py` module is
written and waiting, but it skips without `BOOTBENCH_TEST_DSN`. First real
confirmation is `main.py initdb` on the Linux box.

**No run has touched a board.** Every phase but one is built and tested, but
[Phase 7](#build-status) — the first real flash through the real agent — needs
hardware and a logged-on bench host. Until it has happened, the ~1h42m job
timeout is a calculation rather than a measurement, and the TAC's behavior under
the agent is asserted by `/healthz` rather than proven by a run.

---

## Security posture

- **Agent:** bearer token compared with `hmac.compare_digest`, bound to the lab
  interface, one inbound firewall rule scoped to the coordinator's IP. Rejected
  requests are logged with their source address. **No secret is written to
  `agentconfig.json`** — the token comes from the environment named by
  `token_env`. Plain HTTP is proportionate on an isolated lab network;
  `client/INSTALL.md` documents the `ssl.wrap_socket` two-liner if policy
  requires TLS.
- **Web:** a shared `trigger_token` required on every POST (trigger, cancel,
  acknowledge). GETs are unauthenticated — internal and read-only. Bind to the
  internal interface. **This is not internet-facing.**
- **Postgres:** no password in any file this repo controls. Peer auth over the
  unix socket for a dedicated `bootbench` role, or `~/.pgpass` at `0600`.
  `get_db_dsn()` returning empty is the *preferred* configuration.
- `/etc/bootbench/` at `0750`, its `env` at `0600`, `credentials.yaml` at
  `0600`, `User=bootbench` in both units, plus `NoNewPrivileges`,
  `ProtectSystem=strict` and an explicit `ReadWritePaths`.
- **The agent is a new network listener on a lab machine.** Smaller surface than
  an SSH daemon with a hardware-driving service account, but it is new, and it
  belongs in a threat note rather than a footnote.
- `.gitignore` covers `server/config/credentials.yaml`,
  `client/agentconfig.json`, `artifacts/`, and `jobs/`. `devices.yaml` is
  **not** ignored — the inventory is configuration, not state.

One inherited problem, noted so it is not repeated: `bootbench.py` hardcodes
`LOGIN_PASSWORD = "oelinux123"`, the stock Yocto console password. It is not a
secret of ours, but it is a credential in source, and the serial console is the
thing it protects.

---

## Build status

| Phase | | |
|---|---|---|
| 0 | De-risk: TAC COM from an interactive session | **done** — `session_interactive: true`, `Get_Device_Count()` answers cleanly |
| 1 | Automation surface on `bootbench.py` | **done** — all additive; golden-file tests prove the HTML is byte-identical |
| 2 | Agent: jobs, log tail, manifest, artifacts, cancel, adoption | **done** |
| 3 | Coordinator skeleton: config, inventory, credentials, `agent-ping` | **done** |
| 4 | Postgres schema and migrations | **done** (not yet applied to a live database) |
| 5 | Ingestion, backfill, log-reparse repair | **done** |
| 6 | Webserver + worker loop | **done** |
| 7 | First hardware contact | needs a board |
| 8 | Scheduler, retries, alerting | **done** |
| 9 | Hardening and retention | **done** |

Phase 7 is the first time this project touches a board. It starts at
`num_boots: 1` with `stages: capture` on an already-flashed device — a
sub-ten-minute loop — before graduating to `all`.
