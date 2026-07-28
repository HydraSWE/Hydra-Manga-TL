param(
    [switch]$SkipInpaintRuntime,
    [switch]$RecreateInpaintRuntime,
    [switch]$UseSystemSitePackagesForInpaint
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$MainPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$MainRequirements = Join-Path $RepoRoot "requirements.txt"
$SitePackages = Join-Path $RepoRoot ".venv\Lib\site-packages"
$PySideRoot = Join-Path $SitePackages "PySide6"
$ShibokenRoot = Join-Path $SitePackages "shiboken6"
$BuildSupport = Join-Path $RepoRoot "scripts\build_support"
$BuildRoot = Join-Path $RepoRoot "build"
$PipTemp = Join-Path $BuildRoot "pip-temp"
$PipCache = Join-Path $BuildRoot "pip-cache"
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

if (-not (Test-Path $MainPython)) {
    Invoke-Checked { py -3.12 -m venv (Join-Path $RepoRoot ".venv") } "Create main .venv"
}
if (-not (Test-Path $MainRequirements)) {
    throw "Missing requirements.txt"
}

Invoke-Checked { & $MainPython -m pip install --no-cache-dir --upgrade pip } "Upgrade main pip"
Invoke-Checked { & $MainPython -m pip install --no-cache-dir -r $MainRequirements } "Install main requirements"
& $MainPython -m pip uninstall -y iopaint gradio torchvision

foreach ($Path in @($PySideRoot, $ShibokenRoot, $BuildSupport)) {
    if (-not (Test-Path $Path)) {
        throw "Build dependency path is missing: $Path"
    }
}

function Test-PythonImport {
    param(
        [Parameter(Mandatory = $true)][string[]]$Imports
    )
    $missing = @()
    foreach ($ImportName in $Imports) {
        $code = @"
import importlib
name = "$ImportName"
try:
    importlib.import_module(name)
except Exception as exc:
    raise SystemExit(f"{name}: {type(exc).__name__}: {exc}")
"@
        $output = & $MainPython -c $code 2>&1
        if ($LASTEXITCODE -ne 0) {
            $missing += ($output -join "`n")
        }
    }
    if ($missing.Count -gt 0) {
        throw "Build import preflight failed:`n$($missing -join "`n")"
    }
    Write-Host "Build import preflight OK"
}

function Set-QtReportedPath {
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
    Set-QtReportedPath -Name $QtPath
}

$env:HYDRA_BUILD_DLL_DIRS = "$PySideRoot;$ShibokenRoot"
$env:PYTHONPATH = "$BuildSupport" + $(if ($env:PYTHONPATH) { ";$env:PYTHONPATH" } else { "" })
$env:PATH = "$PySideRoot;$ShibokenRoot;$env:PATH"

Get-ChildItem -Path (Join-Path $SitePackages "nvidia") -Directory -ErrorAction SilentlyContinue | ForEach-Object {
    $BinPath = Join-Path $_.FullName "bin"
    if (Test-Path $BinPath) {
        $env:PATH = "$BinPath;$env:PATH"
    }
}

Test-PythonImport -Imports @(
    "PySide6",
    "PySide6.QtTextToSpeech",
    "paddle",
    "paddleocr",
    "paddlex",
    "cv2",
    "PIL",
    "torch",
    "transformers",
    "sentencepiece",
    "sacremoses",
    "sudachipy",
    "sudachidict_small",
    "keyring",
    "llama_cpp",
    "nvidia"
)

if (-not $SkipInpaintRuntime) {
    $runtimeArgs = @()
    if ($RecreateInpaintRuntime) {
        $runtimeArgs += "-Recreate"
    }
    if ($UseSystemSitePackagesForInpaint) {
        $runtimeArgs += "-UseSystemSitePackages"
    }
    $runtimeArgs += "-BuildHelperExe"
    $InpaintScript = Join-Path $PSScriptRoot "build_inpaint_runtime.ps1"
    Invoke-Checked { & $InpaintScript @runtimeArgs } "Build inpaint runtime"
}

Invoke-Checked { & $MainPython -m PyInstaller (Join-Path $RepoRoot "HydraMangaTL.spec") --noconfirm } "PyInstaller build"

$Dist = Join-Path $RepoRoot "dist\Hydra Manga TL"
if (-not (Test-Path $Dist)) {
    throw "PyInstaller did not produce expected output: $Dist"
}

$Internal = Join-Path $Dist "_internal"
$RequiredBundlePaths = @(
    "PySide6",
    "PySide6\plugins\platforms",
    "PySide6\plugins\imageformats",
    "PySide6\plugins\texttospeech",
    "paddle",
    "paddleocr",
    "paddlex",
    "cv2",
    "PIL",
    "torch",
    "transformers",
    "sentencepiece",
    "sacremoses",
    "sudachipy",
    "sudachidict_small",
    "keyring",
    "llama_cpp",
    "nvidia",
    "assets"
)
foreach ($RelativePath in $RequiredBundlePaths) {
    $Candidate = Join-Path $Internal $RelativePath
    if (-not (Test-Path $Candidate)) {
        throw "Frozen bundle is missing required runtime path: $Candidate"
    }
}

$RequiredMetadata = @(
    "paddlepaddle-*.dist-info",
    "paddleocr-*.dist-info",
    "paddlex-*.dist-info",
    "opencv_contrib_python-*.dist-info",
    "torch-*.dist-info",
    "transformers-*.dist-info",
    "sentencepiece-*.dist-info",
    "sacremoses-*.dist-info",
    "SudachiPy-*.dist-info",
    "sudachidict_small-*.dist-info",
    "keyring-*.dist-info",
    "llama_cpp_python-*.dist-info",
    "nvidia_cuda_runtime_cu12-*.dist-info",
    "nvidia_cublas_cu12-*.dist-info"
)
foreach ($Pattern in $RequiredMetadata) {
    if (-not (Get-ChildItem -Path $Internal -Filter $Pattern -Directory -ErrorAction SilentlyContinue)) {
        throw "Frozen bundle is missing required package metadata: $Pattern"
    }
}

Write-Host "Windows dist ready: $Dist"
