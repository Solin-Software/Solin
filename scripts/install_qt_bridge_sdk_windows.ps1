[CmdletBinding()]
param(
    [string]$InstallDirectory = (
        Join-Path $PSScriptRoot "..\build\dependencies\qt-bridge-sdk\6.11.1\msvc2022_64"
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
    throw "The Qt media bridge SDK requires Windows x64."
}

$qtVersion = "6.11.1"
$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$dependencyRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $repositoryRoot "build\dependencies\qt-bridge-sdk")
)
$resolvedInstallDirectory = [System.IO.Path]::GetFullPath($InstallDirectory)
$allowedPrefix = $dependencyRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar) +
    [System.IO.Path]::DirectorySeparatorChar
if (-not $resolvedInstallDirectory.StartsWith(
    $allowedPrefix,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "The Qt bridge SDK install directory must stay under $dependencyRoot."
}

$repositoryUri = (
    "https://download.qt.io/online/qtsdkrepository/windows_x86/desktop/" +
    "qt6_6111/qt6_6111_msvc2022_64"
)
$packages = @(
    @{
        Component = "qt.qt6.6111.win64_msvc2022_64"
        FileName = (
            "6.11.1-0-202605090529qtbase-Windows-Windows_11_24H2-" +
            "MSVC2022-Windows-Windows_11_24H2-X86_64.7z"
        )
        Sha256 = "7f97edc3937fec7383eb865e010ed5128155bf9c80a563abca450860f3e9bef5"
    },
    @{
        Component = "qt.qt6.6111.addons.qtmultimedia.win64_msvc2022_64"
        FileName = (
            "6.11.1-0-202605090529qtmultimedia-Windows-Windows_11_24H2-" +
            "MSVC2022-Windows-Windows_11_24H2-X86_64.7z"
        )
        Sha256 = "e7255e7ed621c6e4fc5af9f5a1614b45260a960f03bcb4b1f37f8e910f6c8fd9"
    }
)

function Test-InstalledSdk {
    $requiredFiles = @(
        "include\QtCore\qglobal.h",
        "include\QtGui\6.11.1\QtGui\private\qrhi_p.h",
        "include\QtMultimedia\6.11.1\QtMultimedia\private\qhwvideobuffer_p.h",
        "include\QtMultimedia\6.11.1\QtMultimedia\private\qvideoframe_p.h",
        "lib\Qt6Core.lib",
        "lib\Qt6Gui.lib",
        "lib\Qt6Multimedia.lib"
    )
    foreach ($relativePath in $requiredFiles) {
        if (-not (Test-Path -LiteralPath (
            Join-Path $resolvedInstallDirectory $relativePath
        ) -PathType Leaf)) {
            return $false
        }
    }
    return $true
}

if (-not $ForceDownload -and (Test-InstalledSdk)) {
    Write-Output $resolvedInstallDirectory
    return
}

$sevenZip = Get-Command "7z.exe" -ErrorAction SilentlyContinue
if ($null -eq $sevenZip) {
    $programFilesSevenZip = Join-Path $env:ProgramFiles "7-Zip\7z.exe"
    if (Test-Path -LiteralPath $programFilesSevenZip -PathType Leaf) {
        $sevenZipPath = $programFilesSevenZip
    }
    else {
        throw "7-Zip is required to extract the pinned Qt SDK."
    }
}
else {
    $sevenZipPath = $sevenZip.Source
}

$downloadDirectory = Join-Path $repositoryRoot "build\downloads\qt-bridge-sdk"
$stagingDirectory = Join-Path (
    Split-Path -Parent $resolvedInstallDirectory
) (".staging-" + [System.Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $downloadDirectory | Out-Null
New-Item -ItemType Directory -Force -Path $stagingDirectory | Out-Null

try {
    foreach ($package in $packages) {
        $archivePath = Join-Path $downloadDirectory $package.FileName
        $archiveIsValid = $false
        if (Test-Path -LiteralPath $archivePath -PathType Leaf) {
            $actualDigest = (
                Get-FileHash -LiteralPath $archivePath -Algorithm SHA256
            ).Hash.ToLowerInvariant()
            $archiveIsValid = $actualDigest -eq $package.Sha256
        }
        if ($ForceDownload -or -not $archiveIsValid) {
            $partialPath = "$archivePath.partial"
            Remove-Item -LiteralPath $partialPath -Force -ErrorAction SilentlyContinue
            try {
                $ProgressPreference = "SilentlyContinue"
                $downloadUri = (
                    "$repositoryUri/$($package.Component)/$($package.FileName)"
                )
                Invoke-WebRequest -UseBasicParsing -Uri $downloadUri -OutFile $partialPath
                $actualDigest = (
                    Get-FileHash -LiteralPath $partialPath -Algorithm SHA256
                ).Hash.ToLowerInvariant()
                if ($actualDigest -ne $package.Sha256) {
                    throw "Downloaded Qt SDK component failed SHA-256 verification."
                }
                Move-Item -LiteralPath $partialPath -Destination $archivePath -Force
            }
            finally {
                Remove-Item -LiteralPath $partialPath -Force -ErrorAction SilentlyContinue
            }
        }
        $extract = Start-Process `
            -FilePath $sevenZipPath `
            -ArgumentList @("x", "-y", "-o$stagingDirectory", $archivePath) `
            -Wait `
            -PassThru `
            -WindowStyle Hidden
        if ($extract.ExitCode -ne 0) {
            throw "7-Zip failed to extract a Qt SDK component."
        }
    }

    if (Test-Path -LiteralPath $resolvedInstallDirectory) {
        Remove-Item -LiteralPath $resolvedInstallDirectory -Recurse -Force
    }
    Move-Item -LiteralPath $stagingDirectory -Destination $resolvedInstallDirectory
}
finally {
    Remove-Item -LiteralPath $stagingDirectory -Recurse -Force -ErrorAction SilentlyContinue
}

if (-not (Test-InstalledSdk)) {
    throw "The pinned Qt $qtVersion bridge SDK installation is incomplete."
}

Write-Output $resolvedInstallDirectory
