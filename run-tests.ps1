<#
  run-tests.ps1 - полный прогон автотестов на ПК сервера (где нет установленного Python), одной командой.
  Что делает: поднимает ОДНОРАЗОВУЮ базу в Docker (контейнер pmm_pg_test, только 127.0.0.1:5433, лимиты ресурсов),
  запускает pytest переносным Python из C:\pmm-tools (ничего не установлено в Windows), удаляет контейнер.
  Рабочие контейнеры, рабочая база и рабочая сеть не затрагиваются: тесты видят только свою базу general_data_hub_test.
  Защиты: отказ при занятом порте, окружение очищается от APP_ENV и ключа Telegram, поллер недоступен (адрес-пустышка).
  Нужна папка C:\pmm-tools: py311 (Python 3.11 embeddable с pip и пакетами из requirements) и node20 (node 20 zip), вне Git.
  Консоль: запускать из обычного окна PowerShell или cmd (тест update-server читает вывод в кодировке консоли Windows).
  -Tools     папка с переносными Python и node (по умолчанию C:\pmm-tools)
  -Port      порт тестовой базы на 127.0.0.1 (по умолчанию 5433, не 5432: там рабочая база)
  -KeepDb    не удалять контейнер базы после прогона
  -PytestArgs  параметры pytest одной строкой в кавычках, например: .\run-tests.ps1 -PytestArgs "-k alarms"
  Код выхода: код pytest (0 - все тесты зелёные).
#>
param(
    [string]$Tools = 'C:\pmm-tools',
    [int]$Port = 5433,
    [switch]$KeepDb,
    [string]$PytestArgs = ''
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$py = Join-Path $Tools 'py311\python.exe'
$nodeDir = Join-Path $Tools 'node20'
$cname = 'pmm_pg_test'

function Write-Log([string]$m) { Write-Host ('[' + (Get-Date -Format 'HH:mm:ss') + '] ' + $m) }

if ($Port -eq 5432) { throw 'Порт 5432 занят рабочей базой: тестовую базу на нём не поднимаем' }
if (-not (Test-Path -LiteralPath $py)) { throw ('Не найден ' + $py + ': сначала нужны переносные Python и пакеты в ' + $Tools) }
if (-not (Test-Path -LiteralPath (Join-Path $nodeDir 'node.exe'))) { throw "Не найден node в $nodeDir" }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Не найден docker' }
if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) { throw "Порт $Port уже занят" }

$code = 1
try {
    docker rm -f $cname *> $null
    Write-Log "Поднимаю одноразовую тестовую базу ($cname, 127.0.0.1:$Port)..."
    docker run -d --name $cname --cpus 2 --memory 1g -p "127.0.0.1:${Port}:5432" `
        -e POSTGRES_USER=n8n_user -e POSTGRES_PASSWORD=test_only -e POSTGRES_DB=general_data_hub_test postgres:16 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Контейнер базы не запустился' }
    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        docker exec $cname pg_isready -U n8n_user -d general_data_hub_test *> $null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) { throw 'База не стала готова за 30 секунд' }
    Start-Sleep -Seconds 1   # postgres перезапускается после инициализации: пережидаем

    # Окружение процесса тестов: своя база, без рабочих настроек
    $url = "postgresql://n8n_user:test_only@127.0.0.1:${Port}/general_data_hub_test"
    $env:TEST_DATABASE_URL = $url
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:PATH = $nodeDir + ';' + $env:PATH
    foreach ($v in 'APP_ENV', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_BOT_USERNAME', 'TELEGRAM_CA_FILE', 'DATABASE_URL') { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
    $env:POLLER_URL = 'http://127.0.0.1:9'   # адрес-пустышка: тесты не должны дотянуться до настоящего поллера

    Write-Log 'Запускаю pytest...'
    Push-Location (Join-Path $root 'tests')
    try {
        $args2 = @('-m', 'pytest', '-q', '-p', 'no:cacheprovider') + @($PytestArgs -split '\s+' | Where-Object { $_ })
        & $py @args2
        $code = $LASTEXITCODE
    }
    finally { Pop-Location }
}
finally {
    if (-not $KeepDb) {
        docker rm -f $cname *> $null
        Write-Log 'Тестовая база удалена'
    }
}
Write-Log ('Готово, код выхода ' + $code + $(if ($code -eq 0) { ': все тесты зелёные' } else { ': есть падения' }))
exit $code
