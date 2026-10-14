param([Parameter(Mandatory=$true)][string]$Path, [int]$Network = -1)
$ErrorActionPreference = 'Stop'
[xml]$document = Get-Content -Raw -Encoding UTF8 -LiteralPath $Path
function Format-Access($node) {
    if ($node.Symbol) {
        $items = foreach ($component in $node.Symbol.Component) {
            $part = $component.Name
            if ($component.AccessModifier -eq 'Array') {
                $indices = foreach ($access in $component.Access) { Format-Access $access }
                $part += '[' + ($indices -join ',') + ']'
            }
            $part
        }
        $prefix = if ($node.Scope -eq 'LocalVariable') { '#' } else { '' }
        return $prefix + ($items -join '.')
    }
    if ($node.Constant) { return [string]$node.Constant.ConstantValue }
    return $node.InnerText
}
$index = 0
foreach ($unit in $document.SelectNodes('//*[local-name()="SW.Blocks.CompileUnit"]')) {
    $index++
    if ($Network -ge 0 -and $index -ne $Network) { continue }
    $texts = @($unit.SelectNodes('.//*[local-name()="MultilingualTextItem"]/*[local-name()="AttributeList"]/*[local-name()="Text"]') | ForEach-Object { $_.InnerText })
    $net = $unit.SelectSingleNode('.//*[local-name()="FlgNet"]')
    $scl = $unit.SelectSingleNode('.//*[local-name()="StructuredText"]')
    [ordered]@{
        network = $index
        id = $unit.ID
        texts = $texts
        accesses = @(if ($net) { foreach ($a in $net.Parts.Access) {
            [ordered]@{id=$a.UId; scope=$a.Scope; symbol=(Format-Access $a)}
        } })
        parts = @(if ($net) { foreach ($part in $net.Parts.Part) {
            [ordered]@{id=$part.UId; type=$part.Name; negated=@($part.Negated | ForEach-Object {$_.Name}); xml=$part.OuterXml}
        } })
        calls = @(if ($net) {foreach ($call in $net.Parts.Call) { $call.OuterXml }})
        wires = @(if ($net) {foreach ($wire in $net.Wires.Wire) { $wire.OuterXml }})
        scl = $(if ($scl) { $scl.OuterXml } else { $null })
    } | ConvertTo-Json -Depth 20 -Compress
}
