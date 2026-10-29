# Install psst on Windows:  irm https://raw.githubusercontent.com/levzb/psst/main/install.ps1 | iex
# $env:PSST_REPO = 'owner/repo' picks the GitHub repository, $env:PSST_FROM a local psst.exe to install.
$ErrorActionPreference = 'Stop'
if (-not $IsWindows -and $PSVersionTable.PSEdition -eq 'Core') { throw 'psst: on macOS and Linux use install.sh' }
$repo = if ($env:PSST_REPO) { $env:PSST_REPO } else { 'levzb/psst' }
$dir = Join-Path $env:LOCALAPPDATA 'Programs\psst'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$exe = Join-Path $dir 'psst.exe'
if ($env:PSST_FROM) {
  Copy-Item $env:PSST_FROM $exe -Force
} else {
  Invoke-WebRequest "https://github.com/$repo/releases/latest/download/psst-windows-x64.exe" -OutFile $exe -UseBasicParsing
}
Write-Host "psst: installed $exe"

# asks before it adds a marked block to your PowerShell profile
$shell = if ($PSVersionTable.PSEdition -eq 'Core') { 'pwsh' } else { 'powershell' }
& $exe --setup $shell
if ((Get-ExecutionPolicy) -in 'Restricted', 'AllSigned') {
  Write-Host 'psst: your execution policy does not load profiles; to allow it: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned'
}
Write-Host 'psst: next: psst --key  (saves your free OpenRouter key from https://openrouter.ai/keys; psst also asks for it on first use)'
