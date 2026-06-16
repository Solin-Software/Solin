from __future__ import annotations

from solin.core.jw import clip_fetch


def test_clip_fetch_thread_emits_generation_aware_results(monkeypatch):
    monkeypatch.setattr(
        clip_fetch,
        "fetch_clips",
        lambda api_code, force, **kwargs: (
            [
                {
                    "api_code": api_code,
                    "force": force,
                    "fallback_code": kwargs["fallback_code"],
                }
            ],
            123.5,
            True,
        ),
    )
    ready = []
    failed = []
    thread = clip_fetch.ClipFetchThreadFactory().create(
        7,
        api_code="T",
        fallback_code="E",
        is_sign_language=False,
        force=True,
        cache_dir="cache/jw",
    )
    thread.items_ready.connect(
        lambda generation, items, fetched_at, from_cache: ready.append(
            (generation, items, fetched_at, from_cache)
        )
    )
    thread.failed.connect(lambda generation, error: failed.append((generation, error)))

    thread.run()

    assert ready == [
        (
            7,
            [{"api_code": "T", "force": True, "fallback_code": "E"}],
            123.5,
            True,
        )
    ]
    assert failed == []


def test_clip_fetch_thread_reports_transport_failures(monkeypatch):
    def fail_fetch(*_args, **_kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(clip_fetch, "fetch_clips", fail_fetch)
    failed = []
    thread = clip_fetch.ClipFetchThreadFactory().create(
        3,
        api_code="T",
        fallback_code="E",
        is_sign_language=False,
        force=False,
        cache_dir="cache/jw",
    )
    thread.failed.connect(lambda generation, error: failed.append((generation, error)))

    thread.run()

    assert failed == [(3, "offline")]
