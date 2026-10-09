# Обновление рабочего сервера PMM одной командой: бэкап, получение новой версии из Git, сборка, запуск, проверка, откат при сбое
# Запуск: powershell -NoProfile -ExecutionPolicy Bypass -File C:\plc-monitoring-docker-poller\update-server.ps1
# -DryRun: ничего не меняет, только печатает, что было бы сделано; -NoPause: не ждать Enter в конце
# -Force: пересобрать и перезапустить, даже если версия уже последняя (например, код получен вручную командой git pull)
param(
    [switch]$DryRun,
    [switch]$NoPause,
    [switch]$Force   # пересобрать и перезапустить, даже если версия уже последняя (первый запуск после ручного git pull)
)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$backupScript = Join-Path $root 'backup.ps1'
$statusFile = 'C:\plc-backups\status.json'
$logDir = 'C:\plc-backups\update-logs'
$total = 10
$services = @('fastapi-api', 'plc-monitoring-docker-poller')
$containers = @('fastapi_api', 'python-async-poller')
$apiUrl = 'http://localhost:8000'
$script:transcript = $false
$script:pulledFrom = $null   # коммит до git pull: если после pull случится неожиданная ошибка, откатываемся на него
$script:rolledBack = $false

function Write-Step([int]$n, [string]$text) {
    Write-Host ''
    Write-Host ("Шаг {0} из {1}: {2}" -f $n, $total, $text) -ForegroundColor Cyan
}
function Write-Ok([string]$text) { Write-Host ("OK: " + $text) -ForegroundColor Green }
function Write-Fail([string]$text) { Write-Host ("ОШИБКА: " + $text) -ForegroundColor Red }
function Write-Dry([string]$text) { Write-Host ("[DryRun] " + $text) -ForegroundColor Yellow }

function Short-Commit([string]$sha) {
    if (-not $sha) { return '' }
    $subject = (& git log -1 --format=%s $sha)
    return ("{0} {1}" -f $sha.Substring(0, 7), $subject)
}

# Одна проверка работоспособности: контейнеры, /api/env, /api/plcs
function Test-HealthOnce([bool]$requireProd, [ref]$why) {
    foreach ($c in $containers) {
        $state = (& docker inspect -f '{{.State.Running}}' $c)
        if ($LASTEXITCODE -ne 0 -or "$state".Trim() -ne 'true') { $why.Value = "контейнер $c не запущен"; return $false }
    }
    try {
        $envInfo = Invoke-RestMethod -Uri "$apiUrl/api/env" -TimeoutSec 6 -UseBasicParsing
    } catch { $why.Value = "/api/env не отвечает: " + $_.Exception.Message; return $false }
    if ($requireProd -and $envInfo.env -ne 'prod') { $why.Value = "/api/env вернул env=$($envInfo.env), нужно prod"; return $false }
    try {
        $plcs = Invoke-RestMethod -Uri "$apiUrl/api/plcs" -TimeoutSec 6 -UseBasicParsing
    } catch { $why.Value = "/api/plcs не отвечает: " + $_.Exception.Message; return $false }
    if (@($plcs).Count -lt 1) { $why.Value = "/api/plcs вернул пустой список"; return $false }
    return $true
}

# Цикл проверки до 90 секунд с паузой 3 секунды
function Wait-Healthy([bool]$requireProd) {
    $deadline = (Get-Date).AddSeconds(90)
    $why = ''
    while ($true) {
        if (Test-HealthOnce $requireProd ([ref]$why)) { return $true }
        if ((Get-Date) -ge $deadline) { Write-Host "Последняя причина: $why"; return $false }
        Start-Sleep -Seconds 3
    }
}

function Invoke-BuildAndUp {
    & docker compose build --no-cache @services | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "docker compose build завершился с кодом $LASTEXITCODE" }
}

