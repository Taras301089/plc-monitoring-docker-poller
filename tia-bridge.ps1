<#
  tia-bridge.ps1 - служба обмена между сайтом PMM и TIA Portal (Windows).
  Сайт (контейнер Linux) не может вызвать TIA Openness напрямую, поэтому обмен идёт через общую папку (по умолчанию C:\tia-bridge,
  в контейнере она видна как /tia-bridge). Открытых портов нет:
    requests\<id>.json   заявка, которую кладёт сайт (кнопка «Выгрузить проект TIA» у администратора)
    jobs\<id>\status.json состояние выгрузки (queued, running, done, error), журнал export.log
    out\TIA_<проект>_<дата>.zip готовый ZIP с XML проекта (блоки, типы, таблицы тегов, manifest.csv), контролями ProDiag и текстами проекта в Excel
    tia-projects.json     список разрешённых проектов (только отсюда берутся пути; сайт путь не передаёт)
    bridge.json           отметка «служба жива» (обновляется каждые несколько секунд)
  Выгрузку выполняет tia-export.ps1 (открывает проект без окна TIA, ничего не меняет и не сохраняет). Проект в списке должен быть КОПИЕЙ,
  которая не открыта в TIA. Выгрузки идут по одной; у каждой есть тайм-аут.
  Запуск: задача планировщика «PMM TIA bridge» при входе в Windows, под учёткой из группы «Siemens TIA Openness».
  Параметры: -Root папка обмена; -TimeoutMin тайм-аут одной выгрузки; -KeepZips сколько последних ZIP хранить на проект; -Once одна проверка и выход.
#>
param(
    [string]$Root = 'C:\tia-bridge',
    [string]$ExportScript = (Join-Path $PSScriptRoot 'tia-export.ps1'),
    [int]$TimeoutMin = 40,
    [int]$PollSec = 3,
    [int]$KeepZips = 3,
    [switch]$Once
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem
foreach ($d in 'requests', 'jobs', 'out', 'work', 'logs') { New-Item -ItemType Directory -Force -Path (Join-Path $Root $d) | Out-Null }
$utf8 = New-Object System.Text.UTF8Encoding($false)

function Write-Log([string]$Message) {
    $line = '[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $Message
    Write-Host $line
    try { [IO.File]::AppendAllText((Join-Path $Root 'logs\bridge.log'), $line + [Environment]::NewLine, $utf8) } catch { }
}

# Запись JSON целиком (временный файл, затем замена): сайт никогда не читает недописанный файл
function Write-Json([string]$Path, $Obj) {
    $tmp = "$Path.tmp"
    [IO.File]::WriteAllText($tmp, ($Obj | ConvertTo-Json -Depth 6), $utf8)
    Move-Item -LiteralPath $tmp -Destination $Path -Force
}

function Read-Json([string]$Path) {
    try { Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $null }
}

function Send-Heartbeat {
    Write-Json (Join-Path $Root 'bridge.json') @{ time = (Get-Date).ToUniversalTime().ToString('o'); host = $env:COMPUTERNAME; user = "$env:USERDOMAIN\$env:USERNAME" }
}

# Дописать поля в status.json задачи
function Set-Status([string]$JobDir, [hashtable]$Patch) {
    $path = Join-Path $JobDir 'status.json'
    $h = @{}
    $old = Read-Json $path
    if ($old) { $old.PSObject.Properties | ForEach-Object { $h[$_.Name] = $_.Value } }
    foreach ($k in $Patch.Keys) { $h[$k] = $Patch[$k] }
    Write-Json $path $h
}

function Get-Tail([string]$Path, [int]$Lines = 12) {
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    (Get-Content -LiteralPath $Path -Tail $Lines -Encoding OEM -ErrorAction SilentlyContinue) -join "`n"   # вывод дочернего powershell в кодировке консоли (OEM)
}

function Remove-Quietly([string]$Path) {
    try { if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop } } catch { Write-Log "Не удалось удалить $Path : $($_.Exception.Message)" }
}

