# PowerShell entry point. Runs deployment/sh/dev.sh through Git Bash or WSL.
# All logic lives in dev.sh so the two cannot drift.
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path

# Git Bash first: it uses the Windows docker.exe and the repo path as-is, with
# no /mnt/c translation.
$gitBash = @(
    "$env:ProgramFiles\Git\bin\bash.exe",
    "${env:ProgramFiles(x86)}\Git\bin\bash.exe",
    "$env:LOCALAPPDATA\Programs\Git\bin\bash.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

Push-Location $root
try {
    if ($gitBash) {
        & $gitBash 'deployment/sh/dev.sh' @args
        exit $LASTEXITCODE
    }
    if (Get-Command wsl -ErrorAction SilentlyContinue) {
        Write-Host 'Git Bash not found - using WSL.' -ForegroundColor DarkGray
        wsl bash 'deployment/sh/dev.sh' @args
        exit $LASTEXITCODE
    }
}
finally { Pop-Location }

Write-Host ''
Write-Host 'Neither Git Bash nor WSL is available.' -ForegroundColor Red
Write-Host '  Git for Windows:  https://git-scm.com/download/win'
Write-Host '  or enable WSL2:   wsl --install'
Write-Host ''
Write-Host 'Docker Desktop on Windows already requires WSL2, so you likely have it.'
exit 1