function Invoke-Rollback([string]$prev, [string]$reason) {
    $script:rolledBack = $true
    Write-Host ''
    Write-Fail $reason
    Write-Host 'Запускаю откат на прежнюю версию...' -ForegroundColor Yellow
    $done = $false
    try {
        & git reset --keep $prev | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "git reset --keep завершился с кодом $LASTEXITCODE" }
        & docker compose build --no-cache @services | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "сборка прежней версии не удалась (код $LASTEXITCODE)" }
        & docker compose up -d @services | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "запуск прежней версии не удался (код $LASTEXITCODE)" }
        if (-not (Wait-Healthy $false)) { throw 'сайт не отвечает после отката' }
        $done = $true
    } catch {
        Write-Fail $_.Exception.Message
    }
    Write-Host ''
    if ($done) {
        Write-Host ("ОТКАТ ВЫПОЛНЕН: работает прежняя версия " + $prev.Substring(0, 7)) -ForegroundColor Yellow
    } else {
        Write-Host 'ОТКАТ НЕ УДАЛСЯ: нужна ручная проверка' -ForegroundColor Red
        Write-Host 'Диагностика (по одной команде):'
        Write-Host '  docker ps'
        Write-Host '  docker logs fastapi_api --tail 50'
        Write-Host '  docker logs python-async-poller --tail 50'
    }
}

