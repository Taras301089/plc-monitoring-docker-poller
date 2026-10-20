# Ежедневный бэкап баз данных PMM в C:\plc-backups (на сервере хранятся последние 30 дней)
# После успешного дампа копирует свежие файлы в дополнительные папки (сетевая папка, Google Drive), у каждой свой срок хранения
# Кроме дампов собирает набор конфигурации config_ГГГГ-ММ-ДД.zip (.env, роли базы, данные n8n и Grafana, TIA bridge, задачи планировщика, IIS, сертификат);
# в нём секреты, поэтому набор копируется только в папки с признаком includeConfig (сетевая папка), на Google Drive он не отправляется
# После каждого запуска пишет status.json (результат, файлы, копии, набор конфигурации, место на диске C:) для раздела «Бэкапы» в справке
# Сбой копирования или набора конфигурации не считается сбоем бэкапа баз: код возврата 0, ошибка записывается в status.json (copies, config)
# Запуск вручную: powershell -ExecutionPolicy Bypass -File C:\plc-monitoring-docker-poller\backup.ps1
# Запуск при входе в систему: ... -File C:\plc-monitoring-docker-poller\backup.ps1 -OnlyIfMissing (если дампы за сегодня уже есть, выходит, ничего не делая)
# Параметры для проверки: -Dir (папка бэкапов), -ProjectDir (где лежит .env), -ConfigOnly (только набор конфигурации), -TargetsFile (JSON со своими папками копий)
param(
    [switch]$OnlyIfMissing,
    [switch]$ConfigOnly,
    [string]$Dir = 'C:\plc-backups',
    [string]$ProjectDir = 'C:\plc-monitoring-docker-poller',
    [string]$TargetsFile = ''
)
$ErrorActionPreference = 'Stop'
$dir = $Dir
$keepDays = 30
$day = Get-Date -Format 'yyyy-MM-dd'
# Дополнительные папки: путь к сетевой папке задан сетевым адресом (а не буквой O:), чтобы задача планировщика видела её без входа в систему;
# диск G: (Google Drive) виден только в сессии пользователя: если его нет, ошибка попадает в status.json
# includeConfig = $true: туда копируется и config_*.zip (с секретами); на Google Drive набор не отправляется
$copyTargets = @(
    @{ name = 'Сетевая папка'; keepDays = 7; includeConfig = $true; path = '\\alm-mca-fs01.mk.int\Storage\Companies\Astana Motors Manufacturing Kazakhstan\010 Body Shop\030 Maintenance\004 Automation\07_Backups\Backups_BD_PPM' },
    @{ name = 'Google Drive'; keepDays = 7; includeConfig = $false; path = 'G:\Мой диск\Common_ASU_TP_BS\Backups\Backups_BD_PPM' }
)
if ($TargetsFile) {
    $loaded = @(ConvertFrom-Json (Get-Content -LiteralPath $TargetsFile -Raw -Encoding UTF8))
    $copyTargets = @(foreach ($grp in $loaded) { foreach ($x in $grp) {
        @{ name = $x.name; keepDays = [int]$x.keepDays; includeConfig = [bool]$x.includeConfig; path = $x.path }
    } })
}
New-Item -ItemType Directory -Force -Path $dir | Out-Null

function Write-Log([string]$msg) {
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Write-Host $line
    try { Add-Content -LiteralPath "$dir\backup.log" -Value $line -Encoding UTF8 } catch { }
}

# Запуск при входе: если оба дампа за сегодня уже есть и не пустые, ничего не делаем (status.json не трогаем)
if ($OnlyIfMissing -and -not $ConfigOnly) {
    $have = $true
    foreach ($n in @('hub', 'n8n')) {
        $f = "$dir\${n}_$day.dump"
        if (-not ((Test-Path -LiteralPath $f) -and (Get-Item -LiteralPath $f).Length -ge 1024)) { $have = $false }
    }
    if ($have) {
        Write-Log "OnlyIfMissing: дампы за $day уже есть, бэкап не нужен"
        exit 0
    }
    Write-Log "OnlyIfMissing: дампов за $day нет, делаю бэкап"
}

