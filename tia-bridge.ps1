<#
  tia-bridge.ps1 - служба обмена между сайтом PMM и TIA Portal (Windows).
  Сайт (контейнер Linux) не может вызвать TIA Openness напрямую, поэтому обмен идёт через общую папку (по умолчанию C:\tia-bridge,
  в контейнере она видна как /tia-bridge). Открытых портов нет:
    requests\<id>.json   заявка, которую кладёт сайт (вкладка «Выгрузка TIA», кнопка «Выгрузить»); поле target — «Путь сохранения»
    jobs\<id>\status.json состояние выгрузки (queued, running, done, error), журнал export.log; target и saved_to — куда положена копия ZIP
    out\TIA_<проект>_<дата>.zip готовый ZIP с XML проекта (блоки, типы, таблицы тегов, manifest.csv), контролями ProDiag и текстами проекта в Excel;
                          копия ZIP дополнительно кладётся в папку target (проверка пути Test-TargetDir и пробная запись до запуска TIA)
    tia-projects.json     список разрешённых проектов (только отсюда берутся пути проектов; сайт путь проекта не передаёт), поле plc — имя ПЛК в PMM;
                          поле roots — папки поиска .ap17 (по умолчанию C:\TIA_for_claude_to_XML и C:\projects); служба сама дописывает пары «ПЛК -> проект»
    found-projects.json   найденные файлы .ap17 (обновляется каждые 60 с: ключ, путь, относительный путь, дата, размер); из них выбирают проект в окне выгрузки
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
    [int]$ScanEverySec = 60,
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

