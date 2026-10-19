<#
  iis-pmm-open.ps1 - сайт IIS «PMM open»: http://10.85.176.86 (по IP, без порта) открывает PMM без входа Windows.
  Запускать ОТ ИМЕНИ АДМИНИСТРАТОРА (правой кнопкой по PowerShell, «Запуск от имени администратора»).

  Что делает:
    1. Копия настроек IIS (appcmd add backup) и запись текущих привязок в state.json (для отката).
    2. Прежний сайт на порту 80 (Default Web Site: вход Windows, заготовка X-Remote-User) теряет привязку «любой адрес»
       и получает привязки по имени ПК: alm-amm694.mk.int и alm-amm694. Его настройки (вход Windows, правило X-Remote-User) не меняются.
    3. Новый сайт «PMM open» на порту 80 для всех остальных обращений (по IP): анонимный вход, переадресация всего на http://127.0.0.1:8000.
    4. Включает прокси ARR на сервере, проверяет ответ.
  Параметры:
    -DryRun    ничего не менять, только показать, что будет сделано
    -Rollback  вернуть как было (удалить сайт «PMM open», вернуть прежнюю привязку)
  Не меняет: код и контейнеры PMM, брандмауэр, другие сайты IIS.
#>
param([switch]$DryRun, [switch]$Rollback)

$ErrorActionPreference = 'Stop'
$app = Join-Path $env:windir 'system32\inetsrv\appcmd.exe'
$dir = 'C:\plc-backups\iis'
$stateFile = Join-Path $dir 'state.json'
$newSite = 'PMM open'
$newPool = 'PMM-open'
$newPath = 'C:\inetpub\pmm-open'
$hosts = @('alm-amm694.mk.int', 'alm-amm694')

function Say([string]$m) { Write-Host ('[' + (Get-Date -Format 'HH:mm:ss') + '] ' + $m) }
function Run([string[]]$a) {
    Say ('appcmd ' + ($a -join ' '))
    if ($DryRun) { return }
    $out = & $app @a 2>&1
    if ($LASTEXITCODE -ne 0) { throw ("appcmd завершился с ошибкой (код $LASTEXITCODE): " + ($out -join ' ')) }
    if ($out) { $out | ForEach-Object { Write-Host ('    ' + $_) } }
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw 'Нужен запуск от имени администратора.' }
if (-not (Test-Path $app)) { throw 'IIS (appcmd.exe) не найден.' }
New-Item -ItemType Directory -Force -Path $dir | Out-Null

# Сайты и их привязки: SITE "имя" (id:1,bindings:http/*:80:,state:Started)
function Get-Sites {
    $res = @()
    foreach ($l in (& $app list site)) {
        if ($l -match '^SITE "(.+?)" \(id:(\d+),bindings:(.*?),state:(\w+)\)') { $res += [pscustomobject]@{ Name = $Matches[1]; Id = $Matches[2]; Bindings = $Matches[3]; State = $Matches[4] } }
    }
    return $res
}

