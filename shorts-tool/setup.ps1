# Автоматичне встановлення / оновлення інструмента автонарізки шортсів.
# Запускається з SETUP.bat. Повторний запуск = оновлення (ваші налаштування, рамки й зразки зберігаються).
param(
    [string]$Branch = "claude/wonderful-pasteur-vz3090",
    [string]$Target = "C:\ShortsTool"
)

$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = "SilentlyContinue"

$Repo = "MrAntibiotik/git-script"
$Total = 8

function Step($n, $text) { Write-Host ""; Write-Host "[$n/$Total] $text" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "  [ OK ] $t" -ForegroundColor Green }
function Warn($t) { Write-Host "  [УВАГА] $t" -ForegroundColor Yellow }
function Fail($t) {
    Write-Host ""
    Write-Host "  [ПОМИЛКА] $t" -ForegroundColor Red
    Write-Host "  Сфотографуйте/скопіюйте цей текст і надішліть розробнику. Після виправлення просто запустіть SETUP.bat ще раз."
    exit 1
}

function Download($url, $out) {
    Write-Host "  Завантажую: $url"
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        & curl.exe -fL --retry 3 --retry-delay 3 --progress-bar -o $out $url
        if ($LASTEXITCODE -ne 0) { throw "curl.exe завершився з кодом $LASTEXITCODE" }
    } else {
        Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $out
    }
}

function Extract($zip, $dest) {
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    $tar = Get-Command tar.exe -ErrorAction SilentlyContinue
    if ($tar) {
        & tar.exe -xf $zip -C $dest
        if ($LASTEXITCODE -eq 0) { return }
    }
    Expand-Archive -Force -Path $zip -DestinationPath $dest
}

function Run($exe, [string[]]$argList, $what) {
    & $exe @argList
    if ($LASTEXITCODE -ne 0) { Fail "$what (код $LASTEXITCODE)" }
}

function Find-Python {
    foreach ($v in @("3.11", "3.12")) {
        try {
            $p = & py "-$v" -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $p) { return ($p | Select-Object -First 1).Trim() }
        } catch {}
    }
    $cands = @(
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "C:\Program Files\Python311\python.exe",
        "C:\Program Files\Python312\python.exe"
    )
    foreach ($c in $cands) { if (Test-Path $c) { return $c } }
    return $null
}

Write-Host "======================================================================"
Write-Host " Встановлення інструмента автонарізки шортсів у $Target"
Write-Host " Займе 20–60 хв (більшість часу — завантаження ~5 ГБ). Не закривайте вікно."
Write-Host "======================================================================"

$tmp = Join-Path $env:TEMP ("shorts_setup_" + (Get-Random))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

# ---------------------------------------------------------------- 1. відеокарта
Step 1 "Відеокарта і драйвер NVIDIA"
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($smi) {
    try {
        $line = (& nvidia-smi --query-gpu=name,driver_version --format=csv,noheader | Select-Object -First 1)
        $parts = $line.Split(",")
        $gpu = $parts[0].Trim(); $drv = $parts[1].Trim()
        $major = [int]($drv.Split(".")[0])
        if ($major -lt 570) {
            Warn "$gpu, драйвер $drv — застарий (потрібен 570+). Відкриваю сторінку завантаження драйвера."
            Warn "Встановлення продовжиться; після оновлення драйвера перезавантажте ПК."
            Start-Process "https://www.nvidia.com/uk-ua/drivers/"
        } else { Ok "$gpu, драйвер $drv" }
    } catch { Warn "Не вдалося прочитати версію драйвера: $_" }
} else {
    Warn "nvidia-smi не знайдено — драйвер NVIDIA не встановлено? Відкриваю сторінку завантаження."
    Start-Process "https://www.nvidia.com/uk-ua/drivers/"
}

# ---------------------------------------------------------------- 2. Python
Step 2 "Python 3.11"
$py = Find-Python
if (-not $py) {
    $pyExe = Join-Path $tmp "python-3.11.9-amd64.exe"
    Download "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe" $pyExe
    Write-Host "  Встановлюю Python (тихо, лише для вашого користувача)…"
    $p = Start-Process -Wait -PassThru -FilePath $pyExe -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_tcltk=1 Include_launcher=1 Include_test=0"
    if ($p.ExitCode -ne 0) { Fail "Інсталятор Python завершився з кодом $($p.ExitCode)" }
    $py = Find-Python
    if (-not $py) { Fail "Python встановлено, але не знайдено. Перезапустіть SETUP.bat." }
}
Ok "Python: $py"

