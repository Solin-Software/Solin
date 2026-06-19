from __future__ import annotations

from pathlib import Path

from solin.core.rendering import pdf as pdf_module


class _PageSize:
    def width(self) -> float:
        return 72.0

    def height(self) -> float:
        return 72.0


class _FakeImage:
    def __init__(self, calls: list[tuple[str, object, int]]) -> None:
        self._calls = calls

    def isNull(self) -> bool:  # noqa: N802 - Qt-style test double
        return False

    def save(self, path: str, image_format, quality: int) -> bool:
        self._calls.append((path, image_format, quality))
        Path(path).write_bytes(b"page")
        return True


class _FakePdfDocument:
    class Error:
        None_ = object()

    save_calls: list[tuple[str, object, int]] = []

    def load(self, _path: str):
        return self.Error.None_

    def pageCount(self) -> int:  # noqa: N802 - Qt-style test double
        return 1

    def pagePointSize(self, _page_index: int):  # noqa: N802 - Qt-style test double
        return _PageSize()

    def render(self, _page_index: int, _size):
        return _FakeImage(self.save_calls)

    def close(self) -> None:
        pass


def test_render_pdf_pages_passes_image_format_as_string(monkeypatch, tmp_path):
    pdf_path = tmp_path / "slides.pdf"
    pdf_path.write_bytes(b"%PDF fake")
    output_dir = tmp_path / "pages"
    _FakePdfDocument.save_calls = []

    monkeypatch.setattr(pdf_module, "QPdfDocument", _FakePdfDocument)
    monkeypatch.setattr(pdf_module, "_flatten_to_rgb", lambda image: image)

    pages = pdf_module.render_pdf_pages_sync(pdf_path, output_dir)

    assert pages == [str(output_dir / "page_001.jpg")]
    assert _FakePdfDocument.save_calls == [
        (str(output_dir / "page_001.jpg"), "JPEG", 85)
    ]
