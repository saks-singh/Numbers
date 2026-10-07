# Installing the bootbench client on a Windows bench host

This directory is the only part of the project that runs on Windows. It owns
the hardware: PCAT, the Alpaca TAC, the serial console, and `adb`. The Linux
coordinator sends it jobs over HTTP and never touches the board directly.

Deploying to a new bench host is: copy this directory, write
`agentconfig.json`, make a Startup shortcut. `agent.py` is standard library
only — no virtualenv, nothing to go stale on a lab machine nobody logs into
for three months. `bootbench.py` ships beside it and needs `pyserial` +
`comtypes`, which the host already has if the skill has ever been run there
by hand (`py -3 -m pip install -r requirements.txt` otherwise).

---

## Do not run this as a Windows Service

This is the single most important thing on this page.

The Alpaca TAC is driven through an out-of-process COM server,
`TACCOM.AlpacaTACServer`. COM servers like that routinely refuse to start —
or start in Session 0 with no access to USB/HID devices — when the calling
process has no interactive window station.

- A **Windows Service** runs in **Session 0**. The TAC will not work.
- An agent launched from **`shell:startup`** in a logged-on desktop session
  runs in **Session 1+**, with the same session context in which the TAC
  demonstrably works today when an engineer runs the skill by hand.

`GET /healthz` reports `session_id` and `session_interactive` on every call,
and the dashboard shows a hard warning when the session is 0. If someone
later "improves" this by converting it to a Service, that is how you will
find out.

The trade-off is explicit: the bench host must stay logged on. A reboot
without auto-logon leaves no session, `/healthz` becomes unreachable, the
scheduler records `agent_unhealthy`, and the dashboard shows it. That is a
visible failure, which is the point — it is strictly better than a silently
broken flash.

---

## 1. Lay out the files

Copy this whole directory to the bench host. Nothing else from the repo is
needed — in particular, not `server/`.

```
C:\bench\
  client\
    agent.py
    bootbench.py              <- vendored from the skill bundle
    requirements.txt
    agentconfig.json          <- you create this
    docs\                     <- bootbench's own CLI and data-model reference
  artifacts\                  <- created on first run
```

`C:\bench\artifacts` must **not** be inside OneDrive. The agent refuses to
start if `artifact_root` contains `OneDrive`, because nightly multi-MB log
trees in a synced folder cause sync storms, and OneDrive's file locking makes
`adb pull` fail mid-write in a way that looks like an adb bug.

If `pyserial` or `comtypes` is missing:

```
py -3 -m pip install -r C:\bench\client\requirements.txt
```

## 2. Write the config

```
copy agentconfig.json.example agentconfig.json
notepad agentconfig.json
```

Fill in `bootbench`, `share_root`, and one entry per board under `devices`.

`com_port` / `tac_port` / `adb_serial` are optional on a single-board host
(bootbench auto-detects), but **required once two boards share one bench
host** — otherwise bootbench's COM auto-probe will open the other device's
live console mid-capture, `_open_tac()` raises on an ambiguous TAC count, and
unqualified `adb` picks a board arbitrarily.

No token goes in this file.

## 3. Set the shared token

Generate one on the coordinator:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Store it on the bench host as a **user** environment variable (so it is
inherited by the Startup shortcut, and is not readable by other accounts):

```
setx BOOTBENCH_AGENT_TOKEN "<the token>"
```

Log off and back on, or the variable will not be in the agent's environment.

On the coordinator, put the same value in
`server/config/credentials.yaml` (gitignored) or in
`BOOTBENCH_AGENT_TOKEN`.

## 4. Verify before serving

```
py -3 C:\bench\client\agent.py --config C:\bench\client\agentconfig.json --check
```

This validates the config and runs the capability probes without opening a
port. You want:

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

| Symptom | Cause |
|---|---|
| `session_id: 0` | Running as a Service or via a non-interactive remote exec. See above. |
| `tac_device_count: null` | `comtypes` missing, TAC drivers not installed, or COM refused. Install `comtypes` first: `py -3 -m pip install comtypes pyserial` |
| `tac_device_count: 0` | COM works, but no TAC board is enumerated. Check USB. |
| `token_set: false` | `setx` was run but you did not re-login. |
| `pcat_present: false` | Fix the `pcat_exe` path in the config. |

## 5. Make it start with the session

Run the setup script once, elevated, **from the account that will own the
agent**:

```
powershell -NoProfile -ExecutionPolicy Bypass ^
  -File C:\bench\client\install\setup-bench-host.ps1 -CoordinatorIp <coordinator-ip>
```

It registers a scheduled task that starts the agent at logon and re-checks
it every 5 minutes, stops the host sleeping, blocks unattended Windows
Update reboots, and opens the port to the coordinator alone. It then starts
the agent and tells you what is left to do by hand.

The task runs with logon type **Interactive** — "run only when the user is
logged on". Do not change it to "run whether the user is logged on or not":
that is Session 0, and it silently breaks the TAC. The script says so, and
`/healthz` reports `session_id` on every poll so a regression is visible.

Confirm:

```
curl -H "Authorization: Bearer %BOOTBENCH_AGENT_TOKEN%" http://localhost:8765/healthz
```

The agent's own output goes to `%LOCALAPPDATA%\bootbench\agent-<date>.log`.
Pass `-Visible` to the setup script if you would rather have a console
window to watch.