function Update-Server {
    # Шаг 1: проверки окружения
    Write-Step 1 'проверка окружения (папка проекта, git, docker)'
    if (-not (Test-Path (Join-Path $root 'docker-compose.yml'))) { Write-Fail "в $root нет docker-compose.yml: скрипт нужно запускать из папки проекта"; return 1 }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Write-Fail 'не найден git'; return 1 }
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Write-Fail 'не найден docker'; return 1 }
    if (-not (Test-Path $backupScript)) { Write-Fail "не найден $backupScript"; return 1 }
    & docker info | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Fail 'docker не отвечает (Docker Desktop запущен?)'; return 1 }
    Set-Location $root
    if ($DryRun) { Write-Dry 'режим DryRun: ничего не меняется, опасные команды не вызываются' }
    Write-Ok 'окружение в порядке'

    # Шаг 2: текущий коммит
    Write-Step 2 'запоминаю текущий коммит (для отката)'
    $prev = (& git rev-parse HEAD)
    if ($LASTEXITCODE -ne 0 -or -not $prev) { Write-Fail 'git rev-parse HEAD не сработал'; return 1 }
    $prev = "$prev".Trim()
    Write-Ok ("текущий коммит " + (Short-Commit $prev))

    # Шаг 3: бэкап (всегда, до любых изменений)
    Write-Step 3 'бэкап баз данных (backup.ps1)'
    if ($DryRun) {
        Write-Dry 'запустил бы backup.ps1 и проверил: код возврата 0, в status.json ok=true, файл status.json не старше 5 минут'
    } else {
        & powershell -NoProfile -ExecutionPolicy Bypass -File $backupScript | Out-Host
        if ($LASTEXITCODE -ne 0) { Write-Fail "backup.ps1 завершился с кодом $LASTEXITCODE. Ничего не изменено."; return 1 }
        if (-not (Test-Path $statusFile)) { Write-Fail "нет файла $statusFile. Ничего не изменено."; return 1 }
        $st = Get-Content $statusFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $age = (Get-Date) - (Get-Item $statusFile).LastWriteTime
        if ($st.ok -ne $true) { Write-Fail "status.json: бэкап не успешен ($($st.error)). Ничего не изменено."; return 1 }
        if ($age.TotalMinutes -gt 5) { Write-Fail 'status.json старше 5 минут: свежий бэкап не подтверждён. Ничего не изменено.'; return 1 }
        Write-Ok 'бэкап сделан'
    }

    # Шаг 4: локальные правки отслеживаемых файлов
    Write-Step 4 'проверка локальных правок отслеживаемых файлов'
    $lines = @(& git -c core.quotepath=off status --porcelain)
    if ($LASTEXITCODE -ne 0) { Write-Fail 'git status не сработал'; return 1 }
    $changed = @()
    foreach ($l in $lines) {
        if ($l.Length -lt 4 -or $l.StartsWith('??')) { continue }
        $p = $l.Substring(3).Trim('"')
        if ($p -match ' -> ') { $p = ($p -split ' -> ')[-1].Trim('"') }
        if ($p -match '^(n8n_data|grafana_data)/') { continue }
        $changed += $p
    }
    if ($changed.Count -gt 0 -and $DryRun) {
        Write-Dry ("при реальном запуске обновление остановилось бы: изменено файлов " + $changed.Count + " (первые: " + (($changed | Select-Object -First 5) -join ', ') + ")")
    } elseif ($changed.Count -gt 0) {
        Write-Fail 'изменены отслеживаемые файлы (кроме n8n_data и grafana_data). Верните их или закоммитьте и повторите. Ничего не изменено:'
        $changed | ForEach-Object { Write-Host ("  " + $_) }
        return 1
    } else {
        Write-Ok 'локальных правок нет (n8n_data и grafana_data не учитываются)'
    }

    # Шаг 5: fetch и сравнение с origin/main
    Write-Step 5 'получение сведений об обновлении (git fetch)'
    $upToDate = $false
    if ($DryRun) {
        Write-Dry 'выполнил бы git fetch и сравнил HEAD с origin/main; если версии равны: «Уже установлена последняя версия», пропуск шагов 6-9'
    } else {
        & git fetch | Out-Host
        if ($LASTEXITCODE -ne 0) { Write-Fail 'git fetch не удался. Ничего не изменено.'; return 1 }
        $remote = (& git rev-parse 'origin/main')
        if ($LASTEXITCODE -ne 0) { Write-Fail 'нет ветки origin/main'; return 1 }
        if ("$remote".Trim() -eq $prev -and $Force) {
            Write-Ok 'Версия уже последняя, но указан -Force: пересобираю и перезапускаю'
        } elseif ("$remote".Trim() -eq $prev) {
            $upToDate = $true
            Write-Ok 'Уже установлена последняя версия'
        } else { Write-Ok ("доступна версия " + (Short-Commit "$remote".Trim())) }
    }

    if (-not $upToDate) {
        # Шаг 6: pull
        Write-Step 6 'получение новой версии (git pull --ff-only)'
        if ($DryRun) { Write-Dry 'выполнил бы git pull --ff-only (без merge-коммитов)' }
        else {
            & git pull --ff-only | Out-Host
            if ($LASTEXITCODE -ne 0) { Write-Fail 'git pull --ff-only не удался. Ничего не изменено.'; return 1 }
            $script:pulledFrom = $prev
            Write-Ok 'новая версия получена'
        }

        # Шаг 7: .env
        Write-Step 7 'файл .env (APP_ENV=prod)'
        $envFile = Join-Path $root '.env'
        if ($DryRun) { Write-Dry 'проверил бы .env: создал бы со строкой APP_ENV=prod, добавил или заменил строку APP_ENV, запись без BOM' }
        else {
            $enc = New-Object System.Text.UTF8Encoding($false)
            if (-not (Test-Path $envFile)) {
                [IO.File]::WriteAllText($envFile, "APP_ENV=prod`r`n", $enc)
                Write-Ok '.env создан: APP_ENV=prod'
            } else {
                $old = @([IO.File]::ReadAllLines($envFile))
                $idx = -1
                for ($i = 0; $i -lt $old.Count; $i++) { if ($old[$i] -match '^\s*APP_ENV\s*=') { $idx = $i; break } }
                if ($idx -lt 0) {
                    $new = $old + 'APP_ENV=prod'
                    [IO.File]::WriteAllText($envFile, (($new -join "`r`n") + "`r`n"), $enc)
                    Write-Ok 'в .env добавлена строка APP_ENV=prod'
                } else {
                    $val = ($old[$idx] -split '=', 2)[1].Trim().Trim('"').Trim("'")
                    if ($val -ne 'prod') {
                        Write-Host ("ВНИМАНИЕ: в .env было " + $old[$idx].Trim() + ", заменяю на APP_ENV=prod") -ForegroundColor Yellow
                        $old[$idx] = 'APP_ENV=prod'
                    } elseif ($old[$idx] -ne 'APP_ENV=prod') { $old[$idx] = 'APP_ENV=prod' }
                    [IO.File]::WriteAllText($envFile, (($old -join "`r`n") + "`r`n"), $enc)
                    Write-Ok '.env: APP_ENV=prod'
                }
            }
        }

        # Шаг 8: сборка
        Write-Step 8 'сборка образов fastapi-api и plc-monitoring-docker-poller (без кэша, может занять несколько минут)'
        if ($DryRun) { Write-Dry 'выполнил бы docker compose build --no-cache fastapi-api plc-monitoring-docker-poller' }
        else {
            try { Invoke-BuildAndUp }
            catch { Invoke-Rollback $prev ("сборка не удалась: " + $_.Exception.Message); return 1 }
            Write-Ok 'образы собраны'
        }

        # Шаг 9: запуск
        Write-Step 9 'запуск контейнеров (база и остальные сервисы не пересоздаются)'
        if ($DryRun) { Write-Dry 'выполнил бы docker compose up -d fastapi-api plc-monitoring-docker-poller' }
        else {
            & docker compose up -d @services | Out-Host
            if ($LASTEXITCODE -ne 0) { Invoke-Rollback $prev "docker compose up завершился с кодом $LASTEXITCODE"; return 1 }
            Write-Ok 'контейнеры запущены'
        }
    } else {
        foreach ($n in 6..9) { Write-Step $n 'пропущен (версия уже последняя)' }
    }

    # Шаг 10: итоговая проверка
    Write-Step 10 'итоговая проверка (контейнеры, /api/env = prod, /api/plcs), до 90 секунд'
    if ($DryRun) {
        Write-Dry 'проверил бы: fastapi_api и python-async-poller в состоянии running; http://localhost:8000/api/env отвечает env=prod; http://localhost:8000/api/plcs отвечает непустым списком ПЛК; иначе откат'
    } else {
        if (-not (Wait-Healthy $true)) {
            if ($upToDate) { Write-Fail 'итоговая проверка не пройдена (обновления не было, откат не нужен)'; return 1 }
            Invoke-Rollback $prev 'итоговая проверка не пройдена за 90 секунд'
            return 1
        }
        Write-Ok 'всё работает'
    }

    # Итог
    Write-Host ''
    Write-Host '=== ИТОГ ===' -ForegroundColor Green
    $now = (& git rev-parse HEAD)
    Write-Host ("Было:  " + (Short-Commit $prev))
    Write-Host ("Стало: " + (Short-Commit "$now".Trim()))
    if (-not $DryRun -and (Test-Path $statusFile)) {
        $st = Get-Content $statusFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $day = Get-Date -Format 'yyyy-MM-dd'
        $names = @($st.files | Where-Object { $_.name -like "*$day*" } | ForEach-Object { $_.name })
        Write-Host ("Свежие дампы: " + ($names -join ', '))
    }
    if ($DryRun) { Write-Dry 'DryRun завершён, изменений нет' }
    Write-Host 'Вручную после обновления: проверить порядок ПЛК и значок связи, при необходимости настроить «Запись переменных» в «Тренды».'
    return 0
}

$code = 1
try {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $logFile = Join-Path $logDir ("update-{0}.log" -f (Get-Date -Format 'yyyy-MM-dd_HHmm'))
    Start-Transcript -Path $logFile | Out-Null
    $script:transcript = $true
    Write-Host "Журнал: $logFile"
} catch {
    Write-Host "Журнал не ведётся: $($_.Exception.Message)"
}
try {
    $code = [int](Update-Server | Select-Object -Last 1)
} catch {
    Write-Fail $_.Exception.Message
    $code = 1
    if ($script:pulledFrom -and -not $script:rolledBack) { Invoke-Rollback $script:pulledFrom ('неожиданная ошибка после получения новой версии: ' + $_.Exception.Message) }
}
Write-Host ''
if ($code -eq 0) { Write-Host 'Обновление завершено успешно.' -ForegroundColor Green }
else { Write-Host 'Обновление завершено с ошибкой.' -ForegroundColor Red }
if ($script:transcript) { try { Stop-Transcript | Out-Null } catch { } }
if (-not $NoPause) { Read-Host 'Нажмите Enter для закрытия окна' | Out-Null }
exit $code
