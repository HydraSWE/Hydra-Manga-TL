param(
    [switch]$SkipInpaintRuntime,
    [switch]$RecreateInpaintRuntime,
    [switch]$UseSystemSitePackagesForInpaint
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$MainPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$SitePackages = Join-Path $RepoRoot ".venv\Lib\site-packages"
$PySideRoot = Join-Path $SitePackages "PySide6"
$ShibokenRoot = Join-Path $SitePackages "shiboken6"
$BuildSupport = Join-Path $RepoRoot "scripts\build_support"

if (-not (Test-Path $MainPython)) {
    throw "Main app venv is missing. Run: py -3.12 -m venv .venv; .\.venv\Scripts\python -m pip install -r requirements.txt"
}

foreach ($Path in @($PySideRoot, $ShibokenRoot, $BuildSupport)) {
    if (-not (Test-Path $Path)) {
        throw "Build dependency path is missing: $Path"
    }
}

function Ensure-QtReportedPath {
    param(
        [Parameter(Mandatory = $true)][string]$Name
    )
    $Source = Join-Path $PySideRoot $Name
    $Destination = Join-Path $SitePackages $Name
    if (-not (Test-Path $Source) -or (Test-Path $Destination)) {
        return
    }
    try {
        New-Item -ItemType Junction -Path $Destination -Target $Source | Out-Null
    } catch {
        Copy-Item -Path $Source -Destination $Destination -Recurse -Force
    }
}

foreach ($QtPath in @("plugins", "qml", "translations")) {
    Ensure-QtReportedPath -Name $QtPath
}

$env:HYDRA_BUILD_DLL_DIRS = "$PySideRoot;$ShibokenRoot"
$env:PYTHONPATH = "$BuildSupport" + $(if ($env:PYTHONPATH) { ";$env:PYTHONPATH" } else { "" })
$env:PATH = "$PySideRoot;$ShibokenRoot;$env:PATH"

if (-not $SkipInpaintRuntime) {
    $runtimeArgs = @()
    if ($RecreateInpaintRuntime) {
        $runtimeArgs += "-Recreate"
    }
    if ($UseSystemSitePackagesForInpaint) {
        $runtimeArgs += "-UseSystemSitePackages"
    }
    $runtimeArgs += "-BuildHelperExe"
    & (Join-Path $PSScriptRoot "build_inpaint_runtime.ps1") @runtimeArgs
}

& $MainPython -m PyInstaller (Join-Path $RepoRoot "HydraMangaTL.spec") --noconfirm

$Dist = Join-Path $RepoRoot "dist\Hydra Manga TL"
if (-not (Test-Path $Dist)) {
    throw "PyInstaller did not produce expected output: $Dist"
}

Write-Host "Windows dist ready: $Dist"
