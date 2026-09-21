# Install (or update) the global `oaset` from a CI-built artifact.
#
# This is the sanctioned way to have a global entry: the exe comes from the
# Release workflow (clean checkout + fingerprint), never from a local build.
# The artifact must verify its sha256, and it must report the same fingerprint
# that CI stamped - otherwise the install is refused.
#
#   powershell -NoProfile -File scripts/install_release.ps1
#
param(
  [string]$RunId = "",                  # specific release run; default = latest success
  [string]$Target = "$env:USERPROFILE\.local\bin"
)

$ErrorActionPreference = "Stop"

function Fail($msg) { Write-Error $msg; exit 1 }

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { Fail "gh CLI is required" }

if (-not $RunId) {
  $RunId = (gh run list --workflow=release.yml --status=success --limit=1 `
            --json databaseId -q '.[0].databaseId')
  if (-not $RunId) { Fail "no successful Release run found - trigger one with: gh workflow run release.yml" }
}
Write-Output "installing from Release run $RunId"

$stage = Join-Path ([System.IO.Path]::GetTempPath()) ("oaset-install-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $stage | Out-Null
try {
  gh run download $RunId -n oaset-cli-windows-x64 -D $stage
  $exe = Join-Path $stage "oaset.exe"
  $sum = Join-Path $stage "oaset.exe.sha256"
  if (-not (Test-Path $exe)) { Fail "artifact did not contain oaset.exe" }

  # 1. verify the checksum CI published
  if (Test-Path $sum) {
    $expected = (Get-Content $sum -Raw).Split()[0].ToLower()
    $actual = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLower()
    if ($expected -ne $actual) { Fail "sha256 mismatch: expected $expected, got $actual" }
    Write-Output "sha256 verified: $actual"
  } else {
    Fail "artifact has no checksum file; refusing to install an unverifiable build"
  }

  # 2. verify the artifact identifies itself as the stamped release
  #    (join the lines: -notmatch on an array filters elements, which would
  #    make the version line alone "fail" the fingerprint check)
  $version = (& $exe --version) -join "`n"
  $version.Split("`n") | ForEach-Object { Write-Output "  $_" }
  if ($version -notmatch "release [0-9a-f]{7}-[0-9a-f]{12}") {
    Fail "artifact does not report a release fingerprint; refusing to install"
  }

  # 3. install
  if (-not (Test-Path $Target)) { New-Item -ItemType Directory -Path $Target | Out-Null }
  $dest = Join-Path $Target "oaset.exe"
  Copy-Item $exe $dest -Force
  Write-Output "installed: $dest"

  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  if ($userPath -notlike "*$Target*") {
    [Environment]::SetEnvironmentVariable("Path", "$Target;$userPath", "User")
    Write-Output "added $Target to the user PATH (open a new terminal)"
  } else {
    Write-Output "$Target is already on the user PATH"
  }
  Write-Output "`nopen a new terminal and run: oaset --version"
}
finally {
  Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
}
