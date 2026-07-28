param(
    [switch]$Recreate,
    [switch]$UseSystemSitePackages,
    [switch]$BuildHelperExe
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$RuntimeRoot = Join-Path $RepoRoot "runtime\inpaint"
$VenvRoot = Join-Path $RepoRoot ".venv-inpaint"
$Requirements = Join-Path $RepoRoot "requirements-inpaint.txt"
$BuildRoot = Join-Path $RepoRoot "build"
$PipTemp = Join-Path $BuildRoot "pip-temp-inpaint"
$PipCache = Join-Path $BuildRoot "pip-cache-inpaint"
New-Item -ItemType Directory -Force -Path $PipTemp, $PipCache | Out-Null
$env:TEMP = $PipTemp
$env:TMP = $PipTemp
$env:PIP_CACHE_DIR = $PipCache

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Command,
        [Parameter(Mandatory = $true)][string]$Description
    )
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

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
if ($Recreate -and (Test-Path $VenvRoot)) {
    $resolved = Resolve-Path $VenvRoot
    if (-not $resolved.Path.StartsWith($RepoRoot.Path)) {
        throw "Refusing to remove unexpected path: $resolved"
    }
    Remove-Item -LiteralPath $VenvRoot -Recurse -Force
}

if (-not (Test-Path (Join-Path $VenvRoot "Scripts\python.exe"))) {
    $venvArgs = @("-m", "venv")
    if ($UseSystemSitePackages) {
        $venvArgs += "--system-site-packages"
    }
    $venvArgs += $VenvRoot
    Invoke-Checked { py -3.12 @venvArgs } "Create inpaint .venv-inpaint"
}

$Python = Join-Path $VenvRoot "Scripts\python.exe"
Invoke-Checked { & $Python -m pip install --no-cache-dir --upgrade pip } "Upgrade inpaint pip"
Invoke-Checked { & $Python -m pip install --no-cache-dir -r $Requirements } "Install inpaint requirements"
Invoke-Checked { & $Python -c "import iopaint, PIL, huggingface_hub, torch, torchvision; assert PIL.__version__.startswith('9.5.'), f'inpaint runtime must keep Pillow 9.5.x, got {PIL.__version__}'; print('inpaint runtime ready', PIL.__version__, huggingface_hub.__version__, torch.__version__, torchvision.__version__)" } "Validate inpaint runtime imports"

if ($BuildHelperExe) {
    Invoke-Checked { & $Python -m pip install --no-cache-dir "pyinstaller>=6.11,<7" } "Install inpaint PyInstaller"
    $HelperSource = Join-Path $RepoRoot "scripts\inpaint_helper.py"
    $HelperBuild = Join-Path $RepoRoot "build\inpaint-helper"
    $HelperSpec = Join-Path $HelperBuild "spec"
    New-Item -ItemType Directory -Force -Path $HelperBuild | Out-Null
    New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
    Invoke-Checked { & $Python -m PyInstaller $HelperSource --name hydra-inpaint --onefile --noconfirm --distpath $RuntimeRoot --workpath $HelperBuild --specpath $HelperSpec } "Build hydra-inpaint.exe"
    if (-not (Test-Path (Join-Path $RuntimeRoot "hydra-inpaint.exe"))) {
        throw "Failed to build hydra-inpaint.exe"
    }
}

Write-Host "Inpaint build venv ready: $VenvRoot"
Write-Host "Inpaint runtime ready: $RuntimeRoot"
