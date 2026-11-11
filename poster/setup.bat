@echo off
rem One-click install: creates .venv, installs dependencies, copies example configs.
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo [!] Python not found. Install Python 3.11+ from python.org with "Add python.exe to PATH".
    pause
    exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment .venv ...
    python -m venv .venv || (pause & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt || (pause & exit /b 1)
if not exist config.json copy config.example.json config.json >nul
if not exist .env copy .env.example .env >nul
where ffmpeg >nul 2>nul
if errorlevel 1 echo [!] ffmpeg not found in PATH - set "ffmpeg"/"ffprobe" paths in config.json (see README).
echo.
echo Done. Next steps:
echo   1. Edit config.json and .env  (see README.md)
echo   2. .venv\Scripts\python poster.py auth youtube / auth meta / auth tiktok
echo   3. .venv\Scripts\python poster.py check
echo   4. powershell -ExecutionPolicy Bypass -File install_task.ps1
pause
