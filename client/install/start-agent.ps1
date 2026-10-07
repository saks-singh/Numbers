# Start the bootbench agent if it is not already serving.
#
# Idempotent on purpose: the scheduled task that calls this fires at logon
# AND every few minutes afterwards, so this script is both the starter and
# the watchdog. Running it twice must never produce two agents -- two
# processes bound to one port means one of them silently loses, and the one
# that wins may be the one holding a stale config.
#
# Exits 0 when an agent is serving by the time it returns, whether this
# invocation started it or found it already up.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File start-agent.ps1 `
#       -Config C:\bench\client\agentconfig.json

[CmdletBinding()]
param(
    [string] $Config  = "C:\bench\client\agentconfig.json",
    [string] $Agent   = "",
    [string] $LogDir  = "",
    [int]    $Port    = 0,
    [switch] $Visible
)

$ErrorActionPreference = "Stop"

if (-not $Agent)  { $Agent  = Join-Path (Split-Path -Parent (Split-Path -Parent $PSCommandPath)) "agent.py" }
if (-not $LogDir) { $LogDir = Join-Path $env:LOCALAPPDATA "bootbench" }

function Write-Line($message) {
    "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $message
}

# The port is the agent's own, read from its config so this script has no
# second copy of a number that must agree.
if ($Port -le 0) {
    try {
        $Port = [int](Get-Content -Raw -LiteralPath $Config | ConvertFrom-Json).port
    } catch {
        Write-Line "cannot read port from $Config -- $($_.Exception.Message)"
        exit 1
    }
}
if ($Port -le 0) { $Port = 8765 }

# Listening is the only honest liveness test. A process named python proves
# nothing: it may be mid-crash, or be a different script entirely.
function Test-AgentUp([int] $p) {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $wait = $client.BeginConnect("127.0.0.1", $p, $null, $null)
        $ok = $wait.AsyncWaitHandle.WaitOne(1500)
        if ($ok -and $client.Connected) { $client.Close(); return $true }
        $client.Close()
    } catch { }
    return $false
}

if (Test-AgentUp $Port) {
    Write-Line "agent already serving on port $Port; nothing to do"
    exit 0
}

if (-not (Test-Path -LiteralPath $Agent))  { Write-Line "no agent at $Agent";   exit 1 }
if (-not (Test-Path -LiteralPath $Config)) { Write-Line "no config at $Config"; exit 1 }

# The token lives in the user environment, not in the config file. If it is
# missing the agent will start and then reject every request with 401, which
# looks like a network fault from the coordinator -- so say so here instead.
if (-not $env:BOOTBENCH_AGENT_TOKEN) {
    Write-Line "WARNING: BOOTBENCH_AGENT_TOKEN is not set in this session."
    Write-Line "         The agent will start but reject every request with 401."
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$log = Join-Path $LogDir ("agent-{0}.log" -f (Get-Date -Format "yyyyMMdd"))

Write-Line "starting agent: $Agent --config $Config"
Write-Line "log: $log"

$style = if ($Visible) { "Minimized" } else { "Hidden" }
$arguments = @("-3", "`"$Agent`"", "--config", "`"$Config`"")

try {
    Start-Process -FilePath "py" -ArgumentList $arguments `
        -WindowStyle $style `
        -RedirectStandardOutput $log `
        -RedirectStandardError (Join-Path $LogDir ("agent-{0}.err.log" -f (Get-Date -Format "yyyyMMdd"))) `
        -WorkingDirectory (Split-Path -Parent $Agent) | Out-Null
} catch {
    Write-Line "failed to launch: $($_.Exception.Message)"
    exit 1
}

# Give it a moment and confirm, so a configuration error surfaces now rather
# than as an unreachable agent at 01:00.
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Milliseconds 500
    if (Test-AgentUp $Port) {
        Write-Line "agent is serving on port $Port"
        exit 0
    }
}

Write-Line "agent did not start listening within 10s -- see $log"
exit 1
