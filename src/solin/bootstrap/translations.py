"""Prepare source-checkout catalogs at the application's composition boundary."""

from pathlib import Path

from solin.core.foundation.constants import IS_DEV
from solin.core.foundation.resources import application_translation_root, package_root


def prepare_application_catalogs() -> Path:
    """Installed applications use build outputs; source checkouts prepare their own."""
    resources = application_translation_root()
    if IS_DEV:
        from solin.core.i18n.compiler import compile_catalogs, development_catalog_directory

        output = development_catalog_directory(package_root().parents[1])
        compile_catalogs(resources, output)
        return output
    return resources
