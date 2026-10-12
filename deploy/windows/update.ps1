<#
  Обновление «Патрубки BSI» на Windows (без Docker).

      powershell -ExecutionPolicy Bypass -File deploy\windows\update.ps1

  Если программа ставилась через git — новая версия скачается сама (git pull).
  Иначе перед запуском распакуйте новый ZIP поверх старой папки (файл .env, папки data и .venv не трогать).
#>
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path "$PSScriptRoot\..\..").Path
Set-Location $Root
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"

function Run($exe, [string[]]$arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "Команда завершилась с ошибкой: $exe $($arguments -join ' ')" }
}

# Если программа установлена через git — сначала забираем новую версию с GitHub.
if ((Test-Path (Join-Path $Root ".git")) -and (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "Загружаю новую версию с GitHub (git pull)…"
    & git -C $Root pull --ff-only
    if ($LASTEXITCODE -ne 0) { throw "git pull не удался — проверьте доступ к GitHub или обновите файлы вручную (ZIP)." }
} else {
    Write-Host "Папка не из git: убедитесь, что новая версия (ZIP) уже распакована поверх старой." -ForegroundColor Yellow
}

Stop-ScheduledTask -TaskName "Patrubki BSI" -ErrorAction SilentlyContinue
Run $venvPy @("-m", "pip", "install", "-r", "requirements.txt", "-q")
Run $venvPy @("manage.py", "migrate", "--noinput")
Run $venvPy @("manage.py", "setup_roles")
Run $venvPy @("manage.py", "import_cartons")
Run $venvPy @("manage.py", "collectstatic", "--noinput", "-v", "0")
Start-ScheduledTask -TaskName "Patrubki BSI"
$version = & $venvPy -c "import hoses.version as v; print(v.VERSION)"
Write-Host "Обновлено. Версия: $version" -ForegroundColor Green
Write-Host "В браузере нажмите Ctrl+F5, чтобы увидеть новый интерфейс."
