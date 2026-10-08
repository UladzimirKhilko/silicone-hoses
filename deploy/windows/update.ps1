<#
  Обновление «Патрубки BSI» на Windows (без Docker).

      powershell -ExecutionPolicy Bypass -File deploy\windows\update.ps1

  Перед запуском замените файлы программы новой версией (git pull или распакуйте новый ZIP
  поверх старой папки — файл .env, папки data и .venv при этом не трогать).
#>
param([string]$BackupDir = "C:\Backups\Patrubki")

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

Write-Host "Резервная копия перед обновлением…"
Run $venvPy @("manage.py", "backup", $BackupDir)
Stop-ScheduledTask -TaskName "Patrubki BSI" -ErrorAction SilentlyContinue
Run $venvPy @("-m", "pip", "install", "-r", "requirements.txt", "-q")
Run $venvPy @("manage.py", "migrate", "--noinput")
Run $venvPy @("manage.py", "setup_roles")
Run $venvPy @("manage.py", "collectstatic", "--noinput", "-v", "0")
Start-ScheduledTask -TaskName "Patrubki BSI"
Write-Host "Обновлено." -ForegroundColor Green
