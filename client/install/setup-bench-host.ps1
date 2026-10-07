# Configure a Windows bench host so the agent survives the login screen.
#
# Run once per bench host, as Administrator, from the account that will own
# the agent:
#
#   powershell -NoProfile -ExecutionPolicy Bypass `
#       -File setup-bench-host.ps1 -CoordinatorIp 10.0.0.5
#
# WHAT THIS SOLVES, AND WHAT IT DOES NOT
#
# The agent must run in an interactive desktop session: out-of-process COM
# servers -- which is what the Alpaca TAC is -- refuse to start, or start
# with no USB access, in Session 0. A Startup-folder shortcut gets that
# right but only lasts as long as someone stays logged on, and a host that
# reboots to a login screen has no session at all, so the agent is not
# running and every nightly records `agent_unhealthy`.
#
# This script configures the two halves it can do safely:
#
#   * a scheduled task that starts the agent at logon AND re-checks it every
#     few minutes, so a crashed agent comes back unattended;
#   * power and update settings, so the host is awake and not rebooting
#     during the nightly window.
#
# It deliberately does NOT configure automatic logon, which is the other
# half and cannot be done well from a script -- the registry method stores
# the password in plaintext where any local administrator can read it. See
# INSTALL.md "Surviving the login screen" for the one-time manual step using
# Sysinternals Autologon, which keeps it in the LSA secret store instead.
#
# A LOCKED screen needs none of this: locking keeps the session, its window
# station, and the agent's COM access intact. Only logging off or rebooting
# destroys them.

[CmdletBinding()]
param(
    [string] $ClientDir       = "C:\bench\client",
    [string] $Config          = "",
    [string] $User            = "",
    [string] $CoordinatorIp   = "",
    [int]    $Port            = 8765,
    [string] $TaskName        = "bootbench-agent",
    [int]    $WatchdogMinutes = 5,
    [switch] $Visible
)

$ErrorActionPreference = "Stop"

function Step($message) { Write-Host "`n== $message" -ForegroundColor Cyan }
function Ok($message)   { Write-Host "   $message" -ForegroundColor Green }
function Warn($message) { Write-Host "   $message" -ForegroundColor Yellow }

# -- preconditions ----------------------------------------------------------

$admin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    throw "Run this in an elevated PowerShell (Run as Administrator)."
}

if (-not $Config) { $Config = Join-Path $ClientDir "agentconfig.json" }
if (-not $User)   { $User   = "$env:USERDOMAIN\$env:USERNAME" }

$launcher = Join-Path $ClientDir "install\start-agent.ps1"
foreach ($path in @($Config, $launcher, (Join-Path $ClientDir "agent.py"))) {
    if (-not (Test-Path -LiteralPath $path)) { throw "missing: $path" }
}

Write-Host "bootbench bench-host setup"
Write-Host "  client dir : $ClientDir"
Write-Host "  config     : $Config"
Write-Host "  run as     : $User"
Write-Host "  port       : $Port"

# -- 1. the agent task ------------------------------------------------------

Step "Scheduled task '$TaskName'"

# LogonType Interactive is the whole point: it means "run only when this
# user is logged on", which puts the agent in the user's own session with a
# real window station. The Task Scheduler option that sounds more robust --
# "run whether the user is logged on or not" -- is Session 0 and silently
# breaks the TAC. Do not change it.
$visibleArg = if ($Visible) { " -Visible" } else { "" }
$argument = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden " +
            "-File `"$launcher`" -Config `"$Config`" -Port $Port$visibleArg"

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argument

$triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $User),
    # The watchdog. start-agent.ps1 exits immediately when the port is
    # already serving, so this costs nothing on a healthy host and is the
    # only thing that recovers a crashed agent unattended.
    (New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
        -RepetitionInterval (New-TimeSpan -Minutes $WatchdogMinutes))
)

$principal = New-ScheduledTaskPrincipal -UserId $User `
    -LogonType Interactive -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TaskName -Action $action `
    -Trigger $triggers -Principal $principal -Settings $settings -Force | Out-Null

Ok "registered: at logon, then every $WatchdogMinutes minutes"
Ok "interactive session only (never Session 0)"

