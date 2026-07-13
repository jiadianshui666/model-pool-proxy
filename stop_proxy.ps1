$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidPath = Join-Path $ScriptDir 'proxy.pid'

if (-not (Test-Path -LiteralPath $PidPath)) {
    Write-Host 'No proxy.pid found. Nothing to stop.'
    exit 0
}

$pidValue = (Get-Content -LiteralPath $PidPath -Raw -Encoding ASCII).Trim()
if (-not $pidValue) {
    Remove-Item -LiteralPath $PidPath -Force
    Write-Host 'Empty proxy.pid removed.'
    exit 0
}

$process = Get-Process -Id ([int]$pidValue) -ErrorAction SilentlyContinue
if (-not $process) {
    Remove-Item -LiteralPath $PidPath -Force
    Write-Host "Process $pidValue is not running. Removed stale proxy.pid."
    exit 0
}

Stop-Process -Id $process.Id -Force
Remove-Item -LiteralPath $PidPath -Force
Write-Host "Proxy stopped: PID $pidValue"
