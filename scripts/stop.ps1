# Stop FresherPilot and its watchdog (until the next Windows sign-in, or until you start it again).
#   powershell -ExecutionPolicy Bypass -File scripts\stop.ps1
# Restart with the latest code: run this, then scripts\install_autostart.ps1 (or sign out and in).
$root = Split-Path -Parent $PSScriptRoot
# The scheduled task starts the watchdog every 10 min / on wake: this flag keeps it off until install_autostart.ps1.
New-Item -ItemType Directory -Force (Join-Path $root "logs") | Out-Null
Set-Content -Path (Join-Path $root "logs\stopped") -Value (Get-Date) -Encoding utf8
foreach ($name in @("keep_running.pid", "app.pid")) {   # watchdog first, so it can't start the app again
    $file = Join-Path $root "logs\$name"
    if (Test-Path $file) {
        $id = [int](Get-Content $file)
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $id"
        if ($proc -and $proc.CommandLine -match "keep_running\.py|main\.py") {
            Stop-Process -Id $id -Force
            "Stopped $name (pid $id)."
        }
        Remove-Item $file -Force -ErrorAction SilentlyContinue
    }
}
# An app you started yourself (python main.py) in this folder's window: close that window.
$others = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object { $_.CommandLine -match "main\.py" }
if ($others) { "Still running (started by hand?): " + (($others | ForEach-Object { "pid $($_.ProcessId)" }) -join ", ") + ". Close its window to stop it." }