function Write-Status([bool]$ok, [string]$err, $copies, $config = $null) {
    $disk = Get-PSDrive -Name C
    $files = @(Get-ChildItem $dir -Filter '*.dump' | Sort-Object LastWriteTime -Descending | ForEach-Object {
        [ordered]@{ name = $_.Name; size = $_.Length; time = $_.LastWriteTime.ToString('o') }
    })
    $status = [ordered]@{
        time       = (Get-Date).ToString('o')
        ok         = $ok
        error      = $err
        folder     = $dir
        keep_days  = $keepDays
        disk_total = [int64]($disk.Used + $disk.Free)
        disk_free  = [int64]$disk.Free
        files      = $files
        copies     = @($copies)
    }
    if ($null -ne $config) { $status['config'] = $config }
    $json = $status | ConvertTo-Json -Depth 5
    [IO.File]::WriteAllText("$dir\status.json", $json, (New-Object System.Text.UTF8Encoding($false)))
}

# Набор конфигурации: config_ГГГГ-ММ-ДД.zip. Каждая часть независима: сбой пишется в manifest.txt и в missing, остальное собирается дальше.
# Секреты (.env, роли базы, ключ n8n) только лежат в архиве, в вывод и журнал их содержимое не попадает
function New-ConfigSet {
    $ErrorActionPreference = 'Continue'
    $zipName = "config_$day.zip"
    $res = [ordered]@{ ok = $false; file = $zipName; size = 0; missing = @() }
    # временная папка сборки в корне диска (короткий путь): в jobs\ длинные имена, и zip на длинном пути (>260 знаков) не собирается
    $stage = Join-Path ([IO.Path]::GetPathRoot((Resolve-Path -LiteralPath $dir).Path)) 'pmm-cfg-stage'
    $lines = New-Object System.Collections.ArrayList
    $missing = New-Object System.Collections.ArrayList
    function Add-Line([string]$s) { [void]$lines.Add($s) }
    function Add-Miss([string]$part, [string]$why) { [void]$missing.Add($part); Add-Line("ПРОПУЩЕНО  ${part}: $why") }
    function Add-Ok([string]$part, [string]$path) {
        $sz = 0
        if (Test-Path -LiteralPath $path) {
            if ((Get-Item -LiteralPath $path).PSIsContainer) { $sz = (Get-ChildItem -LiteralPath $path -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum }
            else { $sz = (Get-Item -LiteralPath $path).Length }
        }
        Add-Line(("вошло      {0}: {1} Б" -f $part, [int64]$sz))
    }
    try {
        if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
        New-Item -ItemType Directory -Force -Path $stage | Out-Null
        Add-Line "Набор конфигурации PMM, собран $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') на $env:COMPUTERNAME"
        Add-Line 'ВНИМАНИЕ: в наборе секреты (.env, роли базы, ключ n8n). Хранить только на сервере и в сетевой папке предприятия, на Google Drive не отправлять.'
        Add-Line ''

        # а) .env
        try {
            $envFile = Join-Path $ProjectDir '.env'
            if (-not (Test-Path -LiteralPath $envFile)) { throw 'файл не найден' }
            Copy-Item -LiteralPath $envFile -Destination "$stage\.env" -Force
            Add-Ok '.env' "$stage\.env"
        } catch { Add-Miss '.env' $_.Exception.Message }

        # б) роли и права базы
        try {
            docker exec postgres_db sh -c "pg_dumpall -U n8n_user --globals-only > /tmp/globals.sql"
            if ($LASTEXITCODE -ne 0) { throw "pg_dumpall завершился с ошибкой ($LASTEXITCODE)" }
            docker cp "postgres_db:/tmp/globals.sql" "$stage\globals.sql"
            if ($LASTEXITCODE -ne 0) { throw "docker cp завершился с ошибкой ($LASTEXITCODE)" }
            docker exec postgres_db rm -f /tmp/globals.sql
            if ((Get-Item "$stage\globals.sql").Length -lt 100) { throw 'файл ролей пустой' }
            Add-Ok 'globals.sql (роли и права базы)' "$stage\globals.sql"
        } catch { Add-Miss 'globals.sql (роли и права базы)' $_.Exception.Message }

        # в), г) данные n8n и Grafana: что именно примонтировано, берём у работающего контейнера (папка или том), читаем только на чтение
        $img = $null
        $have = @(docker images --format '{{.Repository}}:{{.Tag}}')
        foreach ($c in @('alpine:latest', 'python:3.12-alpine', 'node:20-alpine')) {
            if ($have -contains $c) { $img = $c; break }
        }
        $apps = @(
            @{ cont = 'n8n_app'; part = 'n8n'; file = 'n8n_data.tgz'; fallback = 'n8n_data'; target = '/home/node/.n8n' },
            @{ cont = 'grafana_app'; part = 'Grafana'; file = 'grafana_data.tgz'; fallback = ''; target = '/var/lib/grafana' }
        )
        foreach ($app in $apps) {
            $label = "$($app.file) (данные $($app.part))"
            try {
                if (-not $img) { throw 'нет локального образа alpine для tar' }
                $src = $null
                $mj = docker inspect $app.cont --format '{{json .Mounts}}'
                if ($LASTEXITCODE -eq 0 -and $mj) {
                    $m = @($mj | ConvertFrom-Json) | Where-Object { $_.Destination -eq $app.target } | Select-Object -First 1
                    if ($m) { $src = if ($m.Type -eq 'volume') { $m.Name } else { $m.Source } }
                }
                if (-not $src) { $src = $app.fallback }
                if (-not $src) { throw 'не удалось определить, что примонтировано' }
                docker run --rm -v "${src}:/data:ro" -v "${stage}:/out" $img sh -c "tar czf /out/$($app.file) -C /data ."
                if ($LASTEXITCODE -ne 0) { throw "tar завершился с ошибкой ($LASTEXITCODE)" }
                if (-not (Test-Path "$stage\$($app.file)")) { throw 'архив не создан' }
                $from = if ($src -match '^[A-Za-z]:') { $src } else { 'том ' + $src }
                Add-Ok "$label, источник: $from" "$stage\$($app.file)"
            } catch { Add-Miss $label $_.Exception.Message }
        }

        # д) C:\tia-bridge: настройки, логика, диагностика, запросы, задания и архивы выгрузок; без work\ и probe-*
        try {
            $tia = 'C:\tia-bridge'
            if (-not (Test-Path -LiteralPath $tia)) { throw 'папка не найдена' }
            New-Item -ItemType Directory -Force -Path "$stage\tia-bridge" | Out-Null
            $capBytes = 200MB
            $total = 0
            foreach ($f in @('tia-projects.json', 'bridge.json')) {
                if (Test-Path -LiteralPath "$tia\$f") {
                    Copy-Item -LiteralPath "$tia\$f" -Destination "$stage\tia-bridge\$f" -Force
                    $total += (Get-Item -LiteralPath "$tia\$f").Length
                    Add-Line "вошло      tia-bridge\${f}"
                }
                else { Add-Line "нет        tia-bridge\${f} (на сервере не найден)" }
            }
            foreach ($d in @('logic', 'prodiag', 'requests', 'jobs', 'out')) {
                if (-not (Test-Path -LiteralPath "$tia\$d")) { Add-Line "нет        tia-bridge\$d\ (на сервере не найдена)"; continue }
                $sz = [int64](Get-ChildItem -LiteralPath "$tia\$d" -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
                if ($total + $sz -gt $capBytes) {
                    Add-Line ("ПРОПУЩЕНО  tia-bridge\{0}\: {1} Б, превышен предел {2} МБ на всю папку tia-bridge" -f $d, $sz, ($capBytes / 1MB))
                    [void]$missing.Add("tia-bridge\$d")
                    continue
                }
                robocopy "$tia\$d" "$stage\tia-bridge\$d" /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
                if ($LASTEXITCODE -ge 8) { Add-Miss "tia-bridge\$d" "robocopy завершился с кодом $LASTEXITCODE"; continue }
                $total += $sz
                Add-Line ("вошло      tia-bridge\{0}\: {1} Б" -f $d, $sz)
            }
            Add-Line 'не берётся по правилу: tia-bridge\work\, tia-bridge\probe-*\, tia-bridge\logs\'
        } catch { Add-Miss 'tia-bridge' $_.Exception.Message }

        # е) задачи планировщика
        New-Item -ItemType Directory -Force -Path "$stage\scheduled-tasks" | Out-Null
        foreach ($tn in @('PMM backup', 'PMM copy reports', 'PMM TIA bridge')) {
            try {
                $xml = Export-ScheduledTask -TaskName $tn -ErrorAction Stop
                [IO.File]::WriteAllText("$stage\scheduled-tasks\$($tn -replace ' ', '_').xml", ($xml -join "`r`n"), (New-Object System.Text.UTF8Encoding($false)))
                Add-Line "вошло      задача планировщика «$tn»"
            } catch { Add-Miss "задача планировщика «$tn»" $_.Exception.Message }
        }

        # ж) IIS: applicationHost.config обычно читает только администратор; web.config сайтов лежат в C:\inetpub
        try {
            New-Item -ItemType Directory -Force -Path "$stage\iis" | Out-Null
            $ah = 'C:\Windows\System32\inetsrv\config\applicationHost.config'
            try { Copy-Item -LiteralPath $ah -Destination "$stage\iis\applicationHost.config" -Force -ErrorAction Stop; Add-Line 'вошло      iis\applicationHost.config' }
            catch { Add-Miss 'IIS applicationHost.config' 'не прочитан (нет прав чтения у пользователя, под которым идёт бэкап)' }
            $skip = @('history', 'logs', 'temp', 'custerr', 'ftproot', 'AppPools')
            $siteCfgs = @(Get-ChildItem 'C:\inetpub' -Directory -ErrorAction SilentlyContinue | Where-Object { $skip -notcontains $_.Name } |
                ForEach-Object { Get-Item -LiteralPath (Join-Path $_.FullName 'web.config') -ErrorAction SilentlyContinue })
            foreach ($w in $siteCfgs) {
                Copy-Item -LiteralPath $w.FullName -Destination "$stage\iis\$($w.Directory.Name)_web.config" -Force
                Add-Line "вошло      iis\$($w.Directory.Name)_web.config"
            }
            if ($siteCfgs.Count -eq 0) { Add-Miss 'IIS web.config сайтов' 'в C:\inetpub не найдены' }
        } catch { Add-Miss 'IIS' $_.Exception.Message }

        # з) корпоративный сертификат
        try {
            Copy-Item -LiteralPath 'C:\pmm-certs\corp-ca.pem' -Destination "$stage\corp-ca.pem" -Force -ErrorAction Stop
            Add-Ok 'corp-ca.pem' "$stage\corp-ca.pem"
        } catch { Add-Miss 'corp-ca.pem' $_.Exception.Message }

        Add-Line ''
        $none = if ($missing.Count) { $missing -join '; ' } else { 'ничего' }
        Add-Line "Не вошло: $none"
        [IO.File]::WriteAllText("$stage\manifest.txt", (($lines -join "`r`n") + "`r`n"), (New-Object System.Text.UTF8Encoding($true)))

        $zip = Join-Path $dir $zipName
        if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::CreateFromDirectory($stage, $zip, [IO.Compression.CompressionLevel]::Optimal, $false)
        $res.size = (Get-Item -LiteralPath $zip).Length
        $res.ok = ($missing.Count -eq 0)
    }
    catch {
        [void]$missing.Add('набор целиком: ' + $_.Exception.Message)
        $res.size = 0
        if ($zip -and (Test-Path -LiteralPath $zip)) { Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue }
    }
    finally { if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue } }
    $res.missing = @($missing)
    $none = if ($missing.Count) { $missing -join '; ' } else { 'ничего' }
    Write-Log ("Config set: {0}, {1} Б, не вошло: {2}" -f $zipName, $res.size, $none)
    return $res
}

# Копирует свежие дампы (и набор конфигурации только туда, где includeConfig) в дополнительные папки; каждая папка независима, сбой одной не мешает остальным
function Copy-ToTargets([string[]]$names, [string]$configName = '') {
    $results = @()
    foreach ($t in $copyTargets) {
        $r = [ordered]@{ name = $t.name; path = $t.path; keep_days = $t.keepDays; ok = $false; error = ''; time = (Get-Date).ToString('o') }
        try {
            if (-not (Test-Path -LiteralPath $t.path)) { throw 'папка недоступна' }
            $list = @($names)
            if ($t.includeConfig -and $configName -and (Test-Path -LiteralPath "$dir\$configName")) { $list += $configName }
            foreach ($n in $list) {
                Copy-Item -LiteralPath "$dir\$n" -Destination $t.path -Force
                if ((Get-Item -LiteralPath (Join-Path $t.path $n)).Length -ne (Get-Item -LiteralPath "$dir\$n").Length) { throw "размер файла $n в копии не совпадает" }
            }
            # чистим только свои файлы (hub_/n8n_ГГГГ-ММ-ДД.dump, а где разрешён набор, ещё config_ГГГГ-ММ-ДД.zip) старше срока этой папки, чужие не трогаем
            $pattern = if ($t.includeConfig) { '^((hub|n8n)_\d{4}-\d{2}-\d{2}\.dump|config_\d{4}-\d{2}-\d{2}\.zip)$' } else { '^(hub|n8n)_\d{4}-\d{2}-\d{2}\.dump$' }
            Get-ChildItem -LiteralPath $t.path -File | Where-Object { $_.Name -match $pattern -and $_.LastWriteTime -lt (Get-Date).AddDays(-$t.keepDays) } | Remove-Item -Force
            $r.ok = $true
        }
        catch { $r.error = $_.Exception.Message }
        $results += [pscustomobject]$r
        $state = if ($r.ok) { 'OK' } else { 'FAILED: ' + $r.error }
        Write-Host ("Copy -> {0}: {1}" -f $t.name, $state)
    }
    return $results
}

$dumpErr = ''
$fresh = @()
if (-not $ConfigOnly) {
    try {
        foreach ($db in @('general_data_hub_BD', 'n8n_db')) {
            $name = if ($db -eq 'n8n_db') { 'n8n' } else { 'hub' }
            docker exec postgres_db sh -c "pg_dump -U n8n_user -Fc $db > /tmp/$name.dump"
            if ($LASTEXITCODE -ne 0) { throw "pg_dump $db завершился с ошибкой ($LASTEXITCODE)" }
            docker cp "postgres_db:/tmp/$name.dump" "$dir\${name}_$day.dump"
            if ($LASTEXITCODE -ne 0) { throw "docker cp $name завершился с ошибкой ($LASTEXITCODE)" }
            if ((Get-Item "$dir\${name}_$day.dump").Length -lt 1024) { throw "дамп ${name}_$day.dump пустой" }
            $fresh += "${name}_$day.dump"
        }
    }
    catch { $dumpErr = $_.Exception.Message }
}

# Набор конфигурации собирается независимо от дампов и никогда не роняет бэкап
$config = $null
try { $config = New-ConfigSet }
catch { $config = [ordered]@{ ok = $false; file = "config_$day.zip"; size = 0; missing = @('набор целиком: ' + $_.Exception.Message) } }

try {
    if ($dumpErr) { throw $dumpErr }
    Get-ChildItem $dir -Filter '*.dump' | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$keepDays) } | Remove-Item -Force
    Get-ChildItem $dir -Filter 'config_*.zip' | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$keepDays) } | Remove-Item -Force
    $cfgName = if ($config.size -gt 0) { $config.file } else { '' }
    $copies = @(Copy-ToTargets $fresh $cfgName)
    Write-Status $true '' $copies $config
    Write-Host "Backup done: $dir (hub_$day.dump, n8n_$day.dump, config_$day.zip), status.json updated"
}
catch {
    Write-Status $false $_.Exception.Message @() $config
    Write-Host "Backup FAILED: $($_.Exception.Message)"
    exit 1
}
