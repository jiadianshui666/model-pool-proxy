$WshShell = New-Object -ComObject WScript.Shell
$Desktop = [Environment]::GetFolderPath('Desktop')
$Shortcut = $WshShell.CreateShortcut("$Desktop\Model Pool Proxy.lnk")
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Shortcut.TargetPath = Join-Path $ProjectDir 'start.bat'
$Shortcut.WorkingDirectory = $ProjectDir
$Shortcut.IconLocation = "C:\Windows\System32\imageres.dll,162"
$Shortcut.Description = "Start Model Pool Proxy, status page, and Claude Code"
$Shortcut.Save()
Write-Host "Shortcut created at: $Desktop\Model Pool Proxy.lnk"
