$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigPath = Join-Path $ScriptDir 'config.json'

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "Missing config: $ConfigPath"
}

$cfg = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$bindHost = if ($cfg.bind_host) { [string]$cfg.bind_host } else { '127.0.0.1' }
$port = if ($cfg.port) { [int]$cfg.port } else { 19190 }
$baseUrl = "http://${bindHost}:${port}"

try {
    $health = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri "$baseUrl/health"
    Write-Host "Health: $($health.Content)"
    Write-Host ''
    $status = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri "$baseUrl/status"
    $status.Content
} catch {
    Write-Host "Proxy is not healthy at $baseUrl"
    Write-Host $_.Exception.Message
    exit 1
}
