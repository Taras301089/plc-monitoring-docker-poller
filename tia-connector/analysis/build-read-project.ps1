param(
    [string]$ApiDirectory = 'C:\Program Files\Siemens\Automation\Portal V17\PublicAPI\V17'
)
$ErrorActionPreference = 'Stop'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$api = Join-Path $ApiDirectory 'Siemens.Engineering.dll'
if (!(Test-Path -LiteralPath $compiler) -or !(Test-Path -LiteralPath $api)) {
    throw 'Requires installed .NET Framework x64 compiler and TIA Openness V17 API.'
}
$outputDirectory = Join-Path $PSScriptRoot 'bin'
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
$output = Join-Path $outputDirectory 'ChanganRead.exe'
& $compiler /nologo /target:exe /platform:x64 "/reference:$api" /reference:System.Web.Extensions.dll "/out:$output" (Join-Path $PSScriptRoot 'ReadProject.cs')
if ($LASTEXITCODE -ne 0) { throw 'Read-only analysis helper compilation failed.' }
Write-Output $output
