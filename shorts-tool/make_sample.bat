@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist venv\Scripts\python.exe ( echo Спершу запустіть install.bat & pause & exit /b 1 )
venv\Scripts\python.exe tools\make_sample.py %*
pause
