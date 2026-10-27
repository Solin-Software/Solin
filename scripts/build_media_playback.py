"""Build the pinned OBS playback module against the installed pylibobs runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

if __package__:
    from .build_media_extract import (
        SDK_SPECS,
        _download_sdk,
        installed_runtime,
        prepare_sdk,
        target,
    )
    from .package_libobs_runtime import _publish_runtime, _relocate_macos, _retry_windows_sharing
else:
    from build_media_extract import SDK_SPECS, _download_sdk, installed_runtime, prepare_sdk, target
    from package_libobs_runtime import _publish_runtime, _relocate_macos, _retry_windows_sharing

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRECTORY = ROOT / "native" / "media_playback"
OBS_VERSION = "32.1.2"
SOURCE_URL = f"https://codeload.github.com/obsproject/obs-studio/tar.gz/refs/tags/{OBS_VERSION}"
SOURCE_SHA256 = "b4a59410cddb46d0e31df1ee13b8ec66f30862d7e980c1a8c4e3b5d16fae6053"
MODULE_NAME = "solin-media-source"
SOURCE_ID = "solin_media_source"
MANIFEST_NAME = "media-playback-manifest.json"
SUFFIXES = {"windows": ".dll", "linux": ".so", "macos": ".dylib"}
_PLAYBACK = "shared/media-playback/media-playback"


class MediaPlaybackBuildError(RuntimeError):
    """Pinned sources, toolchain, or installed playback module failed validation."""


def _replace_once(source: str, old: str, new: str, *, patch: str) -> str:
    if source.count(old) != 1:
        raise MediaPlaybackBuildError(f"Pinned OBS source no longer matches audited patch: {patch}")
    return source.replace(old, new, 1)


def _patch_terminal_source(source: str) -> str:
    old = """static void media_stopped(void *opaque)
{
\tstruct ffmpeg_source *s = opaque;
\tif (s->is_clear_on_media_end && !s->is_track_matte) {
\t\tobs_source_output_video(s->source, NULL);
\t}

\tif ((s->close_when_inactive || !s->is_local_file) && s->media)
\t\ts->destroy_media = true;

\tif (s->state != OBS_MEDIA_STATE_STOPPED) {
\t\tset_media_state(s, OBS_MEDIA_STATE_ENDED);
\t\tobs_source_media_ended(s->source);
\t}
}
"""
    new = """static void media_stopped(void *opaque, bool error)
{
\tstruct ffmpeg_source *s = opaque;
\tif (s->is_clear_on_media_end && !s->is_track_matte) {
\t\tobs_source_output_video(s->source, NULL);
\t}

\t/* Destruction belongs to the source owner, never the decoder thread. */
\tif (error || ((s->close_when_inactive || !s->is_local_file) && s->media))
\t\ts->destroy_media = true;

\tif (s->state != OBS_MEDIA_STATE_STOPPED) {
\t\tset_media_state(s, error ? OBS_MEDIA_STATE_ERROR : OBS_MEDIA_STATE_ENDED);
\t\tif (!error)
\t\t\tobs_source_media_ended(s->source);
\t}
}
"""
    source = _replace_once(source, old, new, patch="terminal error state")
    source = _replace_once(
        source,
        "\t\ts->media = media_playback_create(&info);",
        "\t\ts->media = media_playback_create(&info);\n"
        "\t\tif (!s->media)\n\t\t\tmedia_stopped(s, true);",
        patch="synchronous media creation failure",
    )
    source = _replace_once(
        source,
        "static void ffmpeg_source_start(struct ffmpeg_source *s)\n{\n",
        """static void ffmpeg_source_start(struct ffmpeg_source *s)
{
\t/* Explicit retry must not reuse a failed thread before the next tick. */
\tif (s->destroy_media) {
\t\tif (s->media)
\t\t\tmedia_playback_destroy(s->media);
\t\ts->media = NULL;
\t\ts->destroy_media = false;
\t}
\t/* Publish intent before opening: initialization can fail asynchronously. */
\tset_media_state(s, OBS_MEDIA_STATE_PLAYING);
""",
        patch="explicit retry rebuild",
    )
    source = _replace_once(
        source,
        "\tset_media_state(s, OBS_MEDIA_STATE_PLAYING);\n\tobs_source_media_started(s->source);",
        "\tif (s->state == OBS_MEDIA_STATE_PLAYING)\n\t\tobs_source_media_started(s->source);",
        patch="preserve asynchronous startup error",
    )
    source = _replace_once(
        source,
        """static void ffmpeg_source_play_pause(void *data, bool pause)
{
\tstruct ffmpeg_source *s = data;

\tif (!s->media)
\t\tffmpeg_source_open(s);

\tif (!s->media)
\t\treturn;

\tmedia_playback_play_pause(s->media, pause);

\tif (pause) {

\t\tset_media_state(s, OBS_MEDIA_STATE_PAUSED);
\t} else {

\t\tset_media_state(s, OBS_MEDIA_STATE_PLAYING);
\t\tobs_source_media_started(s->source);
\t}
}
""",
        """static void ffmpeg_source_play_pause(void *data, bool pause)
{
\tstruct ffmpeg_source *s = data;

\tif (!pause && (!s->media || s->destroy_media || s->state == OBS_MEDIA_STATE_ERROR ||
\t               s->state == OBS_MEDIA_STATE_ENDED)) {
\t\tffmpeg_source_start(s);
\t\treturn;
\t}
\tif (!s->media)
\t\treturn;

\tset_media_state(s, pause ? OBS_MEDIA_STATE_PAUSED : OBS_MEDIA_STATE_PLAYING);
\tmedia_playback_play_pause(s->media, pause);
\tif (!pause && s->state == OBS_MEDIA_STATE_PLAYING)
\t\tobs_source_media_started(s->source);
}
""",
        patch="play retries failed decoder through shared start",
    )
    source = _replace_once(
        source,
        """\tif (s->media && should_restart_media) {
\t\tmedia_playback_destroy(s->media);
\t\ts->media = NULL;
\t}
""",
        """\tif (s->media && should_restart_media) {
\t\tmedia_playback_destroy(s->media);
\t\ts->media = NULL;
\t\ts->destroy_media = false;
\t}
""",
        patch="replacement consumes pending destruction",
    )
    source = _replace_once(
        source,
        """\tif (s->media) {
\t\tmedia_playback_stop(s->media);
\t\tobs_source_output_video(s->source, NULL);
\t\tset_media_state(s, OBS_MEDIA_STATE_STOPPED);
\t}
""",
        """\tif (s->media) {
\t\t/* Stop wins over callbacks triggered by cancellation. */
\t\tset_media_state(s, OBS_MEDIA_STATE_STOPPED);
\t\tmedia_playback_stop(s->media);
\t\tobs_source_output_video(s->source, NULL);
\t}
""",
        patch="explicit stop precedence",
    )
    return _replace_once(
        source,
        """\tif (obs_source_showing(s->source))
\t\tffmpeg_source_start(s);

\tset_media_state(s, OBS_MEDIA_STATE_PLAYING);
""",
        """\tif (obs_source_showing(s->source))
\t\tffmpeg_source_start(s);
\telse
\t\tset_media_state(s, OBS_MEDIA_STATE_PLAYING);
""",
        patch="restart preserves initialization error",
    )


def _patch_terminal_callbacks(source: str, kind: str, variable: str) -> str:
    source = _replace_once(
        source,
        f"\tif (stopping && {variable}->stop_cb)\n\t\t{variable}->stop_cb({variable}->opaque);",
        f"\tif (stopping && {variable}->stop_cb)\n\t\t{variable}->stop_cb({variable}->opaque, false);",
        patch=f"{kind} normal stop callback",
    )
    old = f"""\tif (!mp_{kind}_thread({variable})) {{
\t\tif ({variable}->stop_cb) {{
\t\t\t{variable}->stop_cb({variable}->opaque);
\t\t}}
\t}}
"""
    new = f"""\tif (!mp_{kind}_thread({variable})) {{
\t\tpthread_mutex_lock(&{variable}->mutex);
\t\tbool cancelled = {variable}->kill || {variable}->stopping;
\t\tpthread_mutex_unlock(&{variable}->mutex);
\t\tif (!cancelled && {variable}->stop_cb)
\t\t\t{variable}->stop_cb({variable}->opaque, true);
\t}}
"""
    return _replace_once(source, old, new, patch=f"{kind} failure callback and cancellation")


def patch_source(name: str, source: str) -> str:
    """Apply the bounded playback changes; all upstream license headers remain."""
    if name == "plugins/obs-ffmpeg/obs-ffmpeg-source.c":
        source = _replace_once(
            source, '.id = "ffmpeg_source",', f'.id = "{SOURCE_ID}",', patch="source identifier"
        )
        return _patch_terminal_source(source)
    if name == f"{_PLAYBACK}/media-playback.h":
        return _replace_once(
            source,
            "typedef void (*mp_stop_cb)(void *opaque);",
            "typedef void (*mp_stop_cb)(void *opaque, bool error);",
            patch="private terminal callback contract",
        )
    if name == f"{_PLAYBACK}/cache.c":
        return _patch_terminal_callbacks(source, "cache", "c")
    if name == f"{_PLAYBACK}/media.h":
        return _replace_once(
            source,
            "\tbool eof;",
            "\tbool eof;\n\tbool terminal_packet_corrupt;\n\tbool completed;",
            patch="terminal demux packet status",
        )
    if name == f"{_PLAYBACK}/decode.c":
        old = "\tstream = d->stream = m->fmt->streams[ret];\n\tid = stream->codecpar->codec_id;"
        new = """\t/* Attached pictures belong to metadata, not the playback clock. */
\tif (type == AVMEDIA_TYPE_VIDEO &&
\t    (m->fmt->streams[ret]->disposition & AV_DISPOSITION_ATTACHED_PIC)) {
\t\tret = AVERROR_STREAM_NOT_FOUND;
\t\tfor (unsigned int i = 0; i < m->fmt->nb_streams; i++) {
\t\t\tAVStream *candidate = m->fmt->streams[i];
\t\t\tif (candidate->codecpar->codec_type != type ||
\t\t\t    (candidate->disposition & AV_DISPOSITION_ATTACHED_PIC))
\t\t\t\tcontinue;
\t\t\tret = av_find_best_stream(m->fmt, type, (int)i, -1, NULL, 0);
\t\t\tif (ret >= 0)
\t\t\t\tbreak;
\t\t}
\t\tif (ret < 0)
\t\t\treturn false;
\t}
\tstream = d->stream = m->fmt->streams[ret];
\tid = stream->codecpar->codec_id;"""
        return _replace_once(source, old, new, patch="attached-picture video selection")
    if name == f"{_PLAYBACK}/media.c":
        source = _patch_terminal_callbacks(source, "media", "m")
        source = _replace_once(
            source,
            "\tint ret = av_read_frame(media->fmt, pkt);\n\tif (ret < 0) {",
            """\tint ret = av_read_frame(media->fmt, pkt);
\t/* Demuxers can mask an exhausted transport as EOF (for example WAV). */
\tif (ret == AVERROR_EOF && media->fmt->pb && media->fmt->pb->error < 0 &&
\t    media->fmt->pb->error != AVERROR_EOF)
\t\tret = media->fmt->pb->error;
\tif (ret == AVERROR_EOF && media->terminal_packet_corrupt)
\t\tret = AVERROR_INVALIDDATA;
\tif (ret >= 0)
\t\tmedia->terminal_packet_corrupt = (pkt->flags & AV_PKT_FLAG_CORRUPT) != 0;
\tif (ret < 0) {""",
            patch="demux EOF retains terminal I/O error",
        )
        source = _replace_once(
            source,
            "\t\t\tif (ret == AVERROR_EOF || ret == AVERROR_EXIT) {",
            "\t\t\tif (ret == AVERROR_EOF) {",
            patch="interrupted read is not natural EOF",
        )
        source = _replace_once(
            source,
            "\tAVStream *stream = m->fmt->streams[0];",
            "\tAVStream *stream = m->has_video ? m->v.stream : m->a.stream;",
            patch="primary playback stream for seek",
        )
        old = """\t\tint ret = av_seek_frame(m->fmt, 0, seek_target, seek_flags);
\t\tif (ret < 0) {
\t\t\tblog(LOG_WARNING, "MP: Failed to seek: %s", av_err2str(ret));
\t\t}
"""
        new = """\t\tint ret = av_seek_frame(m->fmt, stream->index, seek_target, seek_flags);
\t\tif (ret < 0) {
\t\t\tblog(LOG_WARNING, "MP: Failed to seek: %s", av_err2str(ret));
\t\t\tm->seek_next_ts = false;
\t\t\treturn;
\t\t}
\t\t/* A seek can recover after the demuxer has read ahead to EOF. */
\t\tm->eof = false;
\t\tm->terminal_packet_corrupt = false;
\t\tpthread_mutex_lock(&m->mutex);
\t\tm->completed = false;
\t\tpthread_mutex_unlock(&m->mutex);
"""
        source = _replace_once(source, old, new, patch="seek demux EOF reset")
        source = _replace_once(
            source,
            """\t\tbool looping;

\t\tpthread_mutex_lock(&m->mutex);
\t\tlooping = m->looping;
\t\tif (!looping) {
\t\t\tm->active = false;
\t\t\tm->stopping = true;
\t\t}
\t\tpthread_mutex_unlock(&m->mutex);

\t\tmp_media_reset(m);
""",
            """\t\tbool looping, notify;

\t\tpthread_mutex_lock(&m->mutex);
\t\tlooping = m->looping;
\t\tnotify = !looping && !m->completed;
\t\tif (!looping) {
\t\t\tm->active = false;
\t\t\tm->completed = true;
\t\t}
\t\tpthread_mutex_unlock(&m->mutex);

\t\t/* Drained playback is complete; optional rewind must not veto it. */
\t\tif (looping)
\t\t\tmp_media_reset(m);
\t\telse if (notify && m->stop_cb)
\t\t\tm->stop_cb(m->opaque, false);
""",
            patch="completion defers optional rewind",
        )
        source = _replace_once(
            source,
            "\tif (m->active)\n\t\tm->reset = true;\n\n\tm->looping = loop;",
            "\tif (m->active || m->completed)\n\t\tm->reset = true;\n"
            "\tif (m->completed)\n\t\tm->reset_ts = true;\n"
            "\tm->completed = false;\n\n\tm->looping = loop;",
            patch="explicit play resets completed decoder",
        )
        source = _replace_once(
            source,
            "\t\tif (reset) {\n\t\t\tmp_media_reset(m);\n\t\t\tcontinue;\n\t\t}",
            "\t\tif (reset) {\n\t\t\tmp_media_reset(m);\n"
            "\t\t\t/* Reactivation needs a fresh clock after the deferred rewind. */\n"
            "\t\t\tif (reset_time)\n\t\t\t\treset_ts(m);\n"
            "\t\t\tcontinue;\n\t\t}",
            patch="reactivation rebases the playback clock",
        )
        old = """\tif (!m->is_local_file) {
\t\tav_dict_set(&opts, "stimeout", "30000000", 0);
\t\tm->fmt->interrupt_callback.callback = interrupt_callback;
\t\tm->fmt->interrupt_callback.opaque = m;
\t}
"""
        new = """\t/* Finite URL inputs also need cancellation during FFmpeg retries. */
\tm->fmt->interrupt_callback.callback = interrupt_callback;
\tm->fmt->interrupt_callback.opaque = m;
\tif (!m->is_local_file) {
\t\tav_dict_set(&opts, "stimeout", "30000000", 0);
\t}
"""
        return _replace_once(source, old, new, patch="finite input cancellation")
    return source


def download_sources(cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"obs-studio-{OBS_VERSION}.tar.gz"
    if archive.is_symlink():
        raise MediaPlaybackBuildError("OBS source archive must not be a symbolic link")
    if not archive.exists():
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as output:
            temporary = Path(output.name)
            try:
                with urllib.request.urlopen(SOURCE_URL, timeout=120) as response:
                    shutil.copyfileobj(response, output)
            except BaseException:  # noqa: BLE001 - remove partial downloads even on cancellation
                output.close()
                temporary.unlink(missing_ok=True)
                raise
        try:
            _verify_archive(temporary)
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    _verify_archive(archive)
    return archive


def _verify_archive(archive: Path) -> None:
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != SOURCE_SHA256:
        raise MediaPlaybackBuildError(f"OBS source archive failed SHA-256 verification: {archive}")


def prepare_sources(cache: Path) -> Path:
    archive = download_sources(cache)
    destination = cache.resolve() / f"obs-{OBS_VERSION}"
    if destination.is_symlink():
        raise MediaPlaybackBuildError("OBS source destination must not be a symbolic link")
    with tempfile.TemporaryDirectory(prefix=".obs-", dir=cache) as directory:
        staging = Path(directory) / "sources"
        staging.mkdir()
        with tarfile.open(archive, "r:gz") as bundle:
            prefix = f"obs-studio-{OBS_VERSION}/"
            for member in bundle:
                if not member.name.startswith(prefix):
                    continue
                name = member.name.removeprefix(prefix)
                selected = (
                    (name.startswith("libobs/") and name.endswith(".h"))
                    or (name.startswith(f"{_PLAYBACK}/") and name.endswith((".c", ".h")))
                    or (name.startswith("deps/w32-pthreads/") and name.endswith(".h"))
                    or name
                    in {
                        "plugins/obs-ffmpeg/obs-ffmpeg-source.c",
                        "plugins/obs-ffmpeg/obs-ffmpeg-compat.h",
                        "plugins/obs-ffmpeg/obs-ffmpeg-formats.h",
                        "plugins/obs-ffmpeg/data/locale/en-US.ini",
                        "COPYING",
                    }
                )
                if not selected:
                    continue
                if not member.isfile() or ".." in PurePosixPath(name).parts:
                    raise MediaPlaybackBuildError("OBS archive contains an invalid source member")
                source = bundle.extractfile(member)
                if source is None:
                    raise MediaPlaybackBuildError(f"OBS archive is missing source bytes: {name}")
                path = staging / name
                path.parent.mkdir(parents=True, exist_ok=True)
                text = source.read().decode("utf-8")
                path.write_text(patch_source(name, text), encoding="utf-8", newline="\n")
        for required in (
            "plugins/obs-ffmpeg/obs-ffmpeg-source.c",
            f"{_PLAYBACK}/media.c",
            f"{_PLAYBACK}/decode.c",
            f"{_PLAYBACK}/media-playback.c",
            f"{_PLAYBACK}/cache.c",
            "libobs/obs.h",
            "COPYING",
        ):
            if not (staging / required).is_file():
                raise MediaPlaybackBuildError(f"OBS archive is missing required source: {required}")
        _publish_runtime(staging, destination)
    return destination


def prepare_playback_sdk(platform: str, architecture: str, cache: Path) -> Path:
    """Reuse SDK headers only after checking them against the verified archive."""
    sdk = cache.resolve() / f"sdk-{platform}-{architecture}"
    if platform != "linux" and sdk.is_dir() and not sdk.is_symlink():
        archive = _download_sdk(SDK_SPECS[platform, architecture], cache)
        prefixes = ("include/libav", "include/libswscale/", "include/simde/")
        checked = 0
        matches = True
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as bundle:
                for member in bundle.infolist():
                    if member.is_dir() or not member.filename.startswith(prefixes):
                        continue
                    path = sdk / member.filename
                    if (
                        not path.is_file()
                        or hashlib.sha256(path.read_bytes()).digest()
                        != hashlib.sha256(bundle.read(member)).digest()
                    ):
                        matches = False
                        break
                    checked += 1
        else:
            with tarfile.open(archive, "r:xz") as bundle:
                for member in bundle:
                    if not member.isfile() or not member.name.startswith(prefixes):
                        continue
                    source = bundle.extractfile(member)
                    path = sdk / member.name
                    if (
                        source is None
                        or not path.is_file()
                        or hashlib.sha256(path.read_bytes()).digest()
                        != hashlib.sha256(source.read()).digest()
                    ):
                        matches = False
                        break
                    checked += 1
        if matches and checked:
            return sdk
    return prepare_sdk(platform, architecture, cache)


_VERIFY_MODULE = """
import sys, tempfile
from pylibobs.context import OBSContext
from pylibobs.source import Source
from pylibobs._ffi import ffi, get_lib
with tempfile.TemporaryDirectory(prefix='solin-playback-qualification-') as config:
    with OBSContext(module_config_path=config) as context:
        context.set_audio(samples_per_sec=48000)
        library = get_lib()
        module = ffi.new('obs_module_t **')
        result = library.obs_open_module(module, sys.argv[1].encode(), sys.argv[2].encode())
        if result != 0 or not library.obs_init_module(module[0]):
            raise RuntimeError('Solin media module could not be initialized')
        source = Source.create('solin_media_source', 'solin-playback-qualification',
                               {'is_local_file': True, 'local_file': ''})
        if source.id != 'solin_media_source':
            raise RuntimeError('Solin media source was not registered')
        source.release()
print('Solin media source qualified')
"""


def verify_module(binary: Path, data: Path) -> None:
    environment = os.environ.copy()
    for name in ("LIBOBS_PATH", "OBS_DATA_PATH", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES"):
        environment.pop(name, None)
    result = subprocess.run(
        [sys.executable, "-B", "-c", _VERIFY_MODULE, str(binary.resolve()), str(data.resolve())],
        env=environment,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=45,
    )
    if result.returncode != 0 or "Solin media source qualified" not in result.stdout:
        raise MediaPlaybackBuildError(
            f"Playback module qualification failed ({result.returncode}):\n"
            f"{result.stdout[-4000:]}\n{result.stderr[-4000:]}"
        )


def install_module(binary: Path, sources: Path, runtime: Path, platform: str) -> Path:
    plugins = runtime / "obs-plugins"
    plugins.mkdir(exist_ok=True)
    destination = plugins / f"{MODULE_NAME}{SUFFIXES[platform]}"
    data = runtime / "data" / "obs-plugins" / MODULE_NAME
    if destination.is_symlink() or data.is_symlink():
        raise MediaPlaybackBuildError("Owned module destinations must not be symbolic links")
    with tempfile.TemporaryDirectory(prefix=".solin-media-", dir=runtime) as directory:
        staging = Path(directory)
        candidate = staging / destination.name
        shutil.copy2(binary, candidate)
        candidate_data = staging / "data"
        (candidate_data / "locale").mkdir(parents=True)
        shutil.copy2(
            sources / "plugins/obs-ffmpeg/data/locale/en-US.ini",
            candidate_data / "locale/en-US.ini",
        )
        shutil.copy2(sources / "COPYING", candidate_data / "COPYING")
        if platform == "macos":
            _relocate_macos(runtime, binaries=[candidate])
        verify_module(candidate, candidate_data)
        _publish_runtime(candidate_data, data)
        candidate_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
        installed_hash = (
            hashlib.sha256(destination.read_bytes()).hexdigest() if destination.is_file() else None
        )
        # Windows keeps loaded modules locked. An identical qualified artifact
        # needs no replacement and can be rebuilt while the application is open.
        if installed_hash != candidate_hash:
            _retry_windows_sharing(lambda: os.replace(candidate, destination))
        receipt = {
            "schema": 1,
            "source_id": SOURCE_ID,
            "obs_version": OBS_VERSION,
            "source_sha256": SOURCE_SHA256,
            "plugin": {
                "path": destination.relative_to(runtime).as_posix(),
                "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            },
            "patches": [
                "attached-picture-video-selection",
                "seek-demux-eof-reset",
                "rejected-seek-queue-preservation",
                "primary-timed-stream-seek",
                "finite-input-cancellation",
                "terminal-error-state",
                "completion-defers-rewind",
                "reactivation-playback-clock",
            ],
        }
        manifest = staging / MANIFEST_NAME
        manifest.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        _retry_windows_sharing(lambda: os.replace(manifest, runtime / MANIFEST_NAME))
    return destination


def build_media_playback(
    *, build_directory: Path, cache_directory: Path, configuration: str = "Release", jobs: int = 0
) -> Path:
    if jobs < 0:
        raise MediaPlaybackBuildError("--jobs must be greater than or equal to zero")
    platform, architecture = target()
    runtime = installed_runtime(platform, architecture)
    sources = prepare_sources(cache_directory.resolve())
    sdk = prepare_playback_sdk(platform, architecture, cache_directory.resolve() / "dependencies")
    cmake = shutil.which("cmake")
    if cmake is None:
        raise MediaPlaybackBuildError("CMake 3.25 or newer and a native C toolchain are required")
    build_directory = build_directory.resolve()
    command = [
        cmake,
        "-S",
        str(SOURCE_DIRECTORY),
        "-B",
        str(build_directory),
        f"-DSOLIN_OBS_ROOT={sources}",
        f"-DSOLIN_OBS_RUNTIME={runtime}",
        f"-DSOLIN_FFMPEG_ROOT={sdk}",
    ]
    if platform == "windows":
        command.extend(["-A", "x64"])
    else:
        command.append(f"-DCMAKE_BUILD_TYPE={configuration}")
    if platform == "macos":
        command.extend(
            [f"-DCMAKE_OSX_ARCHITECTURES={architecture}", "-DCMAKE_OSX_DEPLOYMENT_TARGET=13.0"]
        )
    _run(command)
    command = [cmake, "--build", str(build_directory), "--config", configuration, "--parallel"]
    if jobs:
        command.append(str(jobs))
    _run(command)
    ctest = str(Path(cmake).with_name("ctest.exe" if platform == "windows" else "ctest"))
    _run(
        [
            ctest,
            "--test-dir",
            str(build_directory),
            "--build-config",
            configuration,
            "--output-on-failure",
        ]
    )
    filename = f"{MODULE_NAME}{SUFFIXES[platform]}"
    binary = next(
        (
            p
            for p in (build_directory / configuration / filename, build_directory / filename)
            if p.is_file()
        ),
        None,
    )
    if binary is None:
        raise MediaPlaybackBuildError("CMake did not produce the playback module")
    return install_module(binary, sources, runtime, platform)


def _run(command: list[str]) -> None:
    print("+", subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, default=ROOT / "build/native/media-playback")
    parser.add_argument(
        "--cache-dir", type=Path, default=ROOT / "build/dependencies/media-playback"
    )
    parser.add_argument(
        "--configuration", choices=("Release", "RelWithDebInfo", "Debug"), default="Release"
    )
    parser.add_argument("--jobs", type=int, default=0)
    options = parser.parse_args(arguments)
    try:
        result = build_media_playback(
            build_directory=options.build_dir,
            cache_directory=options.cache_dir,
            configuration=options.configuration,
            jobs=options.jobs,
        )
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
        tarfile.TarError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Solin media playback module installed and qualified: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
