# Install "Meeting Agent" as a Windows application: Start Menu + optional
# Desktop shortcut running the tray launcher via pythonw (no console window).
#
# Usage (from the repo root, with the venv created):
#   powershell -ExecutionPolicy Bypass -File scripts\install_app.ps1 [-Desktop]
#
# Uninstall: delete the two .lnk files this prints.

param(
    [switch]$Desktop
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $repoRoot ".venv\Scripts\pythonw.exe"
$icon = Join-Path $repoRoot "assets\meeting-agent.ico"

if (-not (Test-Path $pythonw)) {
    Write-Error "No venv found at $pythonw - create it first (python -m venv .venv; pip install -e .)"
}

$shell = New-Object -ComObject WScript.Shell

function New-AgentShortcut([string]$path) {
    $sc = $shell.CreateShortcut($path)
    $sc.TargetPath = $pythonw
    $sc.Arguments = "-m cli.main app"
    $sc.WorkingDirectory = $repoRoot
    if (Test-Path $icon) { $sc.IconLocation = $icon }
    $sc.Description = "Meeting Agent - offline meeting notes and action items"
    $sc.Save()
    Write-Host "Created $path"
}

$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Meeting Agent.lnk"
New-AgentShortcut $startMenu

if ($Desktop) {
    $desktopLnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "Meeting Agent.lnk"
    New-AgentShortcut $desktopLnk
}

Write-Host ""
Write-Host "Done. Launch 'Meeting Agent' from the Start Menu - it starts the local"
Write-Host "dashboard, opens your browser, and parks a tray icon (right-click to quit)."
