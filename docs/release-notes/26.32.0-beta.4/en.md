### Highlights

- Rebuilt Settings with a responsive interface that adapts better to narrow windows and keeps dialogs, scrolling, and input controls consistent across the app.
- Added congregation lookup to onboarding and meeting settings. Solin can now fill the published midweek and weekend meeting days and times automatically, while manual entry remains available.
- Media and Scenes now run on a supervised libobs engine, unifying playback, projection, cameras, RTSP sources, transitions, recording, previews, thumbnails, and virtual-camera output across supported platforms.
- Reworked linked-folder synchronization with durable operations and deterministic reconciliation so collaborative meeting and playlist folders recover and converge more reliably after concurrent or interrupted changes.
- Moved application updates to verified GitHub releases with resumable downloads and a unified installer flow across supported platforms.

### Media and projection

- Added media context actions for sending items to supported destinations and for setting idle-screen content directly from playlist and meeting menus.
- Playlists and linked folders can now contain repeated occurrences of the same video while sharing the underlying media resource. Meeting media remains protected from unintended duplicates.
- Fixed image actions in the embedded browser for pages that use relative image URLs, including pages with a custom document base URL.

### Scenes, cameras, and live output

- The new libobs-based runtime keeps scene composition, media playback, cameras, RTSP sources, transitions, program recording, audio-device selection, previews, thumbnails, projection, and virtual-camera output under one supervised engine.
- Improved framing in Scenes: the editor now shows the full source with the current crop overlaid, making it easier to widen or reposition the visible area to match the output aspect ratio.

### Meetings, playlists, and timers

- Fixed publication-group identity collisions that could incorrectly show restore options or create duplicate identities after restoring meeting content. Existing saved trees and sync state are migrated while preserving manual edits and media state.
- Added a responsive advanced timer settings page and improved week/part controls on narrow layouts.
- Improved linked-folder lifecycle behavior so deactivating synchronization removes internal sync state without removing the visible media already in the meeting or playlist.
- Refined timer mode labels and Portuguese and Spanish translations for clearer, more consistent wording.

### Updates and reliability

- Solin now uses a centralized release manifest with verified asset hashes for update discovery and installation.
