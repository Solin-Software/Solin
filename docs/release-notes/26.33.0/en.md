Solin Scenes comes to macOS: combine cameras and media and choose what to show on the projector and in Zoom, with an independent scene for each output. This release also makes it easier to prepare public-talk media, operate PTZ cameras, and record meetings with smaller files. On Windows, fixed background noise that could occur when playing videos and audio on less powerful computers.

### Upgrade notes

> [!IMPORTANT]
>
> - **Linux:** Check your system before updating: this version requires glibc 2.43 or newer, with Ubuntu 26.04 as the reference system. If you use Ubuntu 24.04 or earlier, upgrade your system to use this Solin version. Use X11 or Wayland with XWayland.
> - **Virtual camera on macOS:** To send scenes to Zoom, install OBS, enable its **OBS Virtual Camera** extension, and select that camera in Zoom. Stop the output in OBS before enabling it in Solin. The extension is not needed to display scenes on TVs or a projector.

### Added

- **Solin Scenes:**
  - On macOS, send the complete scene composition to projection and virtual-camera output. For example, show media on the projector and a camera composition in Zoom, controlling each output separately.
  - Watch a scene in a larger window while working elsewhere in Solin. For compatible PTZ cameras, move, zoom, and save presets with thumbnails to recognize each framing. Solin asks for confirmation before moving a camera that is on air.
  - Add ONVIF-compatible IP cameras using network or address search. Choose a discovered video stream and let Solin fill in its RTSP and PTZ settings.
  - Change framing with the **Merge** transition: a camera shared by both scenes moves and resizes smoothly into its new position. Sources entering or leaving the composition fade in or out.
- **Publications:**
  - Add only the media you need: when opening a `.jwpub` file, inspect its contents and select the images, videos, or audio to insert into your meeting or playlist. Selection also works with files downloaded through Solin's browser.
  - To prepare a public talk, open **Public talk media…**, choose the S-34mp file containing the talk-outline media, and select the items you will use. Solin remembers the chosen file and places the media in the public-talk section of weekend meetings.
- **Zoom (Windows):**
  - Prepare Zoom with **Start of meeting**: Solin connects your audio and video and mutes participants. For conversation before or afterward, **Before/after meeting** disconnects your audio and video and asks participants to unmute.
  - Automate keyboard shortcuts alongside these controls: in **Automatic Shortcuts**, choose **Before meeting**, **Start of meeting**, or **End of meeting** to run your configured keys after the Zoom actions.
  - Link the background song to the controls to stop it at meeting start and play it before or afterward. If you use the song's scheduled stop, it can also prepare Zoom for the start of the meeting.
- **Updates:** Read what changed in each version in **Settings → About**. You can also opt into betas to try features before the stable release; showing beta notes in the history is a separate option.

### Changed

- **Solin Scenes:**
  - Control projection and virtual-camera scenes independently with the monitor and camera buttons, including in the editor. To show the same composition on both outputs, select the same scene for each. These controls replace **Take live** and the **Show in media windows** link. In the scene panel, use the monitor toggle to show projection controls when needed; they are hidden by default.
  - Reach scenes outside the visible part of the panel with the mouse wheel, touchpad, or scrollbar. Floating panels fit the available screen space.
  - Build framing that extends beyond the scene edges: move sources partly outside the canvas, including with negative coordinates, and resize or crop them without having them jump back inside the frame.
- **Recording:** Record meetings using less disk space, with optimized encoding and 720p as the default resolution. Choose 360p, 540p, 720p, or 1080p to balance detail and file size without changing the virtual-camera resolution sent to Zoom.
- **Background songs:** Test or play a video or audio clip without hearing two sources at once: Solin automatically mutes the background song and restores its volume when you leave the media. The song keeps advancing silently and stays muted during pauses and buffering.
- **Meeting storage:** Solin uses disk space more intelligently: it cleans up temporary extractions, removes old, unused publications from the cache, and stores a single copy of identical images used by multiple profiles. This can significantly reduce accumulated storage, especially when preparing many meetings or using several languages and profiles.
- **Linked folders:** Place media in a meeting's folder to have it appear in Solin, even without synchronizing the meeting's organization between computers. You can now create that folder from Solin and keep it when disabling synchronization; deleting it is a separate, confirmed action. Linked playlist folders can also be created from the application.
- **Profiles:**
  - Find and select profiles more easily in small windows or when you have many profiles. The screen adapts to the available space, with scrolling and keyboard navigation.
  - When creating a profile, choosing a congregation fills in the profile name and enables automatic background songs when meeting times are available. Profile names you have edited are preserved.

### Fixed

- **Cameras:**
  - Connect a camera while Solin is open and it appears in the list automatically. On Windows, reconnecting a camera already used in a scene can restore its picture without restarting Solin, including when it was disconnected at startup.
  - Follow IP cameras with less delay added by Solin: RTSP reception has been adjusted to prioritize live video and skip processing camera audio. Fixes also ensure TCP/UDP and buffer settings are applied and camera pictures appear in previews, projection, and thumbnails with the virtual camera off.
- **Virtual camera:** See when video is actually reaching the output. If delivery stalls, Solin tries to recover it automatically and notifies you if it cannot, rather than continuing to indicate that video is working.
- **Playback:** More reliable playback of local and network files, with fixes for failures when changing speed, seeking, and recovering from connection interruptions. Fixes also prevent unintended restarts when media ends. On Windows, improved audio continuity helps prevent dropouts, crackling, and background noise that could be more noticeable on less powerful computers.
- **Meetings:**
  - Choosing manual downloads now also applies to meeting publications. Previously, they were still downloaded while browsing, even with automatic downloads disabled. Now, click **Download** when you want to prepare a meeting that is not yet available.
  - Prepared meetings receive content updates and recover missing images while preserving your edits, reducing the need to prepare the meeting again when an image disappears.
- **Projection:**
  - Moving the mouse onto the projection screen no longer leaves the pointer over the content shown to the audience.
  - When adjusting a meeting or playlist photo or illustration in the interactive preview, **Match projection aspect** and **Keep image covering the frame** could change or reset the image's zoom and position on the TV without clicking **Apply to Projector**. These options now adjust only the preview; the projected image changes when you apply the adjustments.
- **Updates:** Open release-note links without losing the text you were reading. On supported Windows installations, click **Update now** once: Solin downloads and verifies the package, then starts the installer.
- **Dialogs:** Notifications, updates, and media-destination dialogs open centered on Solin, including after moving it to another monitor.

### Removed

- **Zoom (Windows):** Removed the attendance count because it could omit participants outside the visible portion of Zoom's list.
