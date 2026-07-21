[CmdletBinding()]
param([int64]$MaximumBytes = 10MB)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$forbiddenExtensions = @(
    ".wav", ".flac", ".mp3", ".sf2", ".pt", ".pth", ".ckpt",
    ".safetensors", ".onnx", ".exe", ".dll", ".zip", ".7z", ".bin"
)
$forbiddenNames = @(".env", "auth.json", "credentials.json")
$violations = Get-ChildItem -Recurse -Force -File | Where-Object {
    $_.FullName -notlike "*\.git\*" -and (
        $forbiddenExtensions -contains $_.Extension.ToLowerInvariant() -or
        $forbiddenNames -contains $_.Name -or
        $_.Length -gt $MaximumBytes
    )
}

if ($violations) {
    $violations | Select-Object FullName, Length | Format-Table -AutoSize
    throw "Repository audit found forbidden or oversized files."
}

$patterns = @(
    'DEEPSEEK_API_KEY\s*=\s*["'']?sk-[A-Za-z0-9_-]{16,}',
    'gh[pousr]_[A-Za-z0-9]{30,}',
    "-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----",
    "D:\\2026_nus_summer_workshop",
    "/home/blue/"
)
$textFiles = Get-ChildItem -Recurse -Force -File -Include *.py,*.json,*.md,*.txt,*.ps1,*.sh,*.yml,*.yaml,*.toml |
    Where-Object { $_.FullName -notlike "*\.git\*" -and $_.FullName -ne $PSCommandPath }
$matches = foreach ($pattern in $patterns) {
    $textFiles | Select-String -Pattern $pattern
}
if ($matches) {
    $matches | Select-Object Path, LineNumber, Line | Format-Table -Wrap
    throw "Repository audit found secrets or machine-specific paths."
}

Write-Host "Repository audit passed."
