param(
    [switch]$SkipInpaintRuntime,
    [switch]$RecreateInpaintRuntime,
    [switch]$UseSystemSitePackagesForInpaint
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$MainPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $MainPython)) {
    throw "Main app venv is missing. Run: py -3.12 -m venv .venv; .\.venv\Scripts\python -m pip install -r requirements.txt"
}

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
