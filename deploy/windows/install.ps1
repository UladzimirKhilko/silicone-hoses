<#
  Установка «Патрубки BSI» на Windows Server 2019 / Windows 10–11 без Docker.

  Запуск (PowerShell от имени администратора, из папки программы):
      powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1

  Что делает:
    1. Создаёт окружение Python и ставит библиотеки.
    2. Создаёт файл настроек .env (если его ещё нет).
    3. Создаёт базу, скачивает чертежи с Яндекс.Диска, загружает справочник и историю МТЗ.
    4. Создаёт администратора (спросит логин и пароль).
    5. Открывает порт в брандмауэре и ставит автозапуск при старте сервера.
  Повторный запуск безопасен: данные не задваиваются.
#>
param(
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path "$PSScriptRoot\..\..").Path
Set-Location $Root
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
function Run($exe, [string[]]$arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "Команда завершилась с ошибкой: $exe $($arguments -join ' ')" }
}

# --- 1. Python ---
Step "1/5 Проверяю Python"
$py = $null
foreach ($cand in @("py -3.12", "py -3.13", "py -3.11", "python")) {
    $parts = $cand.Split(" ")
    try {
        $ver = & $parts[0] @($parts[1..9] | Where-Object { $_ }) -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and [version]$ver -ge [version]"3.11") { $py = $parts; break }
    } catch { }
}
if (-not $py) {
    throw "Не найден Python 3.11+. Установите Python 3.12 с https://www.python.org/downloads/windows/ (галочка 'Add python.exe to PATH') и запустите скрипт снова."
}
Write-Host "Python: $($py -join ' ') ($ver)"

$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Run $py[0] (@($py[1..9] | Where-Object { $_ }) + @("-m", "venv", ".venv"))
}
Run $venvPy @("-m", "pip", "install", "--upgrade", "pip", "-q")
Run $venvPy @("-m", "pip", "install", "-r", "requirements.txt", "-q")

# --- 2. Настройки ---
Step "2/5 Файл настроек .env"
$envFile = Join-Path $Root ".env"
$ips = @((Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" }).IPAddress)
if (-not (Test-Path $envFile)) {
    $secret = -join ((1..64) | ForEach-Object { "{0:x}" -f (Get-Random -Maximum 16) })
    $hosts = @("localhost", "127.0.0.1", $env:COMPUTERNAME) + $ips
    @(
        "# Настройки «Патрубки BSI». Не пересылать: содержит секретный ключ.",
        "DJANGO_SECRET_KEY=$secret",
        "DJANGO_DEBUG=0",
        "DJANGO_ALLOWED_HOSTS=$($hosts -join ',')",
        "HOST=0.0.0.0",
        "PORT=$Port",
        "MEDIA_ROOT=$Root\data\media",
        "YANDEX_PUBLIC_URL=https://disk.yandex.by/d/L4tPbTCYDqEIEA"
    ) | Set-Content -Path $envFile -Encoding UTF8
    Write-Host "Создан .env (адреса: $($hosts -join ', '))"
} else {
    Write-Host ".env уже есть — оставляю как есть"
}

# --- 3. База и данные ---
Step "3/5 База данных и данные"
Run $venvPy @("manage.py", "migrate", "--noinput", "-v", "0")
Run $venvPy @("manage.py", "setup_roles")
Run $venvPy @("manage.py", "collectstatic", "--noinput", "-v", "0")
Write-Host "Скачиваю папку «Патрубки» с Яндекс.Диска (~50 МБ, несколько минут)…"
# Яндекс.Диск иногда временно отказывает при частых запросах: до 3 заходов, скачанное не повторяется.
$fetched = $false
foreach ($round in 1..3) {
    & $venvPy manage.py fetch_yandex "$Root\data\source"
    if ($LASTEXITCODE -eq 0) { $fetched = $true; break }
    Write-Host "Не все файлы скачались, повторяю через 30 секунд (заход $round из 3)…" -ForegroundColor Yellow
    Start-Sleep -Seconds 30
}
if (-not $fetched) {
    throw "Не удалось скачать все файлы с Яндекс.Диска. Проверьте интернет на сервере и запустите install.ps1 ещё раз — скачанное повторно не загружается."
}
Run $venvPy @("manage.py", "seed_catalog")
Run $venvPy @("manage.py", "import_drawings", "$Root\data\source")
& $venvPy manage.py shell -v 0 -c "from stock.models import Batch; raise SystemExit(0 if Batch.objects.exists() else 1)"
if ($LASTEXITCODE -ne 0) {
    Run $venvPy @("manage.py", "import_mtz_excel", "$Root\data\source\Учет патрубков МТЗ.xlsx")
} else {
    Write-Host "История МТЗ уже перенесена — пропускаю"
}

# --- 4. Администратор ---
Step "4/5 Администратор"
& $venvPy manage.py shell -v 0 -c "from django.contrib.auth.models import User; raise SystemExit(0 if User.objects.filter(is_superuser=True).exists() else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Придумайте логин и пароль администратора (руководителя):"
    Run $venvPy @("manage.py", "createsuperuser")
} else {
    Write-Host "Администратор уже есть"
}

# --- 5. Брандмауэр и автозапуск ---
Step "5/5 Брандмауэр и автозапуск"
if (-not (Get-NetFirewallRule -DisplayName "Patrubki BSI" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName "Patrubki BSI" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow | Out-Null
}
$serve = Join-Path $Root "deploy\windows\serve.py"
$action = New-ScheduledTaskAction -Execute $venvPy -Argument "`"$serve`"" -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Unregister-ScheduledTask -TaskName "Patrubki BSI" -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName "Patrubki BSI" -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
Start-ScheduledTask -TaskName "Patrubki BSI"

Start-Sleep -Seconds 5
$ip = ($ips | Select-Object -First 1)
Write-Host ""
Write-Host "Готово. Откройте в браузере: http://$($env:COMPUTERNAME):$Port  или  http://$($ip):$Port" -ForegroundColor Green
Write-Host "Пользователей добавляйте в браузере: /admin/ → Пользователи (группа «Руководитель» или «Менеджер»)."
