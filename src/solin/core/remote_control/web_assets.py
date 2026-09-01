from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Final, Protocol


REMOTE_SHELL_ASSET_ROUTE: Final = "_assets"

WEB_ASSET_CONTENT_TYPES: Final = {
    ".css": "text/css",
    ".html": "text/html",
    ".ico": "image/x-icon",
    ".js": "text/javascript",
    ".json": "application/json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".webmanifest": "application/manifest+json",
}

_INDEX_NAME: Final = "index.html"
_SERVICE_WORKER_NAME: Final = "service-worker.js"
_DOCUMENTATION_NAME: Final = "README.md"
_DYNAMIC_INDEX_ASSETS: Final = frozenset({"trust-certificate.cer"})
_SHELL_REVISION_DOMAIN: Final = b"solin-remote-shell-revision-v1\0"
_SHELL_REVISION_PLACEHOLDER: Final = "__SOLIN_REMOTE_SHELL_REVISION__"
_SHELL_RESOURCES_PLACEHOLDER: Final = "__SOLIN_REMOTE_SHELL_RESOURCES__"
_INDEX_ASSET_REFERENCE: Final = re.compile(
    r'(?P<prefix>\b(?:href|src)=")\./(?P<path>[^"?#]+)(?P<suffix>[^"]*")'
)


class _HashWriter(Protocol):
    def update(self, data: bytes, /) -> None: ...


class RemoteControlWebAssets:
    """Immutable, installation-scoped view of the packaged remote shell."""

    __slots__ = (
        "_asset_relative_path_set",
        "_index_html",
        "_revision",
        "_root",
        "_service_worker",
        "_versioned_route_prefix",
    )

    def __init__(
        self,
        root: Path,
        *,
        installation_id: str,
        url_prefix: str,
    ) -> None:
        resolved_root = Path(root).resolve()
        if not resolved_root.is_dir():
            raise FileNotFoundError(
                f"Remote-control asset directory does not exist: {resolved_root}"
            )

        normalized_installation_id = str(installation_id or "").strip()
        if not normalized_installation_id or len(normalized_installation_id) > 256:
            raise ValueError("Remote-control installation ID must contain 1 to 256 characters")

        normalized_url_prefix = "/" + str(url_prefix or "").strip("/")
        if normalized_url_prefix == "/":
            raise ValueError("Remote-control URL prefix cannot be empty")

        index_template = self._read_required(resolved_root / _INDEX_NAME)
        worker_template = self._read_required(resolved_root / _SERVICE_WORKER_NAME)
        relative_paths = self._discover_shell_assets(resolved_root)
        revision = self._calculate_revision(
            installation_id=normalized_installation_id,
            url_prefix=normalized_url_prefix,
            templates=((_INDEX_NAME, index_template), (_SERVICE_WORKER_NAME, worker_template)),
            root=resolved_root,
            relative_paths=relative_paths,
        )
        route_prefix = f"{REMOTE_SHELL_ASSET_ROUTE}/{revision}/"
        url_prefix_with_revision = f"{normalized_url_prefix}/{REMOTE_SHELL_ASSET_ROUTE}/{revision}"

        self._root = resolved_root
        self._asset_relative_path_set = frozenset(relative_paths)
        self._revision = revision
        self._versioned_route_prefix = route_prefix
        self._index_html = self._render_index(index_template, url_prefix_with_revision)
        self._service_worker = self._render_service_worker(
            worker_template, revision, relative_paths
        )

    @property
    def revision(self) -> str:
        return self._revision

    @property
    def index_html(self) -> bytes:
        return self._index_html

    @property
    def service_worker(self) -> bytes:
        return self._service_worker

    def resolve_versioned(self, requested: str) -> Path | None:
        normalized = str(requested or "").strip("/")
        if not normalized.startswith(self._versioned_route_prefix):
            return None
        relative = normalized.removeprefix(self._versioned_route_prefix)
        if relative not in self._asset_relative_path_set:
            return None
        candidate = (self._root / Path(*relative.split("/"))).resolve()
        if self._root not in candidate.parents or not candidate.is_file():
            return None
        return candidate

    @staticmethod
    def _read_required(path: Path) -> bytes:
        try:
            return path.read_bytes()
        except OSError as error:
            raise FileNotFoundError(
                f"Required remote-control asset is unavailable: {path}"
            ) from error

    @staticmethod
    def _discover_shell_assets(root: Path) -> tuple[str, ...]:
        excluded = {_DOCUMENTATION_NAME, _INDEX_NAME, _SERVICE_WORKER_NAME}
        return tuple(
            sorted(
                path.relative_to(root).as_posix()
                for path in root.rglob("*")
                if path.is_file()
                and path.name not in excluded
                and path.suffix.lower() in WEB_ASSET_CONTENT_TYPES
            )
        )

    @staticmethod
    def _calculate_revision(
        *,
        installation_id: str,
        url_prefix: str,
        templates: tuple[tuple[str, bytes], ...],
        root: Path,
        relative_paths: tuple[str, ...],
    ) -> str:
        digest = hashlib.sha256()
        digest.update(_SHELL_REVISION_DOMAIN)
        RemoteControlWebAssets._hash_value(digest, "installation", installation_id.encode("utf-8"))
        RemoteControlWebAssets._hash_value(digest, "url-prefix", url_prefix.encode("utf-8"))
        for relative, data in templates:
            RemoteControlWebAssets._hash_value(digest, relative, data)
        for relative in relative_paths:
            RemoteControlWebAssets._hash_value(digest, relative, (root / relative).read_bytes())
        return digest.hexdigest()[:32]

    @staticmethod
    def _hash_value(digest: _HashWriter, name: str, data: bytes) -> None:
        encoded_name = name.encode("utf-8")
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)

    def _render_index(self, template: bytes, versioned_url_prefix: str) -> bytes:
        try:
            source = template.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("Remote-control index must be valid UTF-8") from error

        def replace_reference(match: re.Match[str]) -> str:
            relative = match.group("path")
            if relative in _DYNAMIC_INDEX_ASSETS:
                return match.group(0)
            if relative not in self._asset_relative_path_set:
                raise ValueError(f"Remote-control index references an unknown asset: {relative}")
            return (
                f"{match.group('prefix')}{versioned_url_prefix}/{relative}{match.group('suffix')}"
            )

        return _INDEX_ASSET_REFERENCE.sub(replace_reference, source).encode("utf-8")

    @staticmethod
    def _render_service_worker(
        template: bytes,
        revision: str,
        relative_paths: tuple[str, ...],
    ) -> bytes:
        try:
            source = template.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("Remote-control service worker must be valid UTF-8") from error
        for placeholder in (_SHELL_REVISION_PLACEHOLDER, _SHELL_RESOURCES_PLACEHOLDER):
            if source.count(placeholder) != 1:
                raise ValueError(
                    f"Remote-control service worker must contain exactly one {placeholder} placeholder"
                )

        resources = [
            "./",
            "./index.html",
            *(f"./{REMOTE_SHELL_ASSET_ROUTE}/{revision}/{path}" for path in relative_paths),
        ]
        rendered = source.replace(_SHELL_REVISION_PLACEHOLDER, revision).replace(
            _SHELL_RESOURCES_PLACEHOLDER,
            json.dumps(resources, ensure_ascii=True, indent=2),
        )
        return rendered.encode("utf-8")
