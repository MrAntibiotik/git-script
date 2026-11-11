@echo off
rem Manual start of poster with a console window (for checking). Autostart: install_task.ps1
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" poster.py run
) else (
    python poster.py run
)
pause
