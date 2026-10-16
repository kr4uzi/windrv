<#
.SYNOPSIS
    Compares the devices windrvscan.py finds in INF files with Microsoft InfVerif ("infverif /info").

.DESCRIPTION
    A device is "ID|kind|architecture|minimum OS|install section". Only INF files with deviations
    are printed, followed by the counts. Ids windrvscan drops because of an ExcludeID directive are
    not deviations (InfVerif lists them, Windows does not install the section for them).
    infverif.exe next to this script is used, otherwise the one of the latest WDK NuGet package is
    downloaded there (-InfVerif to use another one).

.EXAMPLE
    .\infverif-compare.ps1                                    # driver store of this Windows
    .\infverif-compare.ps1 -Path C:\drivers\intel             # all INF files below a directory
    .\infverif-compare.ps1 -Path C:\drivers\intel\e1d.inf
#>
param(
    [string] $Path,
    [string] $InfVerif,
    [string] $Python = "python"
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

if (-not $Path) {
    $Path = Join-Path $env:SystemRoot "System32\DriverStore\FileRepository"
}
if (Test-Path -LiteralPath $Path -PathType Leaf) {
    # FullName: Resolve-Path would prefix UNC paths with the PowerShell provider
    $infs = @((Get-Item -LiteralPath $Path).FullName)
} else {
    $infs = @(Get-ChildItem -LiteralPath $Path -Recurse -File -Filter *.inf | ForEach-Object { $_.FullName } | Sort-Object)
}
if ($infs.Count -eq 0) {
    throw "no INF files found in $Path"
}

if (-not $InfVerif) {
    $InfVerif = Join-Path $root "infverif.exe"
    if (-not (Test-Path -LiteralPath $InfVerif)) {
        # InfVerif is part of the WDK, published as NuGet package (a ZIP file, no installation needed):
        # https://www.nuget.org/packages/Microsoft.Windows.WDK.x64 - latest stable version, only infverif.exe is extracted
        $feed = "https://api.nuget.org/v3-flatcontainer/microsoft.windows.wdk.x64"
        $version = (Invoke-RestMethod "$feed/index.json").versions | Where-Object { $_ -notmatch "-" } |
            Sort-Object { [version] $_ } | Select-Object -Last 1
        $package = Join-Path ([IO.Path]::GetTempPath()) "microsoft.windows.wdk.x64.$version.nupkg"
        Write-Host "downloading infverif.exe of Microsoft.Windows.WDK.x64 $version (about 110 MB)"
        $progress = $ProgressPreference
        $ProgressPreference = "SilentlyContinue"  # Invoke-WebRequest of Windows PowerShell is very slow with a progress bar
        try {
            Invoke-WebRequest "$feed/$version/microsoft.windows.wdk.x64.$version.nupkg" -OutFile $package -UseBasicParsing
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            $zip = [IO.Compression.ZipFile]::OpenRead($package)
            try {
                $entry = $zip.Entries | Where-Object { $_.FullName -like "*/x64/infverif.exe" } | Select-Object -First 1
                if (-not $entry) { throw "no x64/infverif.exe in Microsoft.Windows.WDK.x64 $version" }
                [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $InfVerif)
            } finally {
                $zip.Dispose()
            }
        } finally {
            Remove-Item -LiteralPath $package -ErrorAction SilentlyContinue
            $ProgressPreference = $progress
        }
    }
}

# windrvscan: devices of the INF files, in batches (command line length limit of Windows)
$ours = @{}
for ($i = 0; $i -lt $infs.Count; $i += 100) {
    $batch = $infs[$i..([Math]::Min($i + 99, $infs.Count - 1))]
    $json = & $Python (Join-Path $root "windrvscan.py") --devices -- @batch | Out-String
    if ($LASTEXITCODE) { throw "windrvscan.py --devices failed" }
    (ConvertFrom-Json $json).PSObject.Properties | ForEach-Object { $ours[$_.Name] = $_.Value }
}

# InfVerif: one "Device:" block per models entry
function Get-InfVerifDevices([string] $inf) {
    $devices = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $text = (& $InfVerif /info $inf) -join "`n"
    foreach ($block in ($text -split "(?m)^(?=Device:)") | Where-Object { $_ -match '^Device:' }) {
        $fields = @{}
        $key = $null
        foreach ($line in $block -split "`n") {
            if ($line -match '^(\S[^:]*):\s*(.*?)\s*$') {
                $key = $Matches[1]
                $fields[$key] = @($Matches[2] | Where-Object { $_ })
            } elseif ($key -and $line -match '^\s+(\S.*?)\s*$') {
                # further values of a field, e.g. compatible ids
                $fields[$key] += $Matches[1]
            }
        }
        $suffix = "|$("$($fields['Architecture'])".ToLower())|$($fields['Minimum OS'])|$("$($fields['Section Name'])".ToLower())"
        foreach ($id in $fields['Hardware ID']) { [void]$devices.Add("$($id.ToUpper())|hardware$suffix") }
        foreach ($id in $fields['Compatible IDs']) { [void]$devices.Add("$($id.ToUpper())|compatible$suffix") }
    }
    return ,$devices
}

$different = 0
$count = 0
foreach ($inf in $infs) {
    $count++
    Write-Progress -Activity "Comparing with InfVerif" -Status $inf -PercentComplete (100 * $count / $infs.Count)
    $entry = $ours[$inf]
    if (-not $entry -or $entry.error) {
        Write-Host "$inf`n  windrvscan error: $($entry.error)"
        $different++
        continue
    }

    $theirs = Get-InfVerifDevices $inf
    $onlyOurs = @($entry.devices | Where-Object { -not $theirs.Contains($_) })
    $onlyTheirs = @($theirs | Where-Object { $entry.devices -notcontains $_ -and $entry.excluded -notcontains $_ })
    if ($onlyOurs.Count -or $onlyTheirs.Count) {
        Write-Host $inf
        $onlyOurs | ForEach-Object { Write-Host "  + only windrvscan: $_" }
        $onlyTheirs | ForEach-Object { Write-Host "  - only InfVerif:   $_" }
        $different++
    }
}
Write-Progress -Activity "Comparing with InfVerif" -Completed

Write-Host "$($infs.Count) INF files: $($infs.Count - $different) equal, $different different"
exit [int]($different -gt 0)
