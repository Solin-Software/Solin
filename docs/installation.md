# Installing Solin

This guide covers installation and first launch. For configuration and everyday
use, follow the [user guide](https://solinav.vercel.app/guide/).

Choose the file for your computer from the
[latest stable release](https://github.com/Solin-Software/Solin/releases/latest).
Beta versions are listed under
[all releases](https://github.com/Solin-Software/Solin/releases).

## Windows

Download the Windows 64-bit installer for Windows 10 version 1809 or newer.
Open it and follow the installation steps. You can install for your own account
or for everyone using the computer; the latter requires administrator access.

## macOS

Download the Apple Silicon disk image for an Apple M-series Mac, or the Intel
disk image for an Intel Mac. Solin requires macOS 13 or newer. Open the disk
image and drag Solin to Applications.

Open Solin from Applications. If macOS blocks the first launch, follow the
[macOS first-launch help](#macos-first-launch) below.

## Linux

Download the 64-bit AppImage. Ubuntu 24.04 is the reference system. In your file
manager, open the downloaded file's properties, allow it to run as a program,
and then open it.

The AppImage uses WebKitGTK 4.1, GTK 3, and libsoup 3 from your Linux system.
If Solin reports missing libraries at launch, install the packages listed in
the message using your distribution's package manager.

## Optional software

- Install LibreOffice to import Office documents and presentations as static
  pages. Animations and embedded-media playback are not preserved.
- Install FFmpeg for media thumbnails, file information, and embedded cover art.

## macOS first launch

If macOS cannot verify the developer or check the app, confirm that your copy
came from Solin's release page and that you trust it. After attempting to open
Solin:

1. Open **System Settings → Privacy & Security**.
2. Find the message about Solin and choose **Open Anyway**, if offered.
3. Confirm **Open** when prompted.

If the approval option is unavailable or macOS reports that the app is damaged,
download a fresh copy and replace the app in Applications. If the problem
continues, report the exact message. On a managed computer, ask its
administrator for help.

## After installation

Open Solin once after installation or an update. The
[user guide](https://solinav.vercel.app/guide/) explains initial configuration
and how to check your display and audio setup before a meeting.

Consult the [FAQ](https://solinav.vercel.app/faq/) for common questions. If an
installation problem persists,
[open an issue](https://github.com/Solin-Software/Solin/issues/new/choose)
using the bug-report form. Remove private information from screenshots or logs.

[Back to the README](../README.md)
