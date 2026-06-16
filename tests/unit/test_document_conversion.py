from pathlib import Path

from solin.core.rendering import document_conversion


class _WorkerStub:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


def test_document_conversion_service_binds_cache_roots(monkeypatch):
    pdf_cache_calls = []
    office_cache_calls = []
    availability_calls = []

    monkeypatch.setattr(
        document_conversion,
        "cached_pdf_pages",
        lambda path, cache_dir: pdf_cache_calls.append((path, cache_dir)) or ["pdf"],
    )
    monkeypatch.setattr(
        document_conversion,
        "cached_office_pages",
        lambda path, **kwargs: office_cache_calls.append((path, kwargs)) or ["office"],
    )
    monkeypatch.setattr(
        document_conversion,
        "libreoffice_available",
        lambda: availability_calls.append(True) or True,
    )

    service = document_conversion.DocumentConversionService(
        pdf_pages_dir="cache/pdf",
        pptx_pages_dir="cache/pptx",
        docx_pages_dir="cache/docx",
    )

    assert service.cached_pdf_pages("document.pdf") == ["pdf"]
    assert service.cached_office_pages("slides.pptx") == ["office"]
    assert service.office_conversion_available() is True
    assert pdf_cache_calls == [("document.pdf", Path("cache/pdf"))]
    assert office_cache_calls == [
        (
            "slides.pptx",
            {
                "pptx_pages_dir": Path("cache/pptx"),
                "docx_pages_dir": Path("cache/docx"),
            },
        )
    ]
    assert availability_calls == [True]


def test_document_conversion_service_creates_bound_workers(monkeypatch):
    monkeypatch.setattr(document_conversion, "PdfConvertThread", _WorkerStub)
    monkeypatch.setattr(document_conversion, "LoConvertThread", _WorkerStub)
    owner = object()
    service = document_conversion.DocumentConversionService(
        pdf_pages_dir="cache/pdf",
        pptx_pages_dir="cache/pptx",
        docx_pages_dir="cache/docx",
    )

    pdf_worker = service.create_pdf_thread("document.pdf", parent=owner)
    office_worker = service.create_office_thread("slides.pptx", parent=owner)

    assert pdf_worker.args == ("document.pdf", Path("cache/pdf"))
    assert pdf_worker.kwargs == {"parent": owner}
    assert office_worker.args == ("slides.pptx",)
    assert office_worker.kwargs == {
        "pptx_pages_dir": Path("cache/pptx"),
        "docx_pages_dir": Path("cache/docx"),
        "pdf_pages_dir": Path("cache/pdf"),
        "parent": owner,
    }
