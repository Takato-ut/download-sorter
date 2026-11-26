# Register the scheduled task that runs sort-downloads.ps1 every 30 minutes and at logon.
# Usage: powershell -ExecutionPolicy Bypass -File register-task.ps1 [-IntervalMinutes 30] [-TaskName DownloadSorter]
param([int]$IntervalMinutes = 30, [string]$TaskName = 'DownloadSorter')

$script = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'sort-downloads.ps1'
# conhost --headless keeps a console window from flashing every run
$action = New-ScheduledTaskAction -Execute 'conhost.exe' -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$script`""
$t1 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$t2 = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $t1, $t2 -Settings $set `
    -Description 'Sort Downloads into Documents by rules.json' -Force | Select-Object TaskName, State