if ($Rollback) {
    if (-not (Test-Path $stateFile)) { throw "Нет ${stateFile}: откатывать нечего." }
    $st = Get-Content $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json
    Say "Откат: возвращаю сайт '$($st.old_site)' и удаляю '$newSite'"
    if ((Get-Sites | Where-Object { $_.Name -eq $newSite })) { Run @('delete', 'site', $newSite) }
    if ((& $app list apppool | Select-String -SimpleMatch "`"$newPool`"")) { Run @('delete', 'apppool', $newPool) }
    foreach ($h in $hosts) { try { Run @('set', 'site', $st.old_site, "/-bindings.[protocol='http',bindingInformation='*:80:$h']") } catch { Say "привязка $h уже удалена" } }
    try { Run @('set', 'site', $st.old_site, "/+bindings.[protocol='http',bindingInformation='*:80:']") } catch { Say "привязка *:80 уже есть" }
    Say "Готово. Если что-то не так, полная копия настроек IIS: appcmd restore backup $($st.backup)"
    return
}

Say 'Проверка исходного состояния'
$sites = Get-Sites
$sites | ForEach-Object { Say ("  сайт '{0}': {1} ({2})" -f $_.Name, $_.Bindings, $_.State) }
$old = $sites | Where-Object { $_.Bindings -match '(^|,)http/\*:80:(,|$)' -and $_.Name -ne $newSite }
if (@($old).Count -ne 1) { throw "Ожидался ровно один сайт с привязкой http/*:80: (без имени); найдено: $(@($old).Count). Ничего не менял." }
$old = $old | Select-Object -First 1
Say "Прежний сайт на порту 80: '$($old.Name)' (привязки: $($old.Bindings))"
if (Get-Sites | Where-Object { $_.Name -eq $newSite }) { throw "Сайт '$newSite' уже есть. Для повторного запуска сначала -Rollback." }

$backup = 'pmm-before-open-site-' + (Get-Date -Format 'yyyyMMdd-HHmm')
if (-not $DryRun) { @{ old_site = $old.Name; old_bindings = $old.Bindings; backup = $backup; created = (Get-Date).ToString('o') } | ConvertTo-Json | Set-Content $stateFile -Encoding UTF8 }

Run @('add', 'backup', $backup)

# 1. Папка и web.config нового сайта (только переадресация на PMM, без X-Remote-User)
Say "Создаю $newPath"
if (-not $DryRun) {
    New-Item -ItemType Directory -Force -Path $newPath | Out-Null
    @'
<?xml version="1.0" encoding="UTF-8"?>
<!-- Сайт «PMM open»: всё, что приходит по IP, пересылается в PMM (127.0.0.1:8000), вход Windows не нужен -->
<configuration>
  <system.webServer>
    <rewrite>
      <rules>
        <rule name="PMM open proxy" stopProcessing="true">
          <match url="(.*)" />
          <action type="Rewrite" url="http://127.0.0.1:8000/{R:1}" appendQueryString="true" />
        </rule>
      </rules>
    </rewrite>
  </system.webServer>
</configuration>
'@ | Set-Content (Join-Path $newPath 'web.config') -Encoding UTF8
}

# 2. Прежний сайт: «любой адрес» заменяется привязками по имени ПК
foreach ($h in $hosts) { Run @('set', 'site', $old.Name, "/+bindings.[protocol='http',bindingInformation='*:80:$h']") }
Run @('set', 'site', $old.Name, "/-bindings.[protocol='http',bindingInformation='*:80:']")

# 3. Пул и новый сайт на «любой адрес»
Run @('add', 'apppool', "/name:$newPool", '/managedRuntimeVersion:')
Run @('add', 'site', "/name:$newSite", '/bindings:http/*:80:', "/physicalPath:$newPath")
Run @('set', 'app', "$newSite/", "/applicationPool:$newPool")
Run @('set', 'config', $newSite, '-section:system.webServer/security/authentication/anonymousAuthentication', '/enabled:true', '/commit:apphost')
Run @('set', 'config', $newSite, '-section:system.webServer/security/authentication/windowsAuthentication', '/enabled:false', '/commit:apphost')

# 4. Прокси ARR на уровне сервера (нужен для переадресации на 127.0.0.1:8000)
Run @('set', 'config', '-section:system.webServer/proxy', '/enabled:true', '/commit:apphost')
Run @('start', 'site', $newSite)

if ($DryRun) { Say 'Сухой прогон: ничего не изменено.'; return }

# 5. Проверка
Start-Sleep -Seconds 3
Say 'Проверка'
foreach ($u in 'http://10.85.176.86/api/env', 'http://127.0.0.1/api/env', 'http://alm-amm694.mk.int/') {
    try { $r = Invoke-WebRequest $u -UseBasicParsing -TimeoutSec 8 -ErrorAction Stop; Say ("  {0} -> HTTP {1} {2}" -f $u, $r.StatusCode, $r.Content.Substring(0, [Math]::Min(60, $r.Content.Length))) }
    catch { $code = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { $_.Exception.Message }; Say ("  {0} -> {1}" -f $u, $code) }
}
Say 'Ожидается: по IP и 127.0.0.1 ответ {"env":"prod"} (HTTP 200); по имени alm-amm694.mk.int HTTP 401 (вход Windows, заготовка сохранена).'
Say "Откат: powershell -File $PSCommandPath -Rollback"
