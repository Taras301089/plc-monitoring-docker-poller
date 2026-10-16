# Остановка тестовой копии сайта (рабочие контейнеры не трогаются: проект pmm-test)
# Запуск: .\dev-down.ps1
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
docker compose -f docker-compose.dev.yml --env-file .env.dev down
