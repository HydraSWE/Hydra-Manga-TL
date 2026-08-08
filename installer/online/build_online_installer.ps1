<#
.SYNOPSIS
    Compiles the Hydra Manga TL online bootstrap installer using Inno Setup.

.DESCRIPTION
    Locates the Inno Setup compiler (ISCC.exe) and compiles
    HydraMangaTL_Online.iss into "Hydra Manga TL Online Setup.exe".

    The output is written to D:\Tools\Hydra_EXE\ (matching the offline
    installer's output directory).

.PARAMETER IsccPath
    Optional. Explicit path to ISCC.exe. If omitted, the script probes
    the registry and falls back to the known install location.
#>

param(
    [string]$IsccPath
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$IssFile = Join-Path $ScriptDir "HydraMangaTL_Online.iss"

if (-not (Test-Path $IssFile)) {
    throw "ISS script not found: $IssFile"
}

# --- Locate ISCC.exe ---------------------------------------------------
if (-not $IsccPath) {
    # Try registry first (both native and WOW64 views)
    $regPaths = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*'
    )
    foreach ($regPath in $regPaths) {
        $entry = Get-ItemProperty -Path $regPath -ErrorAction SilentlyContinue |
                 Where-Object { $_.DisplayName -like '*Inno Setup*' } |
                 Select-Object -First 1
        if ($entry -and $entry.InstallLocation) {
            $candidate = Join-Path $entry.InstallLocation "ISCC.exe"
            if (Test-Path $candidate) {
                $IsccPath = $candidate
                break
            }
        }
    }
}

# Fallback to the known location on this machine
if (-not $IsccPath -or -not (Test-Path $IsccPath)) {
    $IsccPath = "E:\SWE_tools\Inno Setup 7\ISCC.exe"
}

if (-not (Test-Path $IsccPath)) {
    throw "Inno Setup compiler (ISCC.exe) not found. Install Inno Setup 7 or pass -IsccPath."
}

Write-Host "Using ISCC: $IsccPath"
Write-Host "Compiling: $IssFile"
Write-Host ""

$ManifestFile = Join-Path $ScriptDir "manifest.json"
$AppVersion = "1.0.0"
if (Test-Path $ManifestFile) {
    try {
        $manifest = Get-Content $ManifestFile -Raw | ConvertFrom-Json
        if ($manifest.version) {
            $AppVersion = $manifest.version
        }
    } catch {
        Write-Warning "Failed to parse manifest.json, using default version $AppVersion"
    }
}

# --- Compile ------------------------------------------------------------
& $IsccPath "/DMyAppVersion=$AppVersion" $IssFile
if ($LASTEXITCODE -ne 0) {
    throw "ISCC compilation failed with exit code $LASTEXITCODE"
}

Write-Host ""
Write-Host "Online installer built successfully."
Write-Host "Output: D:\Tools\Hydra_EXE\Hydra Manga TL Online Setup.exe"
