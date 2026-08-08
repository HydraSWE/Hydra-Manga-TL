<#
.SYNOPSIS
    Generates a manifest.json for the Hydra Manga TL online installer.

.DESCRIPTION
    Given the path to a full offline setup EXE, computes its SHA-256 hash
    and file size, then writes a manifest.json suitable for the online
    bootstrap installer to consume.

.PARAMETER SetupExePath
    Absolute path to the full offline setup EXE (e.g. "Hydra Manga TL V1.0.0 Setup.exe").

.PARAMETER Version
    Semantic version string to embed in the manifest (e.g. "1.0.0").

.PARAMETER BaseUrl
    Base HTTPS URL where the setup EXE will be hosted.
    The file name is URL-encoded and appended automatically.

.PARAMETER OutputPath
    Optional. Path for the output manifest.json.
    Defaults to .\manifest.json in the current directory.

.EXAMPLE
    .\generate_manifest.ps1 `
        -SetupExePath "D:\Tools\Hydra_EXE\Hydra Manga TL V1.0.0 Setup.exe" `
        -Version "1.0.0" `
        -BaseUrl "https://storage.example.com/releases"
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$SetupExePath,

    [Parameter(Mandatory = $true)]
    [string]$Version,

    [Parameter(Mandatory = $true)]
    [string]$BaseUrl,

    [string]$OutputPath = ".\manifest.json"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $SetupExePath)) {
    throw "Setup EXE not found: $SetupExePath"
}

$file = Get-Item $SetupExePath
$fileName = $file.Name
$sizeBytes = $file.Length

Write-Host "Computing SHA-256 for $fileName ($([math]::Round($sizeBytes / 1GB, 2)) GB)..."
$hash = (Get-FileHash -Path $SetupExePath -Algorithm SHA256).Hash.ToLower()
Write-Host "SHA-256: $hash"

# URL-encode the file name and build the full download URL.
$encodedName = [Uri]::EscapeDataString($fileName)
$fullUrl = "$($BaseUrl.TrimEnd('/'))/$encodedName"

$manifest = @{
    version   = $Version
    fileName  = $fileName
    url       = $fullUrl
    sha256    = $hash
    sizeBytes = $sizeBytes
}

$json = $manifest | ConvertTo-Json -Depth 1
Set-Content -Path $OutputPath -Value $json -Encoding UTF8
Write-Host "Manifest written to: $(Resolve-Path $OutputPath)"
Write-Host ""
Write-Host $json
