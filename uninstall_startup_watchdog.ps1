$ErrorActionPreference = 'Stop'

$StartupDir = [Environment]::GetFolderPath('Startup')
$ShortcutPath = Join-Path $StartupDir 'Model Pool Proxy Watchdog.lnk'

if (Test-Path -LiteralPath $ShortcutPath) {
    Remove-Item -LiteralPath $ShortcutPath -Force
    Write-Host "Removed startup watchdog shortcut: $ShortcutPath"
} else {
    Write-Host "Startup watchdog shortcut not found."
}
