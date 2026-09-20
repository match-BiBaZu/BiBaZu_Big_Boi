[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Configuration = 'Release|TwinCAT RT (x64)'
$ExpectedTargetNetId = '10.145.4.14.1.1'

if ([Threading.Thread]::CurrentThread.GetApartmentState() -ne [Threading.ApartmentState]::STA) {
    throw 'XAE automation requires an STA shell. Run: pwsh -NoProfile -Sta -File .\Build-OpenTwinCATProject.ps1'
}

$solutionPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'TwinCAT Projekt3.sln')).Path
$systemProjectPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'TwinCAT Projekt3\TwinCAT Projekt3.tsproj')).Path
$pathComparer = [StringComparer]::OrdinalIgnoreCase

function Invoke-ComRetry {
    param([Parameter(Mandatory)][scriptblock]$Action)

    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        try {
            return & $Action
        }
        catch {
            $leaf = $_.Exception
            while ($null -ne $leaf.InnerException) {
                $leaf = $leaf.InnerException
            }
            if ($leaf.HResult -notin @(-2147418111, -2147417846) -or $attempt -eq 39) {
                throw
            }
            Start-Sleep -Milliseconds 250
        }
    }
}

if ($null -eq ('BiBaZu.Xae.RunningDteLocator' -as [type])) {
    Add-Type @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

namespace BiBaZu.Xae {
    public sealed class RunningDteEntry {
        public string DisplayName { get; set; }
        public object Instance { get; set; }
    }

    public static class RunningDteLocator {
        [DllImport("ole32.dll")]
        private static extern int GetRunningObjectTable(int reserved, out IRunningObjectTable table);

        [DllImport("ole32.dll")]
        private static extern int CreateBindCtx(int reserved, out IBindCtx context);

        public static RunningDteEntry[] GetAll() {
            IRunningObjectTable table = null;
            IBindCtx context = null;
            IEnumMoniker iterator = null;
            var result = new List<RunningDteEntry>();
            try {
                Marshal.ThrowExceptionForHR(GetRunningObjectTable(0, out table));
                Marshal.ThrowExceptionForHR(CreateBindCtx(0, out context));
                table.EnumRunning(out iterator);
                var monikers = new IMoniker[1];
                while (iterator.Next(1, monikers, IntPtr.Zero) == 0) {
                    var moniker = monikers[0];
                    try {
                        string displayName;
                        moniker.GetDisplayName(context, null, out displayName);
                        if (displayName.IndexOf("DTE", StringComparison.OrdinalIgnoreCase) < 0) continue;
                        object instance;
                        table.GetObject(moniker, out instance);
                        result.Add(new RunningDteEntry { DisplayName = displayName, Instance = instance });
                    }
                    catch {
                        // A ROT entry may disappear while it is being enumerated.
                    }
                    finally {
                        if (moniker != null) Marshal.ReleaseComObject(moniker);
                    }
                }
                return result.ToArray();
            }
            finally {
                if (iterator != null) Marshal.ReleaseComObject(iterator);
                if (context != null) Marshal.ReleaseComObject(context);
                if (table != null) Marshal.ReleaseComObject(table);
            }
        }
    }
}
'@
}

$matches = @()
$observed = @()
foreach ($entry in [BiBaZu.Xae.RunningDteLocator]::GetAll()) {
    try {
        $dte = $entry.Instance
        $solution = Invoke-ComRetry { ,($dte.Solution) }
        if ($null -eq $solution) {
            continue
        }
        $openPath = Invoke-ComRetry { [string]$solution.FullName }
        if ([string]::IsNullOrWhiteSpace($openPath)) {
            continue
        }
        $openPath = [IO.Path]::GetFullPath($openPath)
        $observed += $openPath
        if ($pathComparer.Equals($openPath, $solutionPath)) {
            $matches += [pscustomobject]@{
                Dte = $dte
                Solution = $solution
                Moniker = $entry.DisplayName
            }
        }
    }
    catch {
        $observed += ($entry.DisplayName + ' (nicht lesbar)')
    }
}

if ($matches.Count -eq 0) {
    $detail = if ($observed.Count) {
        ' Laufende Solutions: ' + (($observed | Sort-Object -Unique) -join '; ')
    }
    else {
        ' Keine laufende DTE-Instanz gefunden.'
    }
    throw "Die erwartete Solution ist nicht bereits in XAE geöffnet: $solutionPath.$detail"
}
if ($matches.Count -ne 1) {
    throw "Die erwartete Solution ist $($matches.Count)-mal geöffnet; eindeutige Auswahl nicht möglich."
}

