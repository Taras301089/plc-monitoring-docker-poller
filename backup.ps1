# Ежедневный бэкап баз данных PLC Monitoring в C:\plc-backups (хранятся последние 30 дней)
# После каждого запуска пишет status.json (результат, файлы, место на диске C:) для раздела «Бэкапы» в справке
# Запуск вручную: powershell -ExecutionPolicy Bypass -File C:\plc-monitoring-docker-poller\backup.ps1
$ErrorActionPreference = 'Stop'
$dir = 'C:\plc-backups'
$keepDays = 30
$day = Get-Date -Format 'yyyy-MM-dd'
New-Item -ItemType Directory -Force -Path $dir | Out-Null

function Write-Status([bool]$ok, [string]$err) {
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
    }
    $json = $status | ConvertTo-Json -Depth 4
    [IO.File]::WriteAllText("$dir\status.json", $json, (New-Object System.Text.UTF8Encoding($false)))
}

try {
    foreach ($db in @('general_data_hub_BD', 'n8n_db')) {
        $name = if ($db -eq 'n8n_db') { 'n8n' } else { 'hub' }
        docker exec postgres_db sh -c "pg_dump -U n8n_user -Fc $db > /tmp/$name.dump"
        if ($LASTEXITCODE -ne 0) { throw "pg_dump $db завершился с ошибкой ($LASTEXITCODE)" }
        docker cp "postgres_db:/tmp/$name.dump" "$dir\${name}_$day.dump"
        if ($LASTEXITCODE -ne 0) { throw "docker cp $name завершился с ошибкой ($LASTEXITCODE)" }
        if ((Get-Item "$dir\${name}_$day.dump").Length -lt 1024) { throw "дамп ${name}_$day.dump пустой" }
    }

    Get-ChildItem $dir -Filter '*.dump' | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$keepDays) } | Remove-Item -Force
    Write-Status $true ''
    Write-Host "Backup done: $dir (hub_$day.dump, n8n_$day.dump), status.json updated"
}
catch {
    Write-Status $false $_.Exception.Message
    Write-Host "Backup FAILED: $($_.Exception.Message)"
    exit 1
}