function Invoke-Job([IO.FileInfo]$ReqFile) {
    $id = $ReqFile.BaseName
    $req = Read-Json $ReqFile.FullName
    Remove-Quietly $ReqFile.FullName      # заявка принята: повторно её не берём
    if ($id -notmatch '^[A-Za-z0-9_-]{1,60}$') { Write-Log "Заявка с недопустимым именем пропущена: $id"; return }
    $jobDir = Join-Path $Root "jobs\$id"
    New-Item -ItemType Directory -Force -Path $jobDir | Out-Null
    $now = { (Get-Date).ToUniversalTime().ToString('o') }

    $cfg = Read-Json (Join-Path $Root 'tia-projects.json')
    $proj = $null
    if ($req -and $cfg) { $proj = @($cfg.projects) | Where-Object { $_.id -eq $req.project } | Select-Object -First 1 }
    if (-not $proj) { Set-Status $jobDir @{ state = 'error'; finished_at = (& $now); error = 'Проект не найден в списке разрешённых (tia-projects.json)' }; return }
    if (-not (Test-Path -LiteralPath $proj.path)) { Set-Status $jobDir @{ state = 'error'; finished_at = (& $now); error = "Файл проекта не найден: $($proj.path)" }; return }

    $work = Join-Path $Root "work\$id"
    $log = Join-Path $jobDir 'export.log'
    Set-Status $jobDir @{ state = 'running'; started_at = (& $now) }
    Write-Log "Выгрузка $($proj.id) начата (заявка $id)"
    try {
        $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$ExportScript`"", '-Project', "`"$($proj.path)`"", '-OutDir', "`"$work`"", '-NoPause')
        $p = Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -PassThru -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError "$log.err"
        $null = $p.Handle      # без этого ExitCode после WaitForExit остаётся пустым
        $deadline = (Get-Date).AddMinutes($TimeoutMin)
        while (-not $p.WaitForExit(5000)) {
            Send-Heartbeat
            if ((Get-Date) -gt $deadline) {
                & taskkill.exe /PID $p.Id /T /F | Out-Null
                throw "Тайм-аут $TimeoutMin мин: выгрузка прервана"
            }
        }
        if ($p.ExitCode -ne 0) {
            $tail = (Get-Tail "$log.err" 6) + ' ' + (Get-Tail $log 6)
            if ($tail -match 'add-on packages are missing:\s*([^\s(]+)') {
                throw "Проект не открылся: в TIA Portal на этом ПК не установлен пакет поддержки оборудования (HSP) $($Matches[1]). Установите его (Siemens Support Packages) или выберите другой проект."
            }
            throw "Выгрузка завершилась с ошибкой (код $($p.ExitCode)): $tail"
        }

        $exported = 0; $skipped = 0; $failed = 0
        $man = Get-ChildItem -LiteralPath $work -Recurse -Filter 'manifest.csv' -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($man) {
            $delim = if ((Get-Content -LiteralPath $man.FullName -TotalCount 1 -Encoding UTF8) -match ';') { ';' } else { ',' }   # tia-export.ps1 пишет manifest.csv с разделителем региона
            foreach ($r in (Import-Csv -LiteralPath $man.FullName -Delimiter $delim -Encoding UTF8)) {
                # строки texts/prodiag (файлы Excel) в число блоков не входят, их ошибки считаются
                if ($r.status -eq 'skipped') { $skipped++ } elseif ($r.status -eq 'error') { $failed++ } elseif ($r.kind -notin 'texts', 'prodiag') { $exported++ }
            }
        }
        $zipName = 'TIA_{0}_{1}.zip' -f $proj.id, (Get-Date -Format 'yyyy-MM-dd_HH-mm')
        $zip = Join-Path $Root "out\$zipName"
        Remove-Quietly $zip
        [IO.Compression.ZipFile]::CreateFromDirectory($work, $zip)
        $size = (Get-Item -LiteralPath $zip).Length
        if ($size -lt 100) { throw 'ZIP получился пустым: в выгрузке нет файлов' }
        Set-Status $jobDir @{ state = 'done'; finished_at = (& $now); file = $zipName; size = $size; exported = $exported; skipped = $skipped; failed = $failed }
        Write-Log "Выгрузка $($proj.id) готова: $zipName, $size байт, блоков $exported, пропущено $skipped"
        # хранится несколько последних ZIP проекта
        Get-ChildItem -LiteralPath (Join-Path $Root 'out') -Filter "TIA_$($proj.id)_*.zip" | Sort-Object LastWriteTime -Descending | Select-Object -Skip $KeepZips | ForEach-Object { Remove-Quietly $_.FullName }
    }
    catch {
        Set-Status $jobDir @{ state = 'error'; finished_at = (& $now); error = $_.Exception.Message }
        Write-Log "Выгрузка $($proj.id) не удалась: $($_.Exception.Message)"
    }
    finally { Remove-Quietly $work }
}

# Задачи, оставшиеся «в работе» от прерванного запуска, помечаются ошибкой
Get-ChildItem -LiteralPath (Join-Path $Root 'jobs') -Directory -ErrorAction SilentlyContinue | ForEach-Object {
    $st = Read-Json (Join-Path $_.FullName 'status.json')
    if ($st -and $st.state -eq 'running') { Set-Status $_.FullName @{ state = 'error'; finished_at = (Get-Date).ToUniversalTime().ToString('o'); error = 'Служба была перезапущена во время выгрузки' } }
}

Write-Log "Служба запущена: папка $Root, скрипт выгрузки $ExportScript"
do {
    Send-Heartbeat
    $next = Get-ChildItem -LiteralPath (Join-Path $Root 'requests') -Filter '*.json' -ErrorAction SilentlyContinue | Sort-Object Name | Select-Object -First 1
    if ($next) { Invoke-Job $next } elseif (-not $Once) { Start-Sleep -Seconds $PollSec }
} while (-not $Once -or $next)