# A Startup shortcut and this task would both try to start an agent. The
# launcher is idempotent so nothing breaks, but leaving both in place means
# two places to look when something is wrong.
$startup = Join-Path ([Environment]::GetFolderPath("Startup")) "agent.lnk"
if (Test-Path -LiteralPath $startup) {
    Warn "A Startup shortcut also exists: $startup"
    Warn "Remove it -- this task supersedes it."
}

# -- 2. stay awake, do not reboot mid-run -----------------------------------

Step "Power settings"
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change disk-timeout-ac 0
powercfg /change monitor-timeout-ac 15
Ok "never sleeps or hibernates on AC; the monitor may still blank (harmless)"

Step "Unattended reboots"
$au = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"
New-Item -Path $au -Force | Out-Null
Set-ItemProperty $au -Name NoAutoRebootWithLoggedOnUsers -Value 1 -Type DWord
Ok "Windows Update will not reboot while this host is logged on"
Warn "A domain GPO can override this. If the host still reboots overnight,"
Warn "that is where to look -- and automatic logon is what makes it survivable."

# -- 3. firewall ------------------------------------------------------------

Step "Firewall"
if (-not $CoordinatorIp) {
    Warn "No -CoordinatorIp given, so no rule was created. The coordinator"
    Warn "cannot reach this host until you add one:"
    Warn "  New-NetFirewallRule -DisplayName 'bootbench agent' -Direction Inbound"
    Warn "    -LocalPort $Port -Protocol TCP -Action Allow -RemoteAddress <ip>"
} else {
    Get-NetFirewallRule -DisplayName "bootbench agent" -ErrorAction SilentlyContinue |
        Remove-NetFirewallRule -ErrorAction SilentlyContinue
    New-NetFirewallRule -DisplayName "bootbench agent" -Direction Inbound `
        -LocalPort $Port -Protocol TCP -Action Allow `
        -RemoteAddress $CoordinatorIp | Out-Null
    Ok "port $Port open to $CoordinatorIp only"
}

# -- 4. start it now --------------------------------------------------------

Step "Starting the agent"
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 6

$up = $false
try {
    $client = New-Object System.Net.Sockets.TcpClient
    $client.Connect("127.0.0.1", $Port)
    $up = $client.Connected
    $client.Close()
} catch { }

if ($up) { Ok "agent is serving on port $Port" }
else     { Warn "not serving yet -- check $env:LOCALAPPDATA\bootbench\" }

Write-Host ""
Write-Host "-------------------------------------------------------------------"
Write-Host "STILL TO DO BY HAND: automatic logon." -ForegroundColor Yellow
Write-Host "Without it, a reboot leaves this host at the login screen with no"
Write-Host "session, and the agent cannot run at all. See INSTALL.md section"
Write-Host "'Surviving the login screen'."
Write-Host "-------------------------------------------------------------------"
Write-Host ""
Write-Host "Then verify, in this order:"
Write-Host ""
Write-Host "  1. The agent answers, in an interactive session:"
Write-Host "       curl.exe -H `"Authorization: Bearer `$env:BOOTBENCH_AGENT_TOKEN`" http://localhost:$Port/healthz"
Write-Host "     session_interactive must be true and tac_device_count at least 1."
Write-Host ""
Write-Host "  2. It survives a lock. Press Win+L, wait a minute, re-run the curl"
Write-Host "     from the coordinator. It should answer throughout."
Write-Host ""
Write-Host "  3. It survives a reboot -- the case that was broken, so test it:"
Write-Host "       Restart-Computer"
Write-Host "     The host should log itself back on and answer the same curl"
Write-Host "     within a minute or two, with nobody touching the keyboard."
Write-Host ""
Write-Host "  4. The coordinator agrees:   main.py agent-ping"
Write-Host ""
Write-Host "Logs:  $env:LOCALAPPDATA\bootbench\agent-<date>.log"
Write-Host "Task:  Get-ScheduledTask $TaskName | Get-ScheduledTaskInfo"
Write-Host "Stop:  Stop-ScheduledTask -TaskName $TaskName"
