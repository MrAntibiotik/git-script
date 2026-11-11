# Видаляє завдання poster з Планувальника завдань Windows
Stop-ScheduledTask -TaskName "ShortsPoster" -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName "ShortsPoster" -Confirm:$false
Write-Host "Завдання ShortsPoster видалено."
