# Запуск тестовой копии сайта только для чтения на http://127.0.0.1:8001 (только с этого ПК)
# Нужен файл .env.dev рядом со скриптом со строкой PMM_READONLY_PASSWORD=... (в Git не хранится)
# Запуск: .\dev-up.ps1
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path '.env.dev')) {
    Write-Host 'Нет файла .env.dev с PMM_READONLY_PASSWORD. Создайте его рядом с dev-up.ps1.' -ForegroundColor Red
    exit 1
}
docker compose -f docker-compose.dev.yml --env-file .env.dev up -d --build
if ($LASTEXITCODE -ne 0) { Write-Host 'Не удалось запустить тестовую копию' -ForegroundColor Red; exit 1 }
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        Invoke-WebRequest -Uri 'http://127.0.0.1:8001/api/env' -UseBasicParsing -TimeoutSec 2 | Out-Null
        $ok = $true
        break
    } catch { Start-Sleep -Seconds 2 }
}
if ($ok) { Write-Host 'Тестовая копия (только чтение) готова: http://127.0.0.1:8001' -ForegroundColor Green }
else { Write-Host 'Тестовая копия не ответила за 60 с: смотрите docker logs pmm_test_api' -ForegroundColor Red; exit 1 }
