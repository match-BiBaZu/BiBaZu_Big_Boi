param(
    [string]$HbmApiRoot = 'C:\Users\Public\Documents\HBM\HBM Common API\API'
)

# Build only. No device connection, device configuration or firmware changes.
$ErrorActionPreference = 'Stop'
$project = Join-Path $PSScriptRoot 'src\QuantumXMonitor\QuantumXMonitor.csproj'
foreach ($name in @('Hbm.Api.Common.dll', 'Hbm.Api.QuantumX.dll', 'Newtonsoft.Json.dll', 'DeviceDriver.plugins')) {
    if (-not (Test-Path -LiteralPath (Join-Path $HbmApiRoot $name) -PathType Leaf)) {
        throw "HBM Common API fehlt oder ist unvollstaendig: $HbmApiRoot\$name. HBM Common API installieren oder den vorhandenen API-Ordner mit -HbmApiRoot angeben. Alternativ den vollstaendigen QuantumXMonitor-Programmordner vom alten Mess-PC uebernehmen."
    }
}
if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
    throw '.NET SDK fehlt. Zum Bauen das .NET SDK und das .NET Framework 4.8 Developer Pack installieren.'
}
$sdkList = @(& dotnet --list-sdks)
if ($LASTEXITCODE -ne 0 -or $sdkList.Count -eq 0) {
    throw '.NET SDK fehlt (eine installierte Runtime reicht zum Bauen nicht). .NET SDK und .NET Framework 4.8 Developer Pack installieren.'
}
& dotnet build $project --configuration Release "-p:HbmApiRoot=$HbmApiRoot"
if ($LASTEXITCODE -ne 0) { throw 'QuantumXMonitor-Build fehlgeschlagen. Buildausgabe pruefen; .NET Framework 4.8 Developer Pack wird benoetigt.' }
$executable = Join-Path $PSScriptRoot 'src\QuantumXMonitor\bin\x86\Release\net48\QuantumXMonitor.exe'
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) { throw 'Build beendet, QuantumXMonitor.exe aber nicht gefunden.' }
Write-Output "Bridge gebaut: $executable"
Write-Output 'Die Conveyor-Setup-GUI erkennt diesen Pfad automatisch. Zum Uebertragen auf einen anderen PC den gesamten net48-Ordner kopieren.'