### Surviving the login screen

The scheduled task covers a crashed agent. It does **not** cover a host that
reboots or logs off, because then there is no session for an interactive task
to run in — and that is the common case on a shared lab machine.

Three different things get confused here, so they are worth separating:

| What happened | Session | Agent | Needs fixing? |
|---|---|---|---|
| Screen locked (<kbd>Win</kbd>+<kbd>L</kbd>, idle timeout) | survives | keeps running, COM still works | **no** |
| Another user signs in (fast user switching) | survives, disconnected | keeps running | no |
| Logged off, or rebooted | gone | not running at all | **yes** |

Locking is harmless: the window station and the agent's COM access outlive
it. Only the third row is a problem, and the fix is **automatic logon**, so
that a reboot lands in a real interactive session instead of at the login
screen.

Set it up with Sysinternals
[Autologon](https://learn.microsoft.com/sysinternals/downloads/autologon),
which stores the password in the LSA secret store:

```
Autologon64.exe
```

Enter the domain, username, and password for the bench account and press
Enable. The registry route (`AutoAdminLogon` + `DefaultPassword` under
`Winlogon`) reaches the same outcome but leaves the password in plaintext
where any local administrator can read it, so prefer Autologon.

Two things worth insisting on:

- **Use a dedicated low-privilege lab account**, not a personal or
  domain-admin one. Automatic logon means anyone with physical access to the
  machine has that account's desktop.
- **Then actually test a reboot.** `Restart-Computer`, walk away, and curl
  `/healthz` from the coordinator a few minutes later. This is the one step
  people skip and the one failure that costs a whole night.

If lab policy forbids automatic logon, the honest fallback is to accept that
a reboot stops the nightly until someone logs in. The coordinator degrades
cleanly rather than lying: the tick records `agent_unhealthy`, no run is
enqueued, and the dashboard shows the device unreachable. After four
consecutive such ticks it alerts.

## 6. Open the port to the coordinator only

Already done if you passed `-CoordinatorIp` in step 5. By hand:

```
netsh advfirewall firewall add rule name="bootbench agent" ^
  dir=in action=allow protocol=TCP localport=8765 ^
  remoteip=<coordinator-ip>
```

Scope it to the coordinator's address. The agent is a new network listener on
a lab machine: it authenticates every request with a constant-time bearer
token comparison and logs every rejection with its source address, but a
single-IP firewall rule is the cheapest real control available.

Plain HTTP is proportionate on an isolated lab network. If policy requires
TLS, wrap the socket in `serve()` with a self-signed cert:

```python
import ssl
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain("agent.crt", "agent.key")
httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
```

...and set `agent_url` to `https://...` on the coordinator.

---

## Operating notes

**Restart is safe mid-job.** `bootbench.py` is a child of the agent, but
Windows does not kill orphans absent a Job object — so if you restart the
agent during a 40-minute flash, the flash keeps running. On startup the agent
scans `artifacts/*/jobs/*/job.json`, and for anything left `running`:

| Condition | Outcome |
|---|---|
| pid still alive | re-adopted, log tailing resumes |
| pid gone, `status.json` present | finalized from bootbench's own status document |
| pid gone, no `status.json` | marked `orphaned` and surfaced in the dashboard |

The second row is the common one, because `bootbench.py` writes
`--json-status` from a `finally` block.

**A logoff or reboot mid-job is the third row, not the second.** Windows
terminates the session's processes hard enough that the `finally` does not
get to run, so the job comes back `orphaned` with no status document. The
coordinator treats that as terminal immediately rather than waiting out the
~1h42m job timeout, records `error_class: NoStatusDocument`, and — because
the cause is transient and usually gone by morning — schedules one retry
after `retry_delay_minutes`, recovering the board from EDL first. So a
Windows Update reboot at 01:20 costs you one attempt, not the night.

**Cancel cannot interrupt a flash.** `POST /jobs/<id>/cancel` writes a
sentinel file that `bootbench.py` checks between boots and before flashing.
The agent never terminates a running process: killing PCAT mid-write to a
boot partition is not a cancellation, it is damage.

**Artifact layout.** `Boot-Charts` and `Boot-Logs` are **per-device and
stable**, not per-job — the skill keeps a 30-run rolling report in
`bootchart-data-<slug>.json`, and giving each run a private directory would
reduce that report to a single column every night. Only `job.json`,
`runner.log`, and `status.json` are per-job.

```
artifacts\<device_id>\
  Boot-Charts\   bootchart-data-<slug>.json, bootchart-overview-<slug>.html
  Boot-Logs\<target>\<build_folder>\...
  .bootbench.lock
  jobs\<job_id>\ job.json, runner.log, status.json, cancel
```

**Disk.** Each night is 6 boots × ~12 files, including `Boot_Trace.txt` and a
decompressed `proc_config.txt`. Nothing under `Boot-Logs` is trimmed by the
skill. `keep_jobs` prunes old per-job directories; `/healthz` reports
`artifact_root_free_gb` so you can see this coming.

**The debug cmdline persists.** The capture stage appends `initcall_debug`,
`log_buf_len=4M`, and `systemd.log_level=debug` to the kernel cmdline and
never removes them, so a board that has run a capture boots debug-y forever.
The nightly always runs `flash`, which restores a clean entry. Do not
"optimize" the nightly by skipping the flash — see the `rmtfs` note in the
project README.
