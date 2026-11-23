**Solin Scenes** are out of beta. Set up your layouts in advance and switch scenes independently for the projector and the virtual camera, which is also useful for sign language meetings. Control the timer from a phone or tablet with **Solin Timer** and, on Windows, track raised hands in Zoom from Solin.

### Upgrade notes

> [!IMPORTANT]
>
> **If you already use remote media control**, enable **Media** in **Settings → Remote access** and set up its login again. Use the new address shown there to open **Solin Media** and reinstall the app if you had it installed before. If you already installed the certificate, there’s no need to install it again.

### Added

- **Timer**
  - With **Solin Timer**, the meeting chairman or another designated brother can control the timer from a phone or tablet on the same network as Solin, while the operator handles audio and video. View the clock and countdown in full screen without signing in. Sign in to start, stop, or adjust the timer.
  - Solin automatically fills in midweek meeting parts and their planned durations from the workbook.
  - To let the person on the platform know someone wants to comment, select **Show hand on timer** next to **Export PDF**. The alert appears on the timer monitor or in Solin Timer, even when you aren’t using Zoom.
- **Zoom (Windows)**
  - Turn on monitoring in Solin’s **Raised hands** tab to see who wants to comment and whether their microphone is muted. From the list, ask someone to unmute, lower their hand, or mute them and lower their hand in one step. Keep **Participants** open in Zoom to follow the list. You can also show the names on the timer display.
  - Turn on **Mute microphone during playback** to mute the room microphone in Zoom while Solin plays media with sound. When playback pauses or ends, Solin unmutes the microphone if it muted it.
- In **meetings and playlists**, use **Delete additional media** to clear added media from a section and its subsections in one step while keeping the section structure, markers, and official meeting media. If you delete the section itself, choose whether to keep or delete its media.
- In **Settings → About**, check the requirements for each version and whether your system meets them. If there’s a package for your platform and architecture, open its download page from the release history.

### Changed

- **New profiles** use Solin Scenes by default. Open **Scenes** to combine cameras and media and, with compatible PTZ cameras, automatically adjust the framing when a scene goes live. OBS integration is still available in Settings.
- In **Remote access**, enable **Solin Media** and **Solin Timer** separately and set up a login for each. You can install both on the same device and see which devices are signed in to each app from the toolbar.

### Fixed

- Live video from **RTSP IP cameras** has less delay, helping bring the picture closer to the room audio for people watching on Zoom.
- On **Zoom for Windows**, muting all participants or asking them to unmute now works even when **Participants** is under **More meeting controls** on the compact toolbar.
- **Toolbar panels** stay open when you switch to another app. A single click on another control in Solin closes the panel and activates the control.
- **Linked folder sync** handles brief file changes more reliably during setup. If saving fails temporarily, Solin keeps meeting edits and tries again. Restored official media stays visible when older changes arrive from another computer.
- **NDI controls** are visible again in Solin’s OBS settings, so you can select the feed to project.
- Solin can fetch the **yeartext** automatically again.
