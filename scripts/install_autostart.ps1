# Start FresherPilot automatically when you sign in to Windows (hidden, restarts itself if it stops).
#   Install:  powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
#   Remove:   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Remove
param([switch]$Remove, [switch]$NoStart)

$root = Split-Path -Parent $PSScriptRoot
$startup = [Environment]::GetFolderPath('Startup')
$link = Join-Path $startup "FresherPilot.lnk"

$task = "FresherPilot watchdog"
if ($Remove) {
    if (Test-Path $link) { Remove-Item $link -Force; "Autostart removed." } else { "Autostart was not installed." }
    if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) { Unregister-ScheduledTask -TaskName $task -Confirm:$false; "Scheduled task removed." }
    return
}
Remove-Item (Join-Path $root "logs\stopped") -Force -ErrorAction SilentlyContinue  # undo scripts\stop.ps1

$python = (Get-Command python -ErrorAction Stop).Source
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found next to $python" }

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($link)
$sc.TargetPath = $pythonw
$sc.Arguments = "`"$root\scripts\keep_running.py`""
$sc.WorkingDirectory = $root
$sc.WindowStyle = 7
$sc.Description = "FresherPilot (keeps the job app running)"
$sc.Save()
"Autostart installed: $link"

# Also a scheduled task: starts the watchdog at sign-in, every 10 minutes and whenever the PC wakes up, so a
# watchdog that died (e.g. during sleep) comes back. A second copy exits at once if one is already running.
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$root\scripts\keep_running.py`"" -WorkingDirectory $root
$every10 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 10)
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$wakeClass = Get-CimClass -ClassName MSFT_TaskEventTrigger -Namespace Root/Microsoft/Windows/TaskScheduler
$wake = New-CimInstance -CimClass $wakeClass -ClientOnly
$wake.Enabled = $true
$wake.Subscription = '<QueryList><Query Id="0" Path="System"><Select Path="System">*[System[Provider[@Name=''Microsoft-Windows-Power-Troubleshooter''] and EventID=1]] or *[System[Provider[@Name=''Microsoft-Windows-Kernel-Power''] and EventID=507]]</Select></Query></QueryList>'
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $task -Action $action -Trigger @($logon, $every10, $wake) -Settings $settings `
    -Principal $principal -Force | Out-Null
"Scheduled task installed: $task (sign-in, every 10 min, on wake)"

if (-not $NoStart) {
    Start-Process -FilePath $pythonw -ArgumentList "`"$root\scripts\keep_running.py`"" -WorkingDirectory $root -WindowStyle Hidden
    "Watchdog started now (it starts the app unless it is already running)."
}