$dte = $matches[0].Dte
$solution = $matches[0].Solution
$loadedProjects = @()
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    $loadedProjects = @()
    $projectCount = Invoke-ComRetry { [int]$solution.Projects.Count }
    for ($index = 1; $index -le $projectCount; $index++) {
        $project = Invoke-ComRetry { ,($solution.Projects.Item($index)) }
        $projectPath = Invoke-ComRetry { [string]$project.FullName }
        if (-not [string]::IsNullOrWhiteSpace($projectPath)) {
            $loadedProjects += [pscustomobject]@{
                Project = $project
                Path = [IO.Path]::GetFullPath($projectPath)
            }
        }
    }
    if ($projectCount -gt 0 -and $loadedProjects.Count -eq $projectCount) {
        break
    }
    Start-Sleep -Milliseconds 500
}

$systemProjects = @($loadedProjects | Where-Object { [IO.Path]::GetExtension($_.Path) -ieq '.tsproj' })
if ($systemProjects.Count -ne 1) {
    throw "Erwartet wurde genau ein geladenes TwinCAT-Systemprojekt, gefunden: $($systemProjects.Count)."
}
if (-not $pathComparer.Equals($systemProjects[0].Path, $systemProjectPath)) {
    throw "Unerwartetes Systemprojekt: $($systemProjects[0].Path)"
}

$systemProject = $systemProjects[0]
$systemManager = Invoke-ComRetry { ,($systemProject.Project.Object) }
$actualTarget = (Invoke-ComRetry { [string]$systemManager.GetTargetNetId() }).Trim()
if ($actualTarget -ne $ExpectedTargetNetId) {
    throw "Target-Prüfung fehlgeschlagen. Erwartet: $ExpectedTargetNetId; Projekt: $actualTarget. Es wurde weder gespeichert noch gebaut."
}

$build = Invoke-ComRetry { ,($solution.SolutionBuild) }
if ($null -eq $build) {
    throw 'Der XAE-Buildservice ist nicht verfügbar.'
}
if ((Invoke-ComRetry { [int]$build.BuildState }) -eq 2) {
    throw 'In dieser XAE-Instanz läuft bereits ein Build.'
}
try {
    $null = Invoke-ComRetry { ,($build.SolutionConfigurations.Item($Configuration)) }
}
catch {
    throw "Buildkonfiguration nicht gefunden: $Configuration"
}

$automationSettings = Invoke-ComRetry { ,($dte.GetObject('TcAutomationSettings')) }
if ($null -eq $automationSettings) {
    throw 'TcAutomationSettings ist nicht verfügbar; Build abgebrochen.'
}
$previousSilentMode = Invoke-ComRetry { [bool]$automationSettings.SilentMode }
$started = [DateTimeOffset]::Now
$watch = [Diagnostics.Stopwatch]::StartNew()
try {
    Invoke-ComRetry { $automationSettings.SilentMode = $true }
    Invoke-ComRetry { $dte.ExecuteCommand('File.SaveAll', '') }
    Invoke-ComRetry { $build.BuildProject($Configuration, $systemProject.Path, $true) }
    $lastBuildInfo = Invoke-ComRetry { [int]$build.LastBuildInfo }
    if ($lastBuildInfo -ne 0) {
        throw "TwinCAT-Build fehlgeschlagen; LastBuildInfo=$lastBuildInfo. Details stehen im XAE-Ausgabefenster."
    }
}
finally {
    $watch.Stop()
    try {
        Invoke-ComRetry { $automationSettings.SilentMode = $previousSilentMode }
    }
    catch {
        Write-Warning 'Der vorherige XAE-SilentMode konnte nicht wiederhergestellt werden.'
    }
}

[pscustomobject]@{
    Solution = $solutionPath
    SystemProject = $systemProjectPath
    TargetNetId = $actualTarget
    Configuration = $Configuration
    Started = $started
    BuildSeconds = [math]::Round($watch.Elapsed.TotalSeconds, 3)
    LastBuildInfo = $lastBuildInfo
    Activated = $false
    Downloaded = $false
}
