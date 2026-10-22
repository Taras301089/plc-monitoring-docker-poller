<#
  tia-export.ps1 - выгрузка логики ПЛК из проекта TIA Portal V17 в XML (SimaticML) через TIA Openness.
  Открывает КОПИЮ проекта без окна TIA и выгружает для каждого ПЛК: программные блоки (OB, FB, FC, DB),
  типы данных (UDT) и таблицы тегов; контроли ProDiag в Excel (ProDiag_Supervisions.xlsx) и тексты проекта
  на каждом языке (ProjectTexts_<язык>.xlsx). Проект не изменяется и не сохраняется.
  Блоки с защитой know-how, F-блоки и блоки, которые TIA не может выгрузить (не скомпилированы), пропускаются
  и перечисляются в отчёте manifest.csv.
  Требования: TIA Portal V17 на этом ПК, учётка Windows в группе «Siemens TIA Openness».
  При первом запуске TIA спросит разрешение на доступ Openness: ответить «Да для всех».
  -Project  путь к файлу .ap17 (копия проекта)
  -OutDir   куда сложить XML (по умолчанию TIA_PROJECTS_ROOT или Documents\Tia_Opennnes_Export вне репозитория)
  -Compile  перед выгрузкой скомпилировать программу в копии (если блоки не выгружаются как несогласованные)
  -NoPause  не ждать нажатия Enter в конце.
  Код выхода: 0 успех, 1 ошибка.
#>
param(
    [Parameter(Mandatory = $true)][string]$Project,
    [string]$OutDir = $(if ([string]::IsNullOrWhiteSpace($env:TIA_PROJECTS_ROOT)) {
        [IO.Path]::Combine([Environment]::GetFolderPath('MyDocuments'), 'Tia_Opennnes_Export')
    } else { $env:TIA_PROJECTS_ROOT }),
    [switch]$Compile,
    [switch]$NoPause
)

$ErrorActionPreference = 'Stop'
$apiDll = 'C:\Program Files\Siemens\Automation\Portal V17\PublicAPI\V17\Siemens.Engineering.dll'

function Write-Log([string]$Message) {
    Write-Host ('[' + (Get-Date -Format 'HH:mm:ss') + '] ' + $Message)
}

# Имя файла из имени блока: недопустимые для Windows символы заменяются на «_»
function Get-SafeName([string]$Name) {
    $bad = [IO.Path]::GetInvalidFileNameChars()
    -join ($Name.ToCharArray() | ForEach-Object { if ($bad -contains $_) { '_' } else { $_ } })
}

# DeviceItem.GetService<T>() вызывается через отражение: PowerShell 5.1 не умеет вызывать обобщённые методы напрямую
function Get-Service2($Item, [type]$Type) {
    $m = [Siemens.Engineering.IEngineeringServiceProvider].GetMethod('GetService').MakeGenericMethod($Type)
    $m.Invoke($Item, $null)
}

$manifest = New-Object System.Collections.Generic.List[object]

function Add-Row($Plc, $Kind, $Path, $Name, $Number, $Lang, $Status, $Note) {
    $manifest.Add([pscustomobject]@{
        plc = $Plc; kind = $Kind; path = $Path; name = $Name; number = $Number
        language = $Lang; status = $Status; note = $Note
    })
}

function Export-One($Obj, [string]$Dir, [string]$Plc, [string]$Kind, [string]$RelPath) {
    $name = $Obj.Name
    $number = ''; $lang = ''
    try { $number = $Obj.Number } catch {}
    try { $lang = [string]$Obj.ProgrammingLanguage } catch {}
    $kh = $false
    try { $kh = [bool]$Obj.IsKnowHowProtected } catch {}
    if ($kh) { Add-Row $Plc $Kind $RelPath $name $number $lang 'skipped' 'know-how'; return }
    if ($lang -like 'F_*') { Add-Row $Plc $Kind $RelPath $name $number $lang 'skipped' 'F-блок'; return }
    if (-not (Test-Path -LiteralPath $Dir)) { New-Item -ItemType Directory -Path $Dir -Force | Out-Null }
    $file = Join-Path $Dir ((Get-SafeName $name) + '.xml')
    if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file -Force }
    try {
        $Obj.Export([IO.FileInfo]$file, [Siemens.Engineering.ExportOptions]::WithDefaults)
        Add-Row $Plc $Kind $RelPath $name $number $lang 'ok' ''
    } catch {
        $msg = $_.Exception.Message
        if ($_.Exception.InnerException) { $msg = $_.Exception.InnerException.Message }
        Add-Row $Plc $Kind $RelPath $name $number $lang 'error' (($msg -replace '\s+', ' ').Trim())
    }
}

