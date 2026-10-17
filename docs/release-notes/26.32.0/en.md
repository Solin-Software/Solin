This release brings a redesigned Settings interface, support for compact windows for side-by-side use, more reliable shared-folder synchronization, and a refreshed Solin Scenes experience.

> If you use Linked Folders on multiple computers, update all of them before resuming shared edits. Earlier versions are incompatible with the new synchronization protocol.

### Added:

- **Meetings:** Congregation search to automatically fill in meeting days and times.
- **Timers:**
  - Access to options through the settings button in the Timer view.
  - Adjust elapsed time during a meeting part in one-second increments without changing its planned duration.
- **Solin Scenes:**
  - Live previews in the Scenes panel.
  - Independent scene selection for projection and the virtual camera.
- **Media:** Context menus for sending items directly to supported destinations, setting local images or videos as idle-screen content, and adding the same video to a playlist more than once.
- **Window:** Support for windows as narrow as 460 pixels, with automatic sidebar collapse below 560 pixels.

### Changed:

- **Settings:** Rebuilt interface with a responsive layout.
- **Framing Editor:** Shows the full source with the crop area overlaid, making it easier to reposition content when editing layers in Solin Scenes.
- **Scenes and Projection:** New supervised playback and scene-compositing engine based on libobs.
- **Linked Folders:** New synchronization protocol that better handles concurrent edits and interrupted connections.
- **Updater:** Moved application updates to GitHub Releases, with file integrity verification and resumable downloads.
- **General:** Refined translations and updated the project license to GPL-3.0-or-later.

### Fixed:

- **Meetings:** Inconsistencies and duplicates that could occur when restoring meeting content.