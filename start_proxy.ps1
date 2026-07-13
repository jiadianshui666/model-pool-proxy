$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigPath = Join-Path $ScriptDir 'config.json'
$PidPath = Join-Path $ScriptDir 'proxy.pid'
$LogDir = Join-Path $ScriptDir 'logs'
$StdoutPath = Join-Path $LogDir 'server.out.log'
$StderrPath = Join-Path $LogDir 'server.err.log'
$MaxLogBytes = 5MB

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Rotate-Log {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $item = Get-Item -LiteralPath $Path
    if ($item.Length -lt $MaxLogBytes) {
        return
    }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $archive = "$Path.$stamp"
    Move-Item -LiteralPath $Path -Destination $archive -Force
}

Rotate-Log -Path $StdoutPath
Rotate-Log -Path $StderrPath

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "Missing config: $ConfigPath"
}

$cfg = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$bindHost = if ($cfg.bind_host) { [string]$cfg.bind_host } else { '127.0.0.1' }
$port = if ($cfg.port) { [int]$cfg.port } else { 19190 }
$apiKeyEnv = if ($cfg.api_key_env) { [string]$cfg.api_key_env } else { 'MODEL_POOL_API_KEY' }
$userApiKey = [Environment]::GetEnvironmentVariable($apiKeyEnv, 'User')
if ($userApiKey) {
    Set-Item -Path "Env:$apiKeyEnv" -Value $userApiKey
}
$healthUrl = "http://${bindHost}:${port}/health"

try {
    $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri $healthUrl
    if ($response.StatusCode -eq 200) {
        Write-Host "Proxy already running: $healthUrl"
        exit 0
    }
} catch {
    if (Test-Path -LiteralPath $PidPath) {
        $pidValue = (Get-Content -LiteralPath $PidPath -Raw -Encoding ASCII -ErrorAction SilentlyContinue).Trim()
        if ($pidValue) {
            $oldProcess = Get-Process -Id ([int]$pidValue) -ErrorAction SilentlyContinue
            if (-not $oldProcess) {
                Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

$python = Get-Command python -ErrorAction SilentlyContinue
$argList = @()
if ($python) {
    $exe = $python.Source
    $argList = @('-u', (Join-Path $ScriptDir 'server.py'), '-c', $ConfigPath)
} else {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if (-not $py) {
        throw 'Python was not found in PATH.'
    }
    $exe = $py.Source
    $argList = @('-3', '-u', (Join-Path $ScriptDir 'server.py'), '-c', $ConfigPath)
}

$process = Start-Process `
    -FilePath $exe `
    -ArgumentList $argList `
    -WorkingDirectory $ScriptDir `
    -RedirectStandardOutput $StdoutPath `
    -RedirectStandardError $StderrPath `
    -WindowStyle Hidden `
    -PassThru

Set-Content -LiteralPath $PidPath -Value $process.Id -Encoding ASCII

for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri $healthUrl
        if ($response.StatusCode -eq 200) {
            Write-Host "Proxy started: $healthUrl"
            Write-Host "PID: $($process.Id)"
            exit 0
        }
    } catch {
    }
}

Write-Host "Proxy did not become healthy. Recent stderr:"
if (Test-Path -LiteralPath $StderrPath) {
    Get-Content -LiteralPath $StderrPath -Tail 30 -Encoding UTF8
}
if ($process -and -not $process.HasExited) {
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
}
exit 1
