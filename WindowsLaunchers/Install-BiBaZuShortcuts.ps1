[CmdletBinding()]
param(
    [switch]$DesktopOnly,
    [switch]$StartMenuOnly
)

$ErrorActionPreference = 'Stop'
$launcherDirectory = Split-Path -Parent $PSCommandPath
$bibazuRepository = Split-Path -Parent $launcherDirectory
$workspace = Split-Path -Parent $bibazuRepository
$iconDirectory = Join-Path $launcherDirectory 'icons'

$desktopDirectory = [Environment]::GetFolderPath('Desktop')
$startMenuDirectory = Join-Path `
    ([Environment]::GetFolderPath('Programs')) `
    'BiBaZu'

$installDesktop = -not $StartMenuOnly
$installStartMenu = -not $DesktopOnly

$applications = @(
    [pscustomobject]@{
        Name = 'BiBaZu Reorientation Control'
        Description = 'Bauteilpose erkennen und Reorientierungszyklus ausführen'
        Executable = Join-Path $bibazuRepository 'ReorientationControlGUI\.venv\Scripts\pythonw.exe'
        Arguments = '-m bibazu_reorientation'
        WorkingDirectory = Join-Path $bibazuRepository 'ReorientationControlGUI'
        Icon = (Join-Path $iconDirectory 'reorientation.ico') + ',0'
    },
    [pscustomobject]@{
        Name = 'BiBaZu Pressure Control'
        Description = 'Düsenarrays und Pressure-Profile einrichten'
        Executable = Join-Path $bibazuRepository '.venv\Scripts\pythonw.exe'
        Arguments = '"' + (Join-Path $bibazuRepository 'CSVSaver\PressureControlGUI.py') + '"'
        WorkingDirectory = Join-Path $bibazuRepository 'CSVSaver'
        Icon = (Join-Path $iconDirectory 'pressure.ico') + ',0'
    },
    [pscustomobject]@{
        Name = 'BiBaZu Conveyor Setup'
        Description = 'Förderband und Lichtschranken einrichten'
        Executable = Join-Path $bibazuRepository '.venv\Scripts\pythonw.exe'
        Arguments = '"' + (Join-Path $bibazuRepository 'CSVSaver\ConveyorSetupGUI.py') + '"'
        WorkingDirectory = Join-Path $bibazuRepository 'CSVSaver'
        Icon = (Join-Path $iconDirectory 'conveyor.ico') + ',0'
    },
    [pscustomobject]@{
        Name = 'BiBaZu Automated Image Capture'
        Description = 'Baumer-Aufnahmen und YOLO-Datenerfassung'
        Executable = Join-Path $workspace 'automated_image_capture\.venv\Scripts\pythonw.exe'
        Arguments = '-m automated_image_capture.app'
        WorkingDirectory = Join-Path $workspace 'automated_image_capture'
        Icon = (Join-Path $iconDirectory 'capture.ico') + ',0'
    }
)

function Assert-ApplicationReady {
    param([pscustomobject]$Application)

    if (-not (Test-Path -LiteralPath $Application.Executable -PathType Leaf)) {
        throw "Python-Umgebung fehlt für '$($Application.Name)': $($Application.Executable)"
    }
    if (-not (Test-Path -LiteralPath $Application.WorkingDirectory -PathType Container)) {
        throw "Projektordner fehlt für '$($Application.Name)': $($Application.WorkingDirectory)"
    }
    $iconPath = ([string]$Application.Icon).Split(',')[0]
    if (-not (Test-Path -LiteralPath $iconPath -PathType Leaf)) {
        throw "Programmsymbol fehlt für '$($Application.Name)': $iconPath"
    }
}

function New-ApplicationShortcut {
    param(
        [pscustomobject]$Application,
        [string]$Destination
    )

    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    $shortcutPath = Join-Path $Destination ($Application.Name + '.lnk')
    $targetPath = [string]$Application.Executable
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $targetPath
    $shortcut.Arguments = [string]$Application.Arguments
    $shortcut.WorkingDirectory = [string]$Application.WorkingDirectory
    $shortcut.Description = [string]$Application.Description
    $shortcut.IconLocation = [string]$Application.Icon
    $shortcut.WindowStyle = 1
    $shortcut.Save()

    $savedShortcut = $shell.CreateShortcut($shortcutPath)
    if ($savedShortcut.TargetPath -ne $targetPath) {
        throw "Verknüpfungsziel konnte nicht gespeichert werden: $shortcutPath"
    }
    if ($savedShortcut.IconLocation -ne [string]$Application.Icon) {
        throw "Programmsymbol konnte nicht gespeichert werden: $shortcutPath"
    }
    Write-Host "Erstellt: $shortcutPath"
}

foreach ($application in $applications) {
    Assert-ApplicationReady -Application $application
    if ($installDesktop) {
        New-ApplicationShortcut -Application $application -Destination $desktopDirectory
    }
    if ($installStartMenu) {
        New-ApplicationShortcut -Application $application -Destination $startMenuDirectory
    }
}

Write-Host ''
Write-Host 'BiBaZu-Verknüpfungen wurden erfolgreich installiert.' -ForegroundColor Green
Write-Host 'Windows-Suche: BiBaZu'