# Рекурсивный обход групп: блоки, типы, таблицы тегов
function Export-BlockGroup($Group, [string]$Dir, [string]$Plc, [string]$Rel) {
    foreach ($b in $Group.Blocks) { Export-One $b $Dir $Plc 'block' $Rel }
    foreach ($g in $Group.Groups) {
        Export-BlockGroup $g (Join-Path $Dir (Get-SafeName $g.Name)) $Plc ($Rel + '/' + $g.Name)
    }
}
function Export-TypeGroup($Group, [string]$Dir, [string]$Plc, [string]$Rel) {
    foreach ($t in $Group.Types) { Export-One $t $Dir $Plc 'type' $Rel }
    foreach ($g in $Group.Groups) {
        Export-TypeGroup $g (Join-Path $Dir (Get-SafeName $g.Name)) $Plc ($Rel + '/' + $g.Name)
    }
}
function Export-TagGroup($Group, [string]$Dir, [string]$Plc, [string]$Rel) {
    foreach ($t in $Group.TagTables) { Export-One $t $Dir $Plc 'tagtable' $Rel }
    foreach ($g in $Group.Groups) {
        Export-TagGroup $g (Join-Path $Dir (Get-SafeName $g.Name)) $Plc ($Rel + '/' + $g.Name)
    }
}

# Все программы ПЛК проекта: устройства в группах и без групп, вложенные модули
$script:plcList = New-Object System.Collections.Generic.List[object]
function Find-PlcInItems($Items) {
    foreach ($it in $Items) {
        $sc = Get-Service2 $it ([Siemens.Engineering.HW.Features.SoftwareContainer])
        if ($sc -and $sc.Software -is [Siemens.Engineering.SW.PlcSoftware]) { $script:plcList.Add($sc.Software) }
        Find-PlcInItems $it.DeviceItems
    }
}
function Find-PlcInDevices($Devices) {
    foreach ($d in $Devices) { Find-PlcInItems $d.DeviceItems }
}
function Find-PlcInGroups($Groups) {
    foreach ($g in $Groups) {
        Find-PlcInDevices $g.Devices
        Find-PlcInGroups $g.Groups
    }
}

