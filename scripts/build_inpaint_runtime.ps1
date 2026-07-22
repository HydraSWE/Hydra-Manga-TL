param(
    [switch]$Recreate,
    [switch]$UseSystemSitePackages,
    [switch]$BuildHelperExe
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$RuntimeRoot = Join-Path $RepoRoot "runtime\inpaint"
$Requirements = Join-Path $RepoRoot "requirements-inpaint.txt"

if (-not (Test-Path $Requirements)) {
    throw "Missing requirements-inpaint.txt"
}

if ($Recreate -and (Test-Path $RuntimeRoot)) {
    $resolved = Resolve-Path $RuntimeRoot
    if (-not $resolved.Path.StartsWith((Join-Path $RepoRoot "runtime"))) {
        throw "Refusing to remove unexpected path: $resolved"
    }
    Remove-Item -LiteralPath $RuntimeRoot -Recurse -Force
}

if (-not (Test-Path (Join-Path $RuntimeRoot "Scripts\python.exe"))) {
    New-Item -ItemType Directory -Force -Path (Split-Path $RuntimeRoot) | Out-Null
    $venvArgs = @("-m", "venv")
    if ($UseSystemSitePackages) {
        $venvArgs += "--system-site-packages"
    }
    $venvArgs += $RuntimeRoot
    py -3.12 @venvArgs
}

$Python = Join-Path $RuntimeRoot "Scripts\python.exe"
& $Python -m pip install --upgrade pip
& $Python -m pip install -r $Requirements
& $Python -c "import iopaint, PIL, huggingface_hub; print('inpaint runtime ready', PIL.__version__, huggingface_hub.__version__)"

if ($BuildHelperExe) {
    & $Python -m pip install "pyinstaller>=6.11,<7"
    $HelperSource = Join-Path $RepoRoot "scripts\inpaint_helper.py"
    $HelperBuild = Join-Path $RepoRoot "build\inpaint-helper"
    $HelperSpec = Join-Path $HelperBuild "spec"
    New-Item -ItemType Directory -Force -Path $HelperBuild | Out-Null
    & $Python -m PyInstaller $HelperSource --name hydra-inpaint --onefile --noconfirm --distpath $RuntimeRoot --workpath $HelperBuild --specpath $HelperSpec
    if (-not (Test-Path (Join-Path $RuntimeRoot "hydra-inpaint.exe"))) {
        throw "Failed to build hydra-inpaint.exe"
    }
}

Write-Host "Inpaint runtime ready: $RuntimeRoot"
