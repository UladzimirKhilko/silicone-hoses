<#
  Обновление «Патрубки BSI» из ZIP-архива с GitHub (без git).

  1. Скачать ZIP ветки claude/busy-bohr-hd5pfq (GitHub → Code → Download ZIP).
  2. Распаковать, например, в C:\Temp\ — появится папка silicone-hoses-claude-busy-bohr-hd5pfq.
  3. PowerShell от имени администратора:

      powershell -ExecutionPolicy Bypass -File C:\Temp\silicone-hoses-claude-busy-bohr-hd5pfq\deploy\windows\update-from-zip.ps1

  Скрипт копирует новую версию программы в C:\Patrubki (другая папка: -Target D:\Patrubki),
  не трогая настройки (.env), базу (db.sqlite3), чертежи и сканы (data), библиотеки (.venv),
  и запускает обычное обновление (update.ps1).
#>
param([string]$Target = "C:\Patrubki")

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Source = (Resolve-Path "$PSScriptRoot\..\..").Path

if (-not (Test-Path (Join-Path $Target ".venv")) -or -not (Test-Path (Join-Path $Target ".env"))) {
    throw "В $Target не найдена установленная программа (.venv и .env). Укажите папку: -Target <папка> или сначала выполните install.ps1."
}
if ((Resolve-Path $Target).Path.TrimEnd("\") -eq $Source.TrimEnd("\")) {
    throw "Запустите скрипт из распакованного архива, а не из папки программы $Target."
}

# Та же версия, что уже стоит, — значит, распакован старый архив (браузер мог сохранить новый как «…(1).zip»)
$versionOf = { param($dir) $f = Join-Path $dir "hoses\version.py"; if (Test-Path $f) { (Select-String -Path $f -Pattern 'VERSION = "(.+)"').Matches[0].Groups[1].Value } }
$newVersion = & $versionOf $Source
$oldVersion = & $versionOf $Target
if ($newVersion -and $newVersion -eq $oldVersion) {
    Write-Host "В архиве та же версия, что уже установлена: $newVersion." -ForegroundColor Red
    Write-Host "Удалите папку $Source и старый ZIP, скачайте ZIP заново и распакуйте." -ForegroundColor Red
    exit 1
}
# Версия в архиве старше установленной — распакован старый ZIP (откат не делаем)
$number = { param($v) if ($v -match '^(\d+(\.\d+)+)') { $n = $Matches[1]; if ($n -notmatch '\.\d+\.') { $n += ".0" }; [version]$n } }
if ($newVersion -and $oldVersion -and (& $number $newVersion) -lt (& $number $oldVersion)) {
    Write-Host "В архиве СТАРАЯ версия $newVersion, а установлена $oldVersion — обновление отменено." -ForegroundColor Red
    Write-Host "Удалите папку $Source и все скачанные ZIP, скачайте ZIP заново по прямой ссылке и распакуйте." -ForegroundColor Red
    exit 1
}
Write-Host "Новая версия: $Source ($newVersion, сейчас установлена $oldVersion)"
Write-Host "Папка программы: $Target"
Write-Host "Останавливаю программу…"
Stop-ScheduledTask -TaskName "Patrubki BSI" -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

Write-Host "Копирую файлы новой версии…"
# /E — с подпапками, без удаления лишнего (никогда не /MIR — он удалил бы базу и чертежи).
& robocopy $Source $Target /E /R:2 /W:2 /NFL /NDL /NJH /NP `
    /XF ".env" "db.sqlite3" `
    /XD (Join-Path $Source ".git") (Join-Path $Source ".venv") (Join-Path $Source "staticfiles") (Join-Path $Source "data\media") (Join-Path $Source "data\source") | Out-Host
if ($LASTEXITCODE -ge 8) { throw "Копирование не удалось (robocopy, код $LASTEXITCODE)." }

Write-Host "Обновляю библиотеки, базу и оформление…"
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Target "deploy\windows\update.ps1")
if ($LASTEXITCODE -ne 0) { throw "Обновление завершилось с ошибкой — пришлите текст из этого окна разработчику." }
