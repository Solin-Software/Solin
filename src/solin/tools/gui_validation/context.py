"""Child-side harness context: the live libobs runtime + readback/asset helpers.

Instantiated once inside each check's subprocess. Boots the process-wide
``obs_runtime`` on first use and offers the primitives the checks share: main-mix
frame capture, colour-scene documents, and on-demand test assets (clips, images,
tagged media) generated with ffmpeg/Qt so the harness needs no fixtures on disk.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solin.tools.gui_validation.harness import CheckResult, CheckStatus, HarnessConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CapturedFrame:
    width: int
    height: int
    data: bytes
    stride: int

    def bgra_at(self, x: int, y: int) -> tuple[int, int, int, int]:
        off = y * self.stride + x * 4
        b, g, r, a = self.data[off:off + 4]
        return (b, g, r, a)

    def center_bgra(self) -> tuple[int, int, int, int]:
        return self.bgra_at(self.width // 2, self.height // 2)


class HarnessContext:
    def __init__(self, config: HarnessConfig) -> None:
        self.config = config
        self.spec: Any = None  # set by execute_check so result() can label itself
        self._runtime: Any = None

    # ── result helper ──────────────────────────────────────────────────────

    def result(
        self,
        status: CheckStatus,
        summary: str,
        *,
        details: dict | None = None,
        artifact: str | None = None,
    ) -> CheckResult:
        return CheckResult(
            name=self.spec.name if self.spec else "?",
            category=self.spec.category if self.spec else "",
            status=status, summary=summary,
            details=details or {}, artifact=artifact,
        )

    def skip(self, why: str, **details) -> CheckResult:
        return self.result(CheckStatus.SKIP, why, details=details)

    # ── runtime ────────────────────────────────────────────────────────────

    @property
    def runtime(self) -> Any:
        if self._runtime is None:
            from solin.core.media.obs_runtime import obs_runtime

            self._runtime = obs_runtime()
            self._runtime.ensure_started()
        return self._runtime

    @property
    def ob(self) -> Any:
        return self.runtime.ob

    def close(self) -> None:
        if self._runtime is not None:
            try:
                self._runtime.shutdown()
            except Exception:  # noqa: BLE001 - teardown is best-effort in a dying child
                log.debug("runtime shutdown during close errored", exc_info=True)
            self._runtime = None

    # ── scene documents ────────────────────────────────────────────────────

    @staticmethod
    def color_document(*scenes: tuple[str, str]) -> dict:
        """A document with one full-frame colour source per (scene_id, hex) pair."""
        sources, scene_defs = [], []
        for scene_id, hex_color in scenes:
            source_id = f"src-{scene_id}"
            sources.append({
                "id": source_id, "type": "color", "name": source_id,
                "configuration": {"color": hex_color},
            })
            scene_defs.append({
                "id": scene_id,
                "layers": [{
                    "id": f"layer-{scene_id}", "source_id": source_id, "visible": True,
                    "rect": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
                }],
            })
        return {"sources": sources, "scenes": scene_defs}

    # ── main-mix frame capture ─────────────────────────────────────────────

    def capture_program_frame(self, timeout: float = 6.0) -> CapturedFrame | None:
        """Capture one composited main-mix frame as tight BGRA."""
        ob = self.ob
        width, height = self.runtime.video.width, self.runtime.video.height
        got: dict = {}
        ready = threading.Event()

        def on_frame(planes, linesizes, w, h, _fmt, _ts):
            if ready.is_set() or not planes or w <= 0:
                return
            got["frame"] = CapturedFrame(w, h, bytes(planes[0]), int(linesizes[0]))
            ready.set()

        cb = ob.add_raw_video_callback(
            on_frame, format=int(ob.VideoFormat.BGRA), width=width, height=height)
        try:
            ready.wait(timeout=timeout)
        finally:
            try:
                ob.remove_raw_video_callback(cb)
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("remove_raw_video_callback errored", exc_info=True)
        return got.get("frame")

    def dwell(self) -> None:
        """Hold the current composite so an operator can watch (``--hold``)."""
        if self.config.hold_seconds > 0:
            time.sleep(self.config.hold_seconds)

    # ── colour classification ──────────────────────────────────────────────

    @staticmethod
    def classify(bgra: tuple[int, int, int, int]) -> str:
        b, g, r, _a = bgra
        if max(b, g, r) < 40:
            return "black"
        if r > 150 and g < 90 and b < 90:
            return "red"
        if g > 150 and r < 90 and b < 90:
            return "green"
        if b > 150 and r < 90 and g < 90:
            return "blue"
        return "other"

    # ── test assets (generated on demand) ──────────────────────────────────

    def ensure_clip(self) -> str | None:
        """A short playable video clip: the configured one, or a generated green one."""
        if self.config.clip_path and Path(self.config.clip_path).exists():
            return self.config.clip_path
        import shutil

        if not shutil.which("ffmpeg"):
            return None
        out = self.config.resolved_out_dir() / "clip-green.mp4"
        if out.exists():
            return str(out)
        cmd = [
            "ffmpeg", "-y", "-f", "lavfi",
            # 0x00FF00 (lime), not the "green" preset (0,128,0) — the readback check
            # classifies bright green.
            "-i", f"color=c=0x00FF00:s={self.config.width}x{self.config.height}:d=2:r={self.config.fps}",
            "-pix_fmt", "yuv420p", str(out),
        ]
        if subprocess.run(cmd, capture_output=True).returncode != 0 or not out.exists():
            return None
        return str(out)

    def ensure_image(self, hex_color: str = "#0000FF", name: str = "image-blue.png") -> str | None:
        """A solid-colour PNG (via Qt, no ffmpeg needed)."""
        from PySide6.QtGui import QImage

        out = self.config.resolved_out_dir() / name
        if out.exists():
            return str(out)
        img = QImage(self.config.width, self.config.height, QImage.Format.Format_RGB32)
        img.fill(_qcolor(hex_color))
        return str(out) if img.save(str(out), "PNG") else None

    def ensure_tagged_media(self, title: str = "Solin Test Title") -> tuple[str | None, bool]:
        """(path, has_cover) for a media file carrying a Title tag + embedded cover.

        Returns ``(None, False)`` when ffmpeg is unavailable. ``has_cover`` reflects
        the file's *actual* content: the cache is keyed on the embedded state
        (distinct filenames), a failed generation removes its partial output rather
        than caching it, and — when ffprobe is present — a claimed cover is verified
        against a real attached-picture stream. So a title-only file never reports a
        cover it does not have (which would flip the routed-metadata check to a
        false FAIL).
        """
        import shutil

        if not shutil.which("ffmpeg"):
            return None, False
        out_dir = self.config.resolved_out_dir()
        cover = self.ensure_image("#FF00FF", "cover.png")
        want_cover = bool(cover)
        out = out_dir / ("tagged-with-cover.mp3" if want_cover else "tagged-title-only.mp3")
        if not out.exists():
            cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
            if want_cover:
                cmd += ["-i", cover, "-map", "0:a", "-map", "1:v", "-c:v", "copy",
                        "-disposition:v:0", "attached_pic"]
            cmd += ["-metadata", f"title={title}", str(out)]
            if subprocess.run(cmd, capture_output=True).returncode != 0 or not out.exists():
                try:
                    out.unlink()  # never cache a partial/failed file
                except OSError:
                    pass
                return None, False
        return str(out), (want_cover and self._file_has_cover(out))

    @staticmethod
    def _file_has_cover(path) -> bool:
        """Whether ``path`` carries a video/attached-picture stream (best effort)."""
        import shutil

        if not shutil.which("ffprobe"):
            return True  # cannot verify → trust the generation that just succeeded
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v",
             "-show_entries", "stream=codec_type", "-of", "default=nw=1", str(path)],
            capture_output=True, text=True)
        return "codec_type=video" in probe.stdout

    def save_png(self, frame: CapturedFrame, name: str) -> str | None:
        """Persist a captured frame as a PNG artifact for the report."""
        from PySide6.QtGui import QImage

        image = QImage(frame.data, frame.width, frame.height, frame.stride,
                       QImage.Format.Format_ARGB32)
        out = self.config.resolved_out_dir() / name
        # copy() detaches from the transient buffer before it is freed.
        return str(out) if image.copy().save(str(out), "PNG") else None


def _qcolor(hex_color: str):
    from PySide6.QtGui import QColor

    return QColor(hex_color)
