# Ежедневный бэкап баз данных PLC Monitoring в C:\plc-backups (хранятся последние 30 дней)
# Запуск вручную: powershell -ExecutionPolicy Bypass -File C:\plc-monitoring-docker-poller\backup.ps1
$ErrorActionPreference = 'Stop'
$dir = 'C:\plc-backups'
$day = Get-Date -Format 'yyyy-MM-dd'
New-Item -ItemType Directory -Force -Path $dir | Out-Null

foreach ($db in @('general_data_hub_BD', 'n8n_db')) {
    $name = if ($db -eq 'n8n_db') { 'n8n' } else { 'hub' }
    docker exec postgres_db sh -c "pg_dump -U n8n_user -Fc $db > /tmp/$name.dump"
    docker cp "postgres_db:/tmp/$name.dump" "$dir\${name}_$day.dump"
}

Get-ChildItem $dir -Filter '*.dump' | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } | Remove-Item -Force
Write-Host "Backup done: $dir (hub_$day.dump, n8n_$day.dump)"
