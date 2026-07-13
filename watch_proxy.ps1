$ErrorActionPreference = 'Continue'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigPath = Join-Path $ScriptDir 'config.json'
$StartScript = Join-Path $ScriptDir 'start_proxy.ps1'
$WatchLog = Join-Path $ScriptDir 'logs\watchdog.log'
$IntervalSeconds = 15

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $WatchLog) | Out-Null

function Write-WatchLog {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Add-Content -LiteralPath $WatchLog -Value $line -Encoding UTF8
    Write-Host $line
}

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    Write-WatchLog "missing config: $ConfigPath"
    exit 1
}

while ($true) {
    try {
        $cfg = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $bindHost = if ($cfg.bind_host) { [string]$cfg.bind_host } else { '127.0.0.1' }
        $port = if ($cfg.port) { [int]$cfg.port } else { 19190 }
        $healthUrl = "http://${bindHost}:${port}/health"

        $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri $healthUrl
        if ($response.StatusCode -ne 200) {
            throw "health status $($response.StatusCode)"
        }
    } catch {
        Write-WatchLog "proxy unhealthy, restarting: $($_.Exception.Message)"
        try {
            & $StartScript | ForEach-Object { Write-WatchLog $_ }
        } catch {
            Write-WatchLog "restart failed: $($_.Exception.Message)"
        }
    }

    Start-Sleep -Seconds $IntervalSeconds
}
