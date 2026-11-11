# Реєструє poster у Планувальнику завдань Windows:
#  - запуск при вході в систему (з затримкою 1 хв),
#  - без вікна (pythonw), без обмеження часу роботи,
#  - автоперезапуск при збої, лише один екземпляр.
# Запуск:  powershell -ExecutionPolicy Bypass -File install_task.ps1
$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$taskName = "ShortsPoster"

$pythonw = Join-Path $dir ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "Не знайдено pythonw.exe. Створіть .venv (див. інструкцію) або встановіть Python." }
    $pythonw = $cmd.Source
}

$action   = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$dir\poster.py`" run" -WorkingDirectory $dir
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$trigger.Delay = "PT1M"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "Автопублікація шортсів (poster)" -Force | Out-Null

Write-Host "Завдання '$taskName' створено. Запускаю зараз..."
Start-ScheduledTask -TaskName $taskName
Write-Host "Готово. Журнал: $dir\log.txt"
