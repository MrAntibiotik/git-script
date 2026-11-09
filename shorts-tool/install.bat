@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ================================================================
echo  Встановлення інструмента автонарізки шортсів
echo ================================================================
set "PY="
where py >nul 2>nul && py -3.11 --version >nul 2>nul && set "PY=py -3.11"
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY (
  echo [ПОМИЛКА] Python не знайдено. Встановіть Python 3.11 ^(README, крок 1^) і запустіть цей файл знову.
  pause
  exit /b 1
)
echo Використовую: %PY%
if not exist venv\Scripts\python.exe (
  echo Створюю окреме середовище venv ...
  %PY% -m venv venv
  if errorlevel 1 ( echo [ПОМИЛКА] Не вдалося створити venv. & pause & exit /b 1 )
)
venv\Scripts\python.exe -m pip install --upgrade pip
echo.
echo Встановлюю PyTorch з підтримкою відеокарти NVIDIA (~2.5 ГБ, це надовго) ...
venv\Scripts\python.exe -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
if errorlevel 1 ( echo [ПОМИЛКА] Не вдалося встановити PyTorch. Перевірте інтернет і запустіть знову. & pause & exit /b 1 )
echo.
echo Встановлюю решту бібліотек ...
venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 ( echo [ПОМИЛКА] Не вдалося встановити бібліотеки. Запустіть знову. & pause & exit /b 1 )
echo.
venv\Scripts\python.exe tools\check_setup.py
pause