# ---------------------------------------------------------------- 3. файли інструмента
Step 3 "Файли інструмента (завантаження/оновлення)"
$zip = Join-Path $tmp "tool.zip"
Download "https://github.com/$Repo/archive/refs/heads/$Branch.zip" $zip
Extract $zip (Join-Path $tmp "src")
$top = Get-ChildItem (Join-Path $tmp "src") -Directory | Select-Object -First 1
$src = Join-Path $top.FullName "shorts-tool"
if (-not (Test-Path (Join-Path $src "shorts"))) { Fail "В архіві немає папки shorts-tool" }
$update = Test-Path (Join-Path $Target "shorts")
New-Item -ItemType Directory -Force -Path $Target | Out-Null
# код — замінюємо; config і samples — лише додаємо нові файли (ваші рамки, пороги й зразки не чіпаємо)
& robocopy $src $Target /E /XD config samples /XF SETUP.bat /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { Fail "Не вдалося скопіювати файли (robocopy $LASTEXITCODE)" }
# профілі ігор: нова версія макета/порогів + ваші рамки і категорії звуків
& $py (Join-Path $src "tools\merge_profiles.py") (Join-Path $src "config\profiles") (Join-Path $Target "config\profiles")
foreach ($d in @("config", "samples")) {
    & robocopy (Join-Path $src $d) (Join-Path $Target $d) /E /XC /XN /XO /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { Fail "Не вдалося скопіювати $d (robocopy $LASTEXITCODE)" }
}
if (-not (Test-Path (Join-Path $Target "SETUP.bat"))) { Copy-Item (Join-Path $src "SETUP.bat") $Target }
if ($update) { Ok "Оновлено (ваші налаштування, рамки й зразки збережено)" } else { Ok "Встановлено в $Target" }

# ---------------------------------------------------------------- 4. ffmpeg
Step 4 "ffmpeg (з підтримкою NVIDIA)"
$ff = Get-ChildItem (Join-Path $Target "ffmpeg") -Recurse -Filter ffmpeg.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if ($ff) { Ok "Уже є: $($ff.FullName)" }
else {
    $ffZip = Join-Path $tmp "ffmpeg.zip"
    Download "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip" $ffZip
    Extract $ffZip (Join-Path $Target "ffmpeg")
    $ff = Get-ChildItem (Join-Path $Target "ffmpeg") -Recurse -Filter ffmpeg.exe | Select-Object -First 1
    if (-not $ff) { Fail "ffmpeg.exe не знайдено після розпакування" }
    Ok $ff.FullName
}

# ---------------------------------------------------------------- 5. бібліотеки Python
Step 5 "Бібліотеки Python (PyTorch з CUDA ~2,5 ГБ — це найдовший крок)"
$vpy = Join-Path $Target "venv\Scripts\python.exe"
if (-not (Test-Path $vpy)) { Run $py @("-m", "venv", (Join-Path $Target "venv")) "Не вдалося створити venv" }
Run $vpy @("-m", "pip", "install", "--upgrade", "pip", "--quiet") "Не вдалося оновити pip"
Run $vpy @("-m", "pip", "install", "torch", "torchvision", "torchaudio", "--index-url", "https://download.pytorch.org/whl/cu128") "Не вдалося встановити PyTorch (перевірте інтернет)"
Run $vpy @("-m", "pip", "install", "-r", (Join-Path $Target "requirements.txt")) "Не вдалося встановити бібліотеки"
Ok "Бібліотеки встановлено"

# ---------------------------------------------------------------- 6. whisper
Step 6 "Субтитри: whisper.cpp і модель (~1,6 ГБ)"
& $vpy (Join-Path $Target "tools\setup_whisper.py") --auto
if ($LASTEXITCODE -ne 0) { Warn "Субтитри не налаштувались — решта працюватиме. Запустіть setup_whisper.bat пізніше." }

# ---------------------------------------------------------------- 7. DaVinci
Step 7 "Кнопка в DaVinci Resolve"
Run $vpy @((Join-Path $Target "tools\install_resolve.py")) "Не вдалося встановити кнопку в DaVinci"
try {
    $ws = New-Object -ComObject WScript.Shell
    $desk = [Environment]::GetFolderPath("Desktop")
    $lnk = $ws.CreateShortcut((Join-Path $desk "Автонарізка шортсів.lnk"))
    $lnk.TargetPath = Join-Path $Target "venv\Scripts\pythonw.exe"
    $lnk.Arguments = '"' + (Join-Path $Target "tools\run_job.py") + '"'
    $lnk.WorkingDirectory = $Target
    $lnk.Save()
    Ok "Ярлик на робочому столі: «Автонарізка шортсів» (запуск без DaVinci)"
} catch { Warn "Ярлик не створено: $_" }

# ---------------------------------------------------------------- 8. перевірка
Step 8 "Перевірка (перший раз завантажить моделі розпізнавання ~200 МБ)"
& $vpy (Join-Path $Target "tools\check_setup.py")

Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
Write-Host ""
Write-Host "======================================================================" -ForegroundColor Green
Write-Host " ГОТОВО." -ForegroundColor Green
if (Get-Process -Name "Resolve" -ErrorAction SilentlyContinue) {
    Write-Host " DaVinci Resolve зараз відкритий — ПОВНІСТЮ закрийте його й відкрийте знову." -ForegroundColor Yellow
}
Write-Host " Далі: DaVinci → відкрийте проєкт → Workspace → Scripts → Shorts Autocut."
Write-Host " При першому запуску для гри вікно саме запропонує обвести рамки на кадрі."
Write-Host " Оновити інструмент у майбутньому — просто запустіть SETUP.bat ще раз."
Write-Host "======================================================================" -ForegroundColor Green
