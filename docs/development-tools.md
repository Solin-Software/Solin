# Development tools

Utilities under `tools/` are opt-in development aids. They are not imported by
the Solin runtime and must not be required for normal application startup.

## Translation editor

`tools/translation_editor/main.py` manages the Qt translation catalogs. See
[Translations](translations.md) for its dependencies and workflow.

## Development data manager

`tools/dev_data_manager.py` manages the isolated `SolinDev` state used during
development and migration testing:

```text
python tools/dev_data_manager.py
```

The tool can:

- delete the current user's `SolinDev` QSettings registry trees;
- delete the corresponding development data and cache directories;
- replace that state with a synthetic legacy fixture for migration testing.

These actions are destructive by design. Close every Solin development process,
inspect the paths displayed by the tool, and read the confirmation dialog before
continuing. It targets development namespaces only and must never be adapted to
delete real application data.

## Repository scripts

Repeatable validation, build, and packaging operations belong in `scripts/`.
Prefer a script over undocumented manual steps when an operation must produce
the same result for another contributor or a build runner.

On Windows, `python scripts/benchmark_zoom.py --samples 30` measures native
meeting discovery and cached audio/video reads with P50 and P95 timings. Open
a Zoom meeting first. The benchmark does not change meeting controls or focus.
Add `--raised-hands` with the Participants panel open to also measure passive
raised-hand reads, including their snapshot status and UIA queries per read.

### Zoom microphone during playback

With **Mute microphone during playback** enabled, Solin restores a microphone it
muted after 800 ms of sustained silence in foreground media. This applies to
local and streamed media, including silent credits. If audio returns, Solin
mutes again. Pausing or ending playback restores the microphone without waiting
for the silence interval.

Set `SOLIN_ZOOM_PLAYBACK_SILENCE_MS` before launching Solin to override the
interval in milliseconds. Use `0` to disable silence detection.