# Проверка пути сохранения копии ZIP: те же правила, что check_target в fastapi-api/tia.py. Возвращает @{ path; error }
function Test-TargetDir([string]$Raw) {
    if ($null -eq $Raw) { return @{ path = $null; error = $null } }
    $p = $Raw.Trim().Replace('/', '\')
    if (-not $p) { return @{ path = $null; error = $null } }
    if ($p.Length -gt 240) { return @{ path = $null; error = 'Путь сохранения длиннее 240 знаков' } }
    if ($p -match '[<>"|?*\x00-\x1f]') { return @{ path = $null; error = 'Путь сохранения содержит недопустимые знаки (< > " | ? *)' } }
    $drive = $null
    if ($p.StartsWith('\\')) {
        $parts = $p.Substring(2).Split('\')
        if ($parts.Count -lt 2 -or -not $parts[0] -or -not $parts[1] -or $p.Contains(':')) { return @{ path = $null; error = 'Сетевой путь пишется как \\сервер\папка\...' } }
        $head = '\\' + $parts[0] + '\' + $parts[1]
        $segs = @($parts | Select-Object -Skip 2)
    }
    elseif ($p -match '^[A-Za-z]:\\') {
        if ($p.Substring(2).Contains(':')) { return @{ path = $null; error = 'Двоеточие допустимо только после буквы диска' } }
        $drive = $p.Substring(0, 1).ToUpper()
        $head = $drive + ':'
        $segs = @($p.Substring(3).Split('\'))
    }
    else { return @{ path = $null; error = 'Укажите полный путь с буквой диска (C:\папка) или сетевой путь (\\сервер\папка)' } }
    $list = New-Object System.Collections.Generic.List[string]
    foreach ($s in $segs) { $list.Add($s) }
    while ($list.Count -gt 0 -and $list[$list.Count - 1] -eq '') { $list.RemoveAt($list.Count - 1) }   # завершающая косая черта
    foreach ($s in $list) {
        if ($s -eq '') { return @{ path = $null; error = 'В пути две косые черты подряд' } }
        if ($s.Trim(' .') -eq '') { return @{ path = $null; error = 'Переход «..» и папки из одних точек в пути запрещены' } }
    }
    if ($drive) {
        if ($list.Count -eq 0) { return @{ path = $null; error = 'Корень диска нельзя выбрать: укажите папку' } }
        $top = $list[0].ToLower()
        if (($top -in @('windows', 'programdata', '$recycle.bin', 'system volume information', 'recovery', 'boot')) -or $top.StartsWith('program files')) {
            return @{ path = $null; error = "Системная папка ${drive}:\$($list[0]) не подходит для сохранения" }
        }
        if ($top -eq 'users' -and $list.Count -le 2) { return @{ path = $null; error = 'Нельзя сохранять в папку Users или в корень профиля: укажите вложенную папку (например, Documents\TIA)' } }
    }
    return @{ path = ($head + '\' + ($list -join '\')).TrimEnd('\'); error = $null }
}

# Папка сохранения создаётся при необходимости, в неё пишется и удаляется пробный файл; при ошибке возвращается понятный текст
function Test-TargetWritable([string]$Dir) {
    try {
        New-Item -ItemType Directory -Force -Path $Dir -ErrorAction Stop | Out-Null
        $probe = Join-Path $Dir ('.pmm-write-test-' + [guid]::NewGuid().ToString('N') + '.tmp')
        [IO.File]::WriteAllText($probe, 'pmm', $utf8)
        Remove-Item -LiteralPath $probe -Force -ErrorAction Stop
        return $null
    }
    catch { return "Папка недоступна для записи: $Dir ($($_.Exception.Message))" }
}

# ---------- Поиск проектов .ap17 в разрешённых папках (выбор проекта в окне «Выгрузка проекта TIA») ----------
# Корни поиска: поле roots в tia-projects.json, иначе значения по умолчанию. Глубина не более 5 уровней, служебные папки TIA и ссылки пропускаются.
$DefaultRoots = @('C:\TIA_for_claude_to_XML', 'C:\projects')
$SkipDirs = @('im', 'xref', 'system', 'logs', 'temp', 'userfiles', 'additionalfiles', 'backup', 'backups', '$recycle.bin')
$ScanMaxDepth = 5
$ScanMaxItems = 500

function Get-Roots {
    $cfg = Read-Json (Join-Path $Root 'tia-projects.json')
    $list = @()
    if ($cfg -and $cfg.roots) { $list = @($cfg.roots | Where-Object { $_ -and ([string]$_).Trim() } | ForEach-Object { ([string]$_).Trim().TrimEnd('\') }) }
    if ($list.Count -eq 0) { $list = $DefaultRoots }
    @($list | Where-Object { $_ -match '^[A-Za-z]:\\.' -and $_ -notmatch '\.\.' })   # корень диска и пути с «..» не принимаются
}

# Папка или файл — ссылка (junction, symlink): по ней из разрешённого корня можно выйти наружу
function Test-Reparse([string]$Path) {
    try { return [bool]((Get-Item -LiteralPath $Path -Force -ErrorAction Stop).Attributes -band [IO.FileAttributes]::ReparsePoint) } catch { return $true }
}

# Путь внутри одного из разрешённых корней: без «..» и без выхода по ссылкам (каждая папка между корнем и файлом не ссылка)
function Test-InsideRoot([string]$Path, $Roots) {
    if (-not $Path -or $Path -match '(^|[\\/])\.\.([\\/]|$)') { return $false }
    try { $full = [IO.Path]::GetFullPath($Path) } catch { return $false }
    foreach ($r in $Roots) {
        $rf = [IO.Path]::GetFullPath($r).TrimEnd('\')
        if (-not $full.StartsWith($rf + '\', [StringComparison]::OrdinalIgnoreCase)) { continue }
        $cur = $full
        while ($cur.Length -gt $rf.Length) {
            if (Test-Reparse $cur) { return $false }
            $cur = Split-Path -Parent $cur
        }
        return $true
    }
    return $false
}

function Find-Ap17([string]$Dir, [int]$Depth, [string]$RootDir, [string]$RootLeaf, $Acc) {
    if ($Acc.Count -ge $ScanMaxItems) { return }
    foreach ($f in @(Get-ChildItem -LiteralPath $Dir -File -Filter '*.ap17' -ErrorAction SilentlyContinue)) {
        if ($f.Extension -ne '.ap17' -or $Acc.Count -ge $ScanMaxItems) { continue }
        $rel = $f.FullName.Substring($RootDir.Length).TrimStart('\')
        $Acc.Add([ordered]@{ key = ($RootLeaf + '\' + $rel); root = $RootLeaf; rel = $rel; path = $f.FullName
            modified = $f.LastWriteTimeUtc.ToString('o'); size = $f.Length }) | Out-Null
    }
    if ($Depth -ge $ScanMaxDepth) { return }
    foreach ($d in @(Get-ChildItem -LiteralPath $Dir -Directory -ErrorAction SilentlyContinue | Sort-Object Name)) {
        if ($SkipDirs -contains $d.Name.ToLower() -or ($d.Attributes -band [IO.FileAttributes]::ReparsePoint)) { continue }
        Find-Ap17 $d.FullName ($Depth + 1) $RootDir $RootLeaf $Acc
    }
}

# Результат поиска пишется атомарно в found-projects.json: время, корни, список (key, путь, относительный путь, дата изменения, размер)
function Invoke-Scan {
    $acc = New-Object System.Collections.ArrayList
    $roots = Get-Roots
    foreach ($r in $roots) {
        if (-not (Test-Path -LiteralPath $r -PathType Container)) { continue }
        $full = [IO.Path]::GetFullPath($r).TrimEnd('\')
        Find-Ap17 $full 0 $full (Split-Path -Leaf $full) $acc
    }
    Write-Json (Join-Path $Root 'found-projects.json') ([ordered]@{ time = (Get-Date).ToUniversalTime().ToString('o'); roots = @($roots); projects = @($acc) })
}

# Пара «ПЛК -> проект» в tia-projects.json: новая запись или замена пути (прежний путь уходит в history, не более 5)
function Set-ProjectPair([string]$Id, [string]$Plc, $Found) {
    $cfgPath = Join-Path $Root 'tia-projects.json'
    $cfg = Read-Json $cfgPath
    $h = [ordered]@{}
    if ($cfg) { $cfg.PSObject.Properties | ForEach-Object { $h[$_.Name] = $_.Value } }
    $list = @(); if ($h.Contains('projects') -and $h['projects']) { $list = @($h['projects']) }
    $segs = @($Found.rel -split '\\')
    $brand = if ($segs.Count -ge 3) { $segs[0] } else { $Plc }
    $name = $brand + ' · ' + [IO.Path]::GetFileNameWithoutExtension($Found.rel)
    $mine = $null
    foreach ($p in $list) { if ($p.id -eq $Id) { $mine = $p } }
    if ($mine) {
        if ($mine.path -ne $Found.path) {
            $hist = @(); if ($mine.history) { $hist = @($mine.history) }
            $hist = @(@($mine.path) + $hist | Select-Object -First 5)
            $mine | Add-Member -NotePropertyName history -NotePropertyValue $hist -Force
            $mine.path = $Found.path
        }
        $mine.name = $name
        $mine | Add-Member -NotePropertyName plc -NotePropertyValue $Plc -Force
    }
    else { $list += [pscustomobject]@{ id = $Id; name = $name; plc = $Plc; path = $Found.path } }
    $h['projects'] = @($list)
    Write-Json $cfgPath $h
}

# Заявка с выбором проекта из found: ключ ищется в found-projects.json, путь проверяется ещё раз (файл, .ap17, внутри разрешённых корней)
function Resolve-FoundProject($Req, [string]$JobDir) {
    $fp = Read-Json (Join-Path $Root 'found-projects.json')
    $item = $null
    if ($fp -and $fp.projects) { $item = @($fp.projects) | Where-Object { $_.key -eq $Req.found_key } | Select-Object -First 1 }
    if (-not $item) { return @{ error = 'Проект не найден в списке found-projects.json: обновите список и выберите проект снова' } }
    $path = [string]$item.path
    if ([IO.Path]::GetExtension($path).ToLower() -ne '.ap17') { return @{ error = "Файл не .ap17: $path" } }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return @{ error = "Файл проекта не найден: $path" } }
    if (-not (Test-InsideRoot $path (Get-Roots))) { return @{ error = "Путь вне разрешённых папок поиска (roots в tia-projects.json): $path" } }
    if ($Req.project -notmatch '^[A-Za-z0-9_-]{1,40}$' -or -not $Req.plc) { return @{ error = 'В заявке не указан ПЛК или проект' } }
    Set-ProjectPair ([string]$Req.project) ([string]$Req.plc) $item
    return @{ error = $null }
}

function Invoke-Job([IO.FileInfo]$ReqFile) {
    $id = $ReqFile.BaseName
    $req = Read-Json $ReqFile.FullName
    Remove-Quietly $ReqFile.FullName      # заявка принята: повторно её не берём
    if ($id -notmatch '^[A-Za-z0-9_-]{1,60}$') { Write-Log "Заявка с недопустимым именем пропущена: $id"; return }
    $jobDir = Join-Path $Root "jobs\$id"
    New-Item -ItemType Directory -Force -Path $jobDir | Out-Null
    $now = { (Get-Date).ToUniversalTime().ToString('o') }

    # проект выбран из найденных (.ap17 в разрешённых папках): служба сама проверяет путь и записывает пару «ПЛК -> проект» в tia-projects.json
    if ($req -and $req.found_key) {
        $res = Resolve-FoundProject $req $jobDir
        if ($res.error) { Set-Status $jobDir @{ state = 'error'; finished_at = (& $now); error = $res.error }; Write-Log "Заявка $id отклонена: $($res.error)"; return }
    }
    $cfg = Read-Json (Join-Path $Root 'tia-projects.json')
    $proj = $null
    if ($req -and $cfg) { $proj = @($cfg.projects) | Where-Object { $_.id -eq $req.project } | Select-Object -First 1 }
    if (-not $proj) { Set-Status $jobDir @{ state = 'error'; finished_at = (& $now); error = 'Проект не найден в списке разрешённых (tia-projects.json)' }; return }
    if (-not (Test-Path -LiteralPath $proj.path)) { Set-Status $jobDir @{ state = 'error'; finished_at = (& $now); error = "Файл проекта не найден: $($proj.path)" }; return }

    # Путь сохранения копии ZIP («Путь сохранения» на сайте): проверка правил и пробная запись до запуска TIA
    $target = $null
    if ($req.target) {
        $chk = Test-TargetDir ([string]$req.target)
        if ($chk.error) { Set-Status $jobDir @{ state = 'error'; finished_at = (& $now); target = [string]$req.target; error = $chk.error }; Write-Log "Заявка $id отклонена: $($chk.error)"; return }
        $target = $chk.path
        $werr = Test-TargetWritable $target
        if ($werr) { Set-Status $jobDir @{ state = 'error'; finished_at = (& $now); target = $target; error = $werr }; Write-Log "Заявка $id отклонена: $werr"; return }
    }

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

        $exported = 0; $skipped = 0; $failed = 0; $texts = 0; $prodiag = $false
        $man = Get-ChildItem -LiteralPath $work -Recurse -Filter 'manifest.csv' -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($man) {
            $delim = if ((Get-Content -LiteralPath $man.FullName -TotalCount 1 -Encoding UTF8) -match ';') { ';' } else { ',' }   # tia-export.ps1 пишет manifest.csv с разделителем региона
            foreach ($r in (Import-Csv -LiteralPath $man.FullName -Delimiter $delim -Encoding UTF8)) {
                # строки texts/prodiag (файлы Excel) в число блоков не входят, их ошибки считаются; сайт показывает, есть ли эти файлы в архиве
                if ($r.status -eq 'ok' -and $r.kind -eq 'texts') { $texts++ }
                if ($r.status -eq 'ok' -and $r.kind -eq 'prodiag') { $prodiag = $true }
                if ($r.status -eq 'skipped') { $skipped++ } elseif ($r.status -eq 'error') { $failed++ } elseif ($r.kind -notin 'texts', 'prodiag') { $exported++ }
            }
        }
        $zipName = 'TIA_{0}_{1}.zip' -f $proj.id, (Get-Date -Format 'yyyy-MM-dd_HH-mm')
        $zip = Join-Path $Root "out\$zipName"
        Remove-Quietly $zip
        [IO.Compression.ZipFile]::CreateFromDirectory($work, $zip)
        $size = (Get-Item -LiteralPath $zip).Length
        if ($size -lt 100) { throw 'ZIP получился пустым: в выгрузке нет файлов' }
        # копия ZIP в папку «Путь сохранения» (в out\ архив остаётся всегда: оттуда его отдаёт кнопка «⬇ ZIP»)
        $savedTo = $zip; $copyErr = $null
        if ($target) {
            $outFull = [IO.Path]::GetFullPath((Join-Path $Root 'out')).TrimEnd('\')
            if ($target -ne $outFull) {
                $dest = Join-Path $target $zipName
                try { Copy-Item -LiteralPath $zip -Destination $dest -Force -ErrorAction Stop; $savedTo = $dest }
                catch { $copyErr = "Копия не записана в $target ($($_.Exception.Message))"; $savedTo = $zip; Write-Log $copyErr }
            }
        }
        Set-Status $jobDir @{ state = 'done'; finished_at = (& $now); file = $zipName; size = $size; exported = $exported; skipped = $skipped; failed = $failed
            texts = $texts; prodiag = $prodiag; target = $target; saved_to = $savedTo; copy_error = $copyErr }
        Write-Log "Выгрузка $($proj.id) готова: $zipName, $size байт, блоков $exported, пропущено $skipped, копия: $savedTo"
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
$lastScan = [datetime]::MinValue
do {
    Send-Heartbeat
    if (((Get-Date) - $lastScan).TotalSeconds -ge $ScanEverySec) {
        try { Invoke-Scan } catch { Write-Log "Поиск проектов .ap17 не удался: $($_.Exception.Message)" }
        $lastScan = Get-Date
    }
    $next = Get-ChildItem -LiteralPath (Join-Path $Root 'requests') -Filter '*.json' -ErrorAction SilentlyContinue | Sort-Object Name | Select-Object -First 1
    if ($next) { Invoke-Job $next } elseif (-not $Once) { Start-Sleep -Seconds $PollSec }
} while (-not $Once -or $next)