$exitCode = 0
$tia = $null
$proj = $null
try {
    if (-not (Test-Path -LiteralPath $apiDll)) { throw "Не найден $apiDll (TIA Portal V17 с компонентом Openness)" }
    if (-not (Test-Path -LiteralPath $Project)) { throw "Не найден проект: $Project" }
    $groups = (whoami /groups) -join "`n"
    if ($groups -notmatch 'Siemens TIA Openness') {
        throw 'Учётка не в группе «Siemens TIA Openness» (или после добавления не было выхода из Windows)'
    }
    # LoadFrom, а не Add-Type: Add-Type падает на вспомогательных типах библиотеки, которые скрипту не нужны
    [void][Reflection.Assembly]::LoadFrom($apiDll)

    Write-Log 'Запуск TIA Portal без окна...'
    $tia = New-Object Siemens.Engineering.TiaPortal([Siemens.Engineering.TiaPortalMode]::WithoutUserInterface)
    Write-Log "Открываю проект $Project"
    $proj = $tia.Projects.Open([IO.FileInfo]$Project)

    Find-PlcInDevices $proj.Devices
    Find-PlcInGroups $proj.DeviceGroups
    Find-PlcInDevices $proj.UngroupedDevicesGroup.Devices
    $plcs = $script:plcList | Sort-Object -Property Name -Unique
    if (-not $plcs) { throw 'В проекте не найдено ни одного ПЛК' }
    Write-Log ('Найдено ПЛК: ' + (($plcs | ForEach-Object { $_.Name }) -join ', '))

    $stamp = Get-Date -Format 'yyyy-MM-dd_HH-mm'
    $root = Join-Path $OutDir $stamp
    New-Item -ItemType Directory -Path $root -Force | Out-Null

    foreach ($sw in $plcs) {
        $plc = $sw.Name
        $plcDir = Join-Path $root (Get-SafeName $plc)
        if ($Compile) {
            Write-Log "$plc - компиляция программы (в копии проекта)..."
            $comp = Get-Service2 $sw ([Siemens.Engineering.Compiler.ICompilable])
            $res = $comp.Compile()
            Write-Log ("$plc - компиляция: " + $res.State + ', ошибок ' + $res.ErrorCount + ', предупреждений ' + $res.WarningCount)
        }
        Write-Log "$plc - выгрузка блоков..."
        Export-BlockGroup $sw.BlockGroup (Join-Path $plcDir 'Blocks') $plc 'Blocks'
        Write-Log "$plc - выгрузка типов (UDT)..."
        Export-TypeGroup $sw.TypeGroup (Join-Path $plcDir 'Types') $plc 'Types'
        Write-Log "$plc - выгрузка таблиц тегов..."
        Export-TagGroup $sw.TagTableGroup (Join-Path $plcDir 'Tags') $plc 'Tags'
        # Контроли ProDiag (сигнал, условие, категория, задержка) и настройки блоков *_PD: сами блоки ProDiag
        # Openness V17 в XML не выгружает, поэтому их содержимое берётся отдельной выгрузкой в Excel
        Write-Log "$plc - выгрузка контролей ProDiag..."
        $pdFile = Join-Path $plcDir 'ProDiag_Supervisions.xlsx'
        try {
            $pd = Get-Service2 $sw ([Siemens.Engineering.SW.Supervision.SupervisionProvider])
            if (-not (Test-Path -LiteralPath $plcDir)) { New-Item -ItemType Directory -Path $plcDir -Force | Out-Null }
            $res = $pd.ExportSupervisionsToXlsx([IO.FileInfo]$pdFile)
            if (Test-Path -LiteralPath $pdFile) { Add-Row $plc 'prodiag' '' 'ProDiag_Supervisions.xlsx' '' '' 'ok' '' }
            else { Add-Row $plc 'prodiag' '' 'ProDiag_Supervisions.xlsx' '' '' 'error' ('файл не создан: ' + $res.State) }
        } catch {
            $e = $_.Exception; while ($e.InnerException) { $e = $e.InnerException }
            Add-Row $plc 'prodiag' '' 'ProDiag_Supervisions.xlsx' '' '' 'error' (($e.Message -replace '\s+', ' ').Trim())
        }
    }

    # Тексты проекта (комментарии, тексты аварий и ProDiag, списки текстов): один файл на каждый язык проекта,
    # в файле справочный язык (обычно en-US) и этот язык рядом
    $ls = $proj.LanguageSettings
    $src = $ls.ReferenceLanguage.Culture
    foreach ($lang in $ls.ActiveLanguages) {
        $t = $lang.Culture
        if ($t.Name -eq $src.Name) { continue }
        $name = 'ProjectTexts_' + $t.Name + '.xlsx'
        Write-Log "Выгрузка текстов проекта $($src.Name) / $($t.Name)..."
        try {
            $proj.ExportProjectTexts([IO.FileInfo](Join-Path $root $name), $src, $t)
            Add-Row '' 'texts' '' $name '' $t.Name 'ok' ''
        } catch {
            $e = $_.Exception; while ($e.InnerException) { $e = $e.InnerException }
            Add-Row '' 'texts' '' $name '' $t.Name 'error' (($e.Message -replace '\s+', ' ').Trim())
        }
    }

    $manifestFile = Join-Path $root 'manifest.csv'
    $manifest | Export-Csv -LiteralPath $manifestFile -NoTypeInformation -Encoding UTF8 -Delimiter ';'
    $ok = @($manifest | Where-Object status -eq 'ok').Count
    $skip = @($manifest | Where-Object status -eq 'skipped').Count
    $err = @($manifest | Where-Object status -eq 'error').Count
    Write-Log "Готово: выгружено $ok, пропущено $skip (know-how, F-блоки), ошибок $err"
    Write-Log "Папка: $root"
    Write-Log "Отчёт: $manifestFile"
    if ($err -gt 0) {
        Write-Log 'Блоки с ошибкой (первые 10):'
        $manifest | Where-Object status -eq 'error' | Select-Object -First 10 |
            ForEach-Object { Write-Log ('  ' + $_.plc + ' / ' + $_.name + ': ' + $_.note) }
        if (-not $Compile) { Write-Log 'Если ошибка про несогласованность блока: запустить ещё раз с -Compile' }
    }
} catch {
    Write-Log ('ОШИБКА: ' + $_.Exception.Message)
    $exitCode = 1
} finally {
    if ($proj) { try { $proj.Close() } catch {} }
    if ($tia) { try { $tia.Dispose() } catch {} }
}

if (-not $NoPause) { Read-Host 'Нажмите Enter для выхода' | Out-Null }
exit $exitCode
