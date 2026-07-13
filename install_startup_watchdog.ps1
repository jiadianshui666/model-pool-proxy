$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WatchScript = Join-Path $ScriptDir 'watch_proxy.ps1'
$StartupDir = [Environment]::GetFolderPath('Startup')
$ShortcutPath = Join-Path $StartupDir 'Model Pool Proxy Watchdog.lnk'

if (-not (Test-Path -LiteralPath $WatchScript)) {
    throw "Missing watchdog script: $WatchScript"
}

New-Item -ItemType Directory -Force -Path $StartupDir | Out-Null

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($ShortcutPath)
$shortcut.TargetPath = 'powershell.exe'
$shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$WatchScript`""
$shortcut.WorkingDirectory = $ScriptDir
$shortcut.IconLocation = 'C:\Windows\System32\imageres.dll,110'
$shortcut.Description = 'Keep Model Pool Proxy running for Claude Code'
$shortcut.Save()

Write-Host "Installed startup watchdog shortcut: $ShortcutPath"
