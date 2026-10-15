### Scenes and live output

- Unified active presentations and the idle screen in the Solin Content source. New scene collections start with Media and Camera scenes, without requiring a separate Default scene. Existing customized compositions are preserved during migration.
- Projection and virtual-camera/recording outputs now keep independent scene selections and return destinations after automatic media playback, respecting manual scene changes.
- Changes between the idle screen and presented content now use the destination scene's transition, including when both use the same scene. Idle videos loop without audio and restart from the beginning when shown again.
- New media, camera, and scene-reference layers fit the entire source inside the layer by default, avoiding unintended cropping. Existing framing is preserved, and fill and stretch remain available.
- Fixed scene preview retaining an image or video after it was closed.

### Cameras

- Added camera resolution, frame-rate, and format controls based on the combinations supported by each device. Automatic selection targets modes up to 1920×1080 at 30 FPS when available; supported custom modes can also be selected. Selected capture settings are now applied consistently across platforms.
- Camera settings load asynchronously and reuse discovered capabilities. Unavailable saved modes remain identified, and capture failures or unexpected frame dimensions now produce clearer feedback.

### Media and interface

- Thumbnails, media titles, and file information now use the media libraries bundled with Solin, removing the need for a separate FFmpeg installation for these features. Thumbnails prefer embedded cover art and fall back to a video frame.
- Improved preparation and cancellation of visual content, including first-frame handling and resource cleanup when replacing content or closing the app.
- Fixed buttons that could remain highlighted after the pointer left the window.
