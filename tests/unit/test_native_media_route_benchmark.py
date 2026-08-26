from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest

from scripts import benchmark_native_media_route as benchmark


def _fixture_manifest(path: Path) -> dict:
    return {
        "schema": benchmark.FIXTURE_SCHEMA,
        "generator": {
            "gstreamer_version": "1.28.5",
            "pinned_installer_sha256": "a" * 64,
            "gst_launch_sha256": "b" * 64,
            "pipeline": ["videotestsrc"],
        },
        "asset": {
            "path": str(path),
            "sha256": benchmark._sha256(path),
            "bytes": path.stat().st_size,
            "container": "mp4",
            "video_codec": "H.264 (OpenH264)",
            "width": 1280,
            "height": 720,
            "frames_per_second": 30,
            "duration_seconds": 30,
            "frame_count": 900,
            "audio": False,
        },
    }


def _configuration(path: Path) -> dict:
    return {
        "schema": benchmark.CONFIGURATION_SCHEMA,
        "route": benchmark.CURRENT_ROUTE,
        "fixture": _fixture_manifest(path),
        "playback": {
            "adapter": "QtMultimedia",
            "decode_path": "automatic",
            "content_ingress_transport": "d3d11_shared_texture",
            "loop_fixture": False,
        },
        "outputs": {
            "native_presenters": 2,
            "editor_preview": False,
            "virtual_camera": True,
        },
        "virtual_camera_consumer": {
            "application": "OBS Studio",
            "architecture": "x64",
            "profile": "NV12 1920x1080 30 fps",
        },
        "measurement_notes": [],
    }


def test_percentiles_interpolate_and_report_p99() -> None:
    values = [0.0, 10.0, 20.0, 30.0, 40.0]

    assert benchmark._percentile(values, 0.50) == 20.0
    assert benchmark._percentile(values, 0.95) == pytest.approx(38.0)
    assert benchmark._percentile(values, 0.99) == pytest.approx(39.6)
    assert benchmark._summary(values) == {
        "samples": 5,
        "p50": 20.0,
        "p95": 38.0,
        "p99": 39.6,
        "minimum": 0.0,
        "maximum": 40.0,
    }


def test_collect_buckets_uses_cumulative_cpu_deltas_and_host_normalization(
    monkeypatch,
) -> None:
    targets = (
        benchmark.ProcessTarget("solin", 10),
        benchmark.ProcessTarget("media_engine", 20),
    )
    snapshots = {
        10: iter(
            (
                benchmark.ProcessSnapshot(1.0, 100, "solin.exe"),
                benchmark.ProcessSnapshot(1.4, 110, "solin.exe"),
                benchmark.ProcessSnapshot(1.6, 120, "solin.exe"),
            )
        ),
        20: iter(
            (
                benchmark.ProcessSnapshot(2.0, 200, "solin-media-engine.exe"),
                benchmark.ProcessSnapshot(2.2, 210, "solin-media-engine.exe"),
                benchmark.ProcessSnapshot(2.5, 220, "solin-media-engine.exe"),
            )
        ),
    }
    now = [0.0]

    def read(pid: int) -> benchmark.ProcessSnapshot:
        return next(snapshots[pid])

    def sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr(benchmark.os, "cpu_count", lambda: 4)
    buckets = benchmark.collect_buckets(
        targets,
        bucket_count=2,
        snapshot_reader=read,
        sleep=sleep,
        monotonic=lambda: now[0],
    )

    assert len(buckets) == 4
    assert [bucket.index for bucket in buckets] == [0, 0, 1, 1]
    assert buckets[0].cpu_one_core_percent == pytest.approx(40.0)
    assert buckets[0].cpu_host_percent == pytest.approx(10.0)
    assert buckets[3].cpu_one_core_percent == pytest.approx(30.0)
    assert buckets[3].cpu_host_percent == pytest.approx(7.5)
    assert buckets[3].resident_bytes == 220


