<#
  copy-reports.ps1 - копирование отчётов *.xlsx из локальной папки в сетевую.
  Копирует новые и изменённые файлы вместе с подпапками (месяц, линия) через robocopy.
  Ничего не удаляет в назначении (режимы зеркалирования и очистки не используются).
  -DryRun  только проверяет доступность папок и печатает, что было бы сделано; robocopy не запускается.
  -NoPause не ждать нажатия Enter в конце.
  Код выхода: 0 успех, 1 ошибка (папка недоступна или robocopy вернул 8 и больше).
#>
param(
    [string]$Source = 'C:\Andon-Reports',
    [string]$Destination = '\\alm-mca-fs01.mk.int\Storage\Companies\Astana Motors Manufacturing Kazakhstan\010 Body Shop\020 Production\Автоматические_отчеты',
    [switch]$DryRun,
    [switch]$NoPause
)

$ErrorActionPreference = 'Stop'
$logDir = 'C:\plc-backups\update-logs'
$logFile = Join-Path $logDir ('copy-reports-' + (Get-Date -Format 'yyyy-MM-dd') + '.log')

function Write-Log([string]$Message) {
    $line = '[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $Message
    Write-Host $line
    if (-not $DryRun) {
        try {
            if (-not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
            Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8
        } catch {
            Write-Host ('Не удалось записать журнал: ' + $_.Exception.Message)
        }
    }
}

function Finish([int]$Code) {
    if (-not $NoPause) { [void](Read-Host 'Нажмите Enter, чтобы закрыть окно') }
    exit $Code
}

Write-Log ('Копирование отчётов: источник "' + $Source + '", назначение "' + $Destination + '"' + $(if ($DryRun) { ' (пробный запуск)' } else { '' }))

if (-not (Test-Path -LiteralPath $Source)) {
    Write-Log 'ОШИБКА: локальная папка отчётов недоступна.'
    Finish 1
}
if (-not (Test-Path -LiteralPath $Destination)) {
    Write-Log 'ОШИБКА: сетевая папка недоступна (проверьте доступ учётной записи, под которой идёт задача).'
    Finish 1
}

if ($DryRun) {
    $count = @(Get-ChildItem -LiteralPath $Source -Recurse -Filter '*.xlsx' -File).Count
    Write-Log ('Пробный запуск: обе папки доступны, файлов *.xlsx в источнике: ' + $count)
    Write-Log 'Было бы выполнено: robocopy <источник> <назначение> *.xlsx /E /XO /R:2 /W:5 /NP /NFL /NDL (копирование не выполнялось)'
    Finish 0
}

& robocopy $Source $Destination '*.xlsx' /E /XO /R:2 /W:5 /NP /NFL /NDL | ForEach-Object { Write-Log ([string]$_) }
$code = $LASTEXITCODE
if ($code -ge 8) {
    Write-Log ('ОШИБКА: robocopy вернул код ' + $code)
    Finish 1
}
Write-Log ('Готово, код robocopy ' + $code + ' (меньше 8 это успех)')
Finish 0
