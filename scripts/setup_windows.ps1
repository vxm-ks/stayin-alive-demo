[CmdletBinding()]
param(
    [string]$Python = "python",
    [switch]$SkipWslCheck
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

& $Python -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11+ is required'; print(sys.version)"
& $Python -m pip install --upgrade pip
& $Python -m pip install -r (Join-Path $Root "requirements.txt")

if (-not (Test-Path (Join-Path $Root ".env"))) {
    Copy-Item -LiteralPath (Join-Path $Root ".env.example") -Destination (Join-Path $Root ".env")
    Write-Host "Created .env from .env.example; add your local API key before online Stage 1 use."
}

if (-not $SkipWslCheck) {
    & wsl.exe --list --verbose
}

Write-Host "Windows dependencies installed. Run .\scripts\test_offline.ps1 next."
