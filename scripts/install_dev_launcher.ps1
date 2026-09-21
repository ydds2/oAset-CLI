# Point the global `oaset` command at THIS checkout on every run.
# Frozen GitHub Release exe is kept as oaset.release.exe so it cannot shadow us.
param(
  [string]$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
  [string]$Target = "$env:USERPROFILE\.local\bin"
)

$ErrorActionPreference = "Stop"
$srcExe = Join-Path $Target "oaset.exe"
$bakExe = Join-Path $Target "oaset.release.exe"
$runner = Join-Path $Repo "scripts\run_oaset.py"
$venvPy = Join-Path $Repo ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPy)) {
  Write-Error "Missing $venvPy — create the venv and: .venv\Scripts\pip install -e ."
  exit 1
}
if (-not (Test-Path $runner)) {
  Write-Error "Missing $runner"
  exit 1
}

if (-not (Test-Path $Target)) {
  New-Item -ItemType Directory -Path $Target | Out-Null
}

if (Test-Path $srcExe) {
  $size = (Get-Item $srcExe).Length
  if ($size -gt 1000000) {
    if (Test-Path $bakExe) { Remove-Item $srcExe -Force }
    else { Move-Item $srcExe $bakExe -Force; Write-Output "backed up frozen exe -> $bakExe" }
  } else {
    Remove-Item $srcExe -Force
  }
}

$cmd = @"
@echo off
"$venvPy" "$runner" %*
"@
Set-Content -Path (Join-Path $Target "oaset.cmd") -Value $cmd -Encoding ASCII

$posixVenv = ($venvPy -replace '\\','/')
$posixRunner = ($runner -replace '\\','/')
$sh = @"
#!/bin/sh
exec "$posixVenv" "$posixRunner" "`$@"
"@
Set-Content -Path (Join-Path $Target "oaset") -Value $sh -Encoding ASCII

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$Target*") {
  [Environment]::SetEnvironmentVariable("Path", "$Target;$userPath", "User")
  Write-Output "added $Target to the user PATH"
}

Write-Output "global oaset -> $runner"
Write-Output "open a new terminal: oaset --version"
