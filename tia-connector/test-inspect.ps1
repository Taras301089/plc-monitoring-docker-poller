param([string]$XmlPath = (Join-Path $PSScriptRoot '..\Tia projects\GWM\UB\Partial export\UB020_20261014_074311.xml'))
$ErrorActionPreference = 'Stop'
$exe = Join-Path $PSScriptRoot 'bin\TiaConnector.exe'
$result = (& $exe inspect $XmlPath 30 17) | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or !$result.ok) { throw 'S30/T17 parsing failed.' }
if ($result.liveValuesAvailable -or $result.step.actions.Count -ne 0) { throw 'Incorrect offline/action state.' }
if (!$result.transition.expressionSupported) { throw 'T17 expression unsupported.' }
foreach ($fragment in @(' OR ', '"Arived"', 'NOT("Stn"."STN"[2]."Baton"[1])', 'NOT("Stn"."STN"[2]."Baton"[2])', 'NOT("Stn"."STN"[2]."Baton"[3])', 'NOT("Stn"."STN"[2]."zW"."Finish_Weld")', '"OBJ"[12]."SI_BWD"')) {
    if (!$result.transition.expression.Contains($fragment)) { throw "Missing condition: $fragment" }
}
foreach ($section in @($result.step.interlocks, $result.step.supervisions)) {
    if (($section | ConvertTo-Json -Depth 30 -Compress) -notmatch 'Powerrail') { throw 'Lost unconditional coil network.' }
}
$next = $result.transition.nextNodes | ConvertTo-Json -Depth 10 -Compress
if ($next -notmatch '"Number":"31"') { throw 'Expected next step S31.' }
$invalid = (& $exe inspect $XmlPath 30 1) | ConvertFrom-Json
if ($LASTEXITCODE -eq 0 -or $invalid.ok) { throw 'Unconnected transition accepted.' }
$unsupported = (& $exe inspect $XmlPath 4 1) | ConvertFrom-Json
# S4 leads via a branch, so direct selection must be rejected.
if ($LASTEXITCODE -eq 0 -or $unsupported.ok) { throw 'Indirect transition accepted as direct.' }
Write-Output 'PASS: S30/T17 conditions, inversions, empty actions, coil networks, next step and invalid selections.'
