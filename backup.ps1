# Ежедневный бэкап баз данных PMM в C:\plc-backups (на сервере хранятся последние 30 дней)
# После успешного дампа копирует свежие файлы в дополнительные папки (сетевая папка, Google Drive), у каждой свой срок хранения
# После каждого запуска пишет status.json (результат, файлы, копии, место на диске C:) для раздела «Бэкапы» в справке
# Сбой копирования в дополнительную папку не считается сбоем бэкапа: код возврата 0, ошибка записывается в status.json (copies)
# Запуск вручную: powershell -ExecutionPolicy Bypass -File C:\plc-monitoring-docker-poller\backup.ps1
$ErrorActionPreference = 'Stop'
$dir = 'C:\plc-backups'
$keepDays = 30
$day = Get-Date -Format 'yyyy-MM-dd'
# Дополнительные папки: путь к сетевой папке задан сетевым адресом (а не буквой O:), чтобы задача планировщика видела её без входа в систему;
# диск G: (Google Drive) виден только в сессии пользователя: если его нет, ошибка попадает в status.json
$copyTargets = @(
    @{ name = 'Сетевая папка'; keepDays = 7; path = '\\alm-mca-fs01.mk.int\Storage\Companies\Astana Motors Manufacturing Kazakhstan\010 Body Shop\030 Maintenance\004 Automation\07_Backups\Backups_BD_PPM' },
    @{ name = 'Google Drive'; keepDays = 7; path = 'G:\Мой диск\Common_ASU_TP_BS\Backups\Backups_BD_PPM' }
)
New-Item -ItemType Directory -Force -Path $dir | Out-Null

function Write-Status([bool]$ok, [string]$err, $copies) {
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
    $json = $status | ConvertTo-Json -Depth 5
    [IO.File]::WriteAllText("$dir\status.json", $json, (New-Object System.Text.UTF8Encoding($false)))
}

# Копирует свежие дампы в дополнительные папки; каждая папка независима, сбой одной не мешает остальным
function Copy-ToTargets([string[]]$names) {
    $results = @()
    foreach ($t in $copyTargets) {
        $r = [ordered]@{ name = $t.name; path = $t.path; keep_days = $t.keepDays; ok = $false; error = ''; time = (Get-Date).ToString('o') }
        try {
            if (-not (Test-Path -LiteralPath $t.path)) { throw 'папка недоступна' }
            foreach ($n in $names) {
                Copy-Item -LiteralPath "$dir\$n" -Destination $t.path -Force
                if ((Get-Item -LiteralPath (Join-Path $t.path $n)).Length -ne (Get-Item -LiteralPath "$dir\$n").Length) { throw "размер файла $n в копии не совпадает" }
            }
            # чистим только свои дампы (hub_/n8n_ГГГГ-ММ-ДД.dump) старше срока этой папки, чужие файлы не трогаем
            Get-ChildItem -LiteralPath $t.path -File | Where-Object { $_.Name -match '^(hub|n8n)_\d{4}-\d{2}-\d{2}\.dump$' -and $_.LastWriteTime -lt (Get-Date).AddDays(-$t.keepDays) } | Remove-Item -Force
            $r.ok = $true
        }
        catch { $r.error = $_.Exception.Message }
        $results += [pscustomobject]$r
        $state = if ($r.ok) { 'OK' } else { 'FAILED: ' + $r.error }
        Write-Host ("Copy -> {0}: {1}" -f $t.name, $state)
    }
    return $results
}

try {
    $fresh = @()
    foreach ($db in @('general_data_hub_BD', 'n8n_db')) {
        $name = if ($db -eq 'n8n_db') { 'n8n' } else { 'hub' }
        docker exec postgres_db sh -c "pg_dump -U n8n_user -Fc $db > /tmp/$name.dump"
        if ($LASTEXITCODE -ne 0) { throw "pg_dump $db завершился с ошибкой ($LASTEXITCODE)" }
        docker cp "postgres_db:/tmp/$name.dump" "$dir\${name}_$day.dump"
        if ($LASTEXITCODE -ne 0) { throw "docker cp $name завершился с ошибкой ($LASTEXITCODE)" }
        if ((Get-Item "$dir\${name}_$day.dump").Length -lt 1024) { throw "дамп ${name}_$day.dump пустой" }
        $fresh += "${name}_$day.dump"
    }

    Get-ChildItem $dir -Filter '*.dump' | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$keepDays) } | Remove-Item -Force
    $copies = @(Copy-ToTargets $fresh)
    Write-Status $true '' $copies
    Write-Host "Backup done: $dir (hub_$day.dump, n8n_$day.dump), status.json updated"
}
catch {
    Write-Status $false $_.Exception.Message @()
    Write-Host "Backup FAILED: $($_.Exception.Message)"
    exit 1
}
