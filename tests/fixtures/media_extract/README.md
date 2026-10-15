# Media extraction fixtures

These small, synthetic files test the bundled decoder without requiring FFmpeg
command-line tools during test execution. They contain generated colors and a
440 Hz tone, with no third-party media.

- `video.mp4`: two-second red MPEG-4 video, 32 × 24, with a Unicode title.
- `video-cover.mp4`: the same video with an attached green JPEG cover.
- `video-rotated.mp4`: the same video with a 90-degree display matrix.
- `video-sar.mp4`: blue video with a 2:1 sample aspect ratio.
- `video-short.mp4`: a single blue video frame.
- `audio.mp3`: half-second MP3 with a title and no cover.
- `audio-cover.mp3`: the same tone with an attached green JPEG cover.

Fixtures were generated with FFmpeg 7.1. Test assertions check content,
metadata and display geometry rather than depending on a particular encoder's
exact compressed bytes.
