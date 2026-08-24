[CmdletBinding()]
param(
    [ValidateSet("runtime", "devel", "debug")]
    [string]$InstallType = "devel",
    [string]$InstallDirectory = (
        Join-Path $PSScriptRoot "..\build\dependencies\gstreamer\msvc_x86_64"
    ),
    [switch]$ForceDownload
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (
    [System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT -or
    [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture -ne
        [System.Runtime.InteropServices.Architecture]::X64
) {
    throw "The pinned GStreamer toolchain requires Windows x64."
}

$version = "1.28.5"
$fileName = "gstreamer-1.0-msvc-x86_64-$version.exe"
$expectedSha256 = "51ee5eaec33008e8409d8cf6f6884457f22aa3bd515f8856f993a3eaab903530"
$downloadUri = "https://gstreamer.freedesktop.org/pkg/windows/$version/msvc/$fileName"
$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$downloadDirectory = Join-Path $repositoryRoot "build\downloads"
$installerPath = Join-Path $downloadDirectory $fileName
$resolvedInstallDirectory = [System.IO.Path]::GetFullPath($InstallDirectory)

New-Item -ItemType Directory -Force -Path $downloadDirectory | Out-Null
New-Item -ItemType Directory -Force -Path $resolvedInstallDirectory | Out-Null

function Test-InstalledToolchain {
    $requiredFiles = @("bin\gst-launch-1.0.exe")
    if ($InstallType -ne "runtime") {
        $requiredFiles += @(
            "bin\pkg-config.exe",
            "include\gstreamer-1.0\gst\gst.h",
            "lib\pkgconfig\gstreamer-1.0.pc"
        )
    }
    foreach ($relativePath in $requiredFiles) {
        if (-not (Test-Path -LiteralPath (Join-Path $resolvedInstallDirectory $relativePath) -PathType Leaf)) {
            return $false
        }
    }
    $gstLaunch = Join-Path $resolvedInstallDirectory "bin\gst-launch-1.0.exe"
    $versionOutput = & $gstLaunch --version 2>$null | Out-String
    return $LASTEXITCODE -eq 0 -and $versionOutput -match [regex]::Escape($version)
}

if (-not $ForceDownload -and (Test-InstalledToolchain)) {
    Write-Output $resolvedInstallDirectory
    return
}

function Test-InstallerDigest {
    if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
        return $false
    }
    $actual = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToLowerInvariant()
    return $actual -eq $expectedSha256
}

if ($ForceDownload -or -not (Test-InstallerDigest)) {
    $partialPath = "$installerPath.partial"
    Remove-Item -LiteralPath $partialPath -Force -ErrorAction SilentlyContinue
    try {
        $ProgressPreference = "SilentlyContinue"
        Invoke-WebRequest -UseBasicParsing -Uri $downloadUri -OutFile $partialPath
        $actual = (Get-FileHash -LiteralPath $partialPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $expectedSha256) {
            throw "Downloaded GStreamer installer failed SHA-256 verification."
        }
        Move-Item -LiteralPath $partialPath -Destination $installerPath -Force
    }
    finally {
        Remove-Item -LiteralPath $partialPath -Force -ErrorAction SilentlyContinue
    }
}

if (-not (Test-InstallerDigest)) {
    throw "Pinned GStreamer installer is missing or has an invalid digest."
}

$arguments = @(
    "/VERYSILENT",
    "/SUPPRESSMSGBOXES",
    "/NORESTART",
    "/CURRENTUSER",
    "/TYPE=$InstallType",
    "/DIR=`"$resolvedInstallDirectory`""
)
$process = Start-Process `
    -FilePath $installerPath `
    -ArgumentList $arguments `
    -Wait `
    -PassThru `
    -WindowStyle Hidden
if ($process.ExitCode -ne 0) {
    throw "GStreamer installer exited with code $($process.ExitCode)."
}

if (-not (Test-InstalledToolchain)) {
    throw "GStreamer installation is incomplete or does not match version $version."
}

Write-Output $resolvedInstallDirectory
