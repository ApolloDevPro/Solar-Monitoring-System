<#
.SYNOPSIS
  Installs the Solar Tracker backend and serial bridge as Windows scheduled
  tasks that start at logon and restart automatically on failure.

.DESCRIPTION
  Creates two tasks (SolarTrackerBackend, SolarTrackerBridge):
    - trigger: at logon of the current user (change to AT STARTUP below if
      you run this script as an administrator for a dedicated kiosk PC)
    - restart on failure every 1 minute, up to 999 times
    - no execution time limit (they run until reboot)

  Run from anywhere:
      powershell -ExecutionPolicy Bypass -File deploy\install_windows_tasks.ps1

  Uninstall:
      powershell -File deploy\install_windows_tasks.ps1 -Uninstall
#>
param(
    [string]$ProjectDir = (Split-Path -Parent $PSScriptRoot),
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$BackendName = "SolarTrackerBackend"
$BridgeName  = "SolarTrackerBridge"

if ($Uninstall) {
    foreach ($n in @($BackendName, $BridgeName)) {
        Unregister-ScheduledTask -TaskName $n -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "removed task $n"
    }
    exit 0
}

$ProjectDir = (Resolve-Path $ProjectDir).Path
$backendBat = Join-Path $ProjectDir "deploy\run_backend.bat"
$bridgeBat  = Join-Path $ProjectDir "deploy\run_bridge.bat"

foreach ($bat in @($backendBat, $bridgeBat)) {
    if (-not (Test-Path $bat)) { throw "missing $bat" }
}
if (-not (Test-Path (Join-Path $ProjectDir ".env"))) {
    Write-Warning ".env not found - run 'python ensure_env.py' in $ProjectDir first"
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

# Use AT STARTUP instead of AT LOGON here if running elevated as a service user.
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

foreach ($t in @(
    @{ Name = $BackendName; Bat = $backendBat },
    @{ Name = $BridgeName;  Bat = $bridgeBat }
)) {
    $action = New-ScheduledTaskAction -Execute $t.Bat -WorkingDirectory $ProjectDir
    Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force | Out-Null
    Write-Host "installed task $($t.Name) -> $($t.Bat)"
}

Write-Host ""
Write-Host "Done. Start them now with:"
Write-Host "  Start-ScheduledTask $BackendName"
Write-Host "  Start-ScheduledTask $BridgeName"
Write-Host "Dashboard: http://127.0.0.1:8000  (key from $ProjectDir\.env)"
