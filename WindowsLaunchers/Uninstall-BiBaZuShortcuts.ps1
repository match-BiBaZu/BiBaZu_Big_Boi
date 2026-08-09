[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$shortcutNames = @(
    'BiBaZu Reorientation Control.lnk',
    'BiBaZu Pressure Control.lnk',
    'BiBaZu Conveyor Setup.lnk',
    'BiBaZu Automated Image Capture.lnk'
)
$locations = @(
    [Environment]::GetFolderPath('Desktop'),
    (Join-Path ([Environment]::GetFolderPath('Programs')) 'BiBaZu')
)

foreach ($location in $locations) {
    foreach ($name in $shortcutNames) {
        $shortcut = Join-Path $location $name
        if (Test-Path -LiteralPath $shortcut -PathType Leaf) {
            Remove-Item -LiteralPath $shortcut -Force
            Write-Host "Entfernt: $shortcut"
        }
    }
}

$startMenuDirectory = $locations[1]
if ((Test-Path -LiteralPath $startMenuDirectory) -and
    -not (Get-ChildItem -LiteralPath $startMenuDirectory -Force)) {
    Remove-Item -LiteralPath $startMenuDirectory
}

Write-Host 'BiBaZu-Verknüpfungen wurden entfernt.' -ForegroundColor Green