def test_configuration_records_and_verifies_the_fixture(tmp_path: Path) -> None:
    asset = tmp_path / "fixture.mp4"
    asset.write_bytes(b"deterministic-fixture")
    manifest_path = tmp_path / "fixture.json"
    benchmark._write_json(manifest_path, _fixture_manifest(asset))
    configuration_path = tmp_path / "configuration.json"

    configuration = benchmark.write_configuration(
        fixture_manifest_path=manifest_path,
        output=configuration_path,
        native_presenters=2,
        editor_preview=False,
        virtual_camera=True,
        consumer_application="OBS Studio",
        consumer_architecture="x64",
        consumer_profile="NV12 1920x1080 30 fps",
        decode_path="hardware",
        ingress_transport="d3d11_shared_texture",
    )

    assert configuration["fixture"]["asset"]["sha256"] == benchmark._sha256(asset)
    assert configuration["outputs"] == {
        "native_presenters": 2,
        "editor_preview": False,
        "virtual_camera": True,
    }
    assert benchmark._configuration_with_verified_fixture(configuration_path) == configuration

    asset.write_bytes(b"changed")
    with pytest.raises(ValueError, match="SHA-256"):
        benchmark._configuration_with_verified_fixture(configuration_path)


def test_virtual_camera_configuration_rejects_placeholder_consumer(tmp_path: Path) -> None:
    asset = tmp_path / "fixture.mp4"
    asset.write_bytes(b"fixture")
    manifest_path = tmp_path / "fixture.json"
    benchmark._write_json(manifest_path, _fixture_manifest(asset))

    with pytest.raises(ValueError, match="consumer application"):
        benchmark.write_configuration(
            fixture_manifest_path=manifest_path,
            output=tmp_path / "configuration.json",
            native_presenters=1,
            editor_preview=True,
            virtual_camera=True,
            consumer_application="replace-me",
            consumer_architecture="x64",
            consumer_profile="NV12 1920x1080 30 fps",
            decode_path="automatic",
            ingress_transport="d3d11_shared_texture",
        )


def test_build_report_keeps_raw_buckets_and_process_percentiles(tmp_path: Path) -> None:
    asset = tmp_path / "fixture.mp4"
    asset.write_bytes(b"fixture")
    configuration = _configuration(asset)
    targets = (
        benchmark.ProcessTarget("solin", 10),
        benchmark.ProcessTarget("media_engine", 20),
    )
    buckets = [
        benchmark.ProcessBucket(
            role=role,
            pid=pid,
            index=index,
            elapsed_seconds=1.0,
            cpu_seconds=cpu / 100.0,
            cpu_one_core_percent=cpu,
            cpu_host_percent=cpu / 4.0,
            resident_bytes=(100 + index) * 1024 * 1024,
        )
        for index, cpu in enumerate((4.0, 8.0, 12.0, 16.0))
        for role, pid in (("solin", 10), ("media_engine", 20))
    ]

    report = benchmark.build_report(
        configuration=configuration,
        configuration_sha256="c" * 64,
        targets=targets,
        initial_snapshots={
            "solin": benchmark.ProcessSnapshot(0.0, 0, "solin.exe"),
            "media_engine": benchmark.ProcessSnapshot(0.0, 0, "solin-media-engine.exe"),
        },
        buckets=buckets,
        warmup_seconds=10,
        source={"commit": "d" * 40, "branch": "feature", "dirty": False},
        hardware={
            "cpu": {"name": "Fixture CPU", "logical_processors": 4},
            "gpu": {"status": "collected", "controllers": []},
        },
    )

    assert report["schema"] == benchmark.REPORT_SCHEMA
    assert report["source"]["commit"] == "d" * 40
    assert report["hardware"]["cpu"]["name"] == "Fixture CPU"
    assert report["sampling"]["bucket_target_seconds"] == 1.0
    assert report["sampling"]["measurement_buckets"] == 4
    assert report["processes"]["solin"]["cpu_host_percent"]["p99"] > 0
    assert len(report["processes"]["solin"]["buckets"]) == 4
    assert report["aggregate_sampled_processes"]["cpu_host_percent"]["p50"] == 5.0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process sampler")
def test_windows_sampler_reads_the_current_process() -> None:
    before = benchmark.read_process_snapshot(os.getpid())
    sum(index * index for index in range(10_000))
    after = benchmark.read_process_snapshot(os.getpid())

    assert after.cpu_seconds >= before.cpu_seconds
    assert after.resident_bytes is not None and after.resident_bytes > 0
    assert after.executable.casefold().startswith("python")
