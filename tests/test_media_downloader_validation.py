from __future__ import annotations

import pytest

from app.core.media.downloader import IncompleteDownloadError, _validate_download_size


def test_download_size_validation_accepts_complete_or_unknown_length():
    _validate_download_size(1024, 1024)
    _validate_download_size(1024, 0)


def test_download_size_validation_rejects_truncated_response():
    with pytest.raises(IncompleteDownloadError, match="received 512 of 1024 bytes"):
        _validate_download_size(512, 1024)
