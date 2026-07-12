[CmdletBinding()]
param(
    [switch] $SkipStandalone,
    [string] $Distribution = $env:SOLIN_WSL_DISTRO
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($Distribution)) {
    $Distribution = "Ubuntu-24.04"
}

Write-Host "Building Solin AppImage with WSL distribution $Distribution..."
$WslArguments = @(
    "--distribution", $Distribution,
    "--cd", $ProjectRoot
)
if ($SkipStandalone) {
    $WslArguments += @(
        "env", "SOLIN_APPIMAGE_SKIP_BUILD=1",
        "bash", "scripts/package_solin_appimage.sh"
    )
} else {
    $WslArguments += @("bash", "scripts/package_solin_appimage.sh")
}

& wsl.exe @WslArguments
if ($LASTEXITCODE -ne 0) {
    throw "AppImage build failed with exit code $LASTEXITCODE."
}

Write-Host "AppImage build completed in $(Join-Path $ProjectRoot 'dist')"
