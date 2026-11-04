"""Owned publication browsing, lazy previews, and ordered import submission."""

from __future__ import annotations

import base64
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from contextlib import contextmanager
import io
import logging
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4

from PIL import Image, UnidentifiedImageError
from PySide6.QtCore import QObject, Property, QTimer, QUrl, Signal, Slot
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox, QVBoxLayout, QWidget

from solin.core.jw.jwpub_import import (
    JwpubImportCancelled,
    JwpubImportRequest,
    JwpubPlaylistImportService,
)
from solin.core.media.operations import (
    MediaOperationCancelled,
    MediaOperationPresentation,
    MediaOperationSpec,
)
from solin.core.media.placement import (
    END_OF_LIST_INDEX,
    build_media_placement_options,
    resolve_media_placement,
)
from solin.core.playlists.items import playlist_items_from_jwpub
from solin.core.i18n.media_placement import translate_media_placement
from solin.styles.theme import PALETTE
from solin.styles.icons import (
    ICON_SECTION,
    ICON_IMAGE,
    ICON_VIDEO,
    ICON_MUSIC,
    ICON_CHEVRON_DOWN,
    ICON_CHEVRON_UP,
)
from solin.ui.dialogs.positioning import center_dialog_on_parent
from solin.ui.qml.host import configure_qml_host
from solin.ui.qml.jwpub_selection_model import JwpubSelectionModel
from solin.ui.qml.svg_icons import SvgIconProvider

log = logging.getLogger(__name__)


class _PublicationLease:
    """Defer scratch cleanup until workers have released the publication."""

    def __init__(self, publication) -> None:
        self.publication = publication
        self._lock = threading.Lock()
        self._users = 0
        self._retired = False

    @contextmanager
    def use(self):
        with self._lock:
            if self._retired:
                raise MediaOperationCancelled("Publication session closed")
            self._users += 1
        try:
            yield self.publication
        finally:
            with self._lock:
                self._users -= 1
                close = self._retired and self._users == 0
            if close:
                self.publication.close()

    def retire(self) -> None:
        with self._lock:
            if self._retired:
                return
            self._retired = True
            close = self._users == 0
        if close:
            self.publication.close()


def _preview_url(data: bytes | None) -> str:
    if not data:
        return ""
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.thumbnail((192, 120))
            output = io.BytesIO()
            image.convert("RGB").save(output, format="PNG")
        return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        return ""


class JwpubSelectionBridge(QObject):
    """Coordinate one origin-bound selection without performing I/O on the GUI thread."""

    changed = Signal()
    finished = Signal(bool)

    def __init__(
        self,
        *,
        service,
        settings,
        operations,
        language: str,
        dest_images_dir: str,
        scope_id: str,
        is_current: Callable[[], bool],
        placement_ref: Callable,
        insert: Callable,
        list_id: str | None = None,
        insert_index: int = END_OF_LIST_INDEX,
        public_talk: bool = False,
        auto_target: Callable | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.model = JwpubSelectionModel(self)
        self.model.selectionChanged.connect(self._selection_changed)
        self._service = service
        self._settings = settings
        self._operations = operations
        self._language = language
        self._destination = Path(dest_images_dir)
        self._scope_id = scope_id
        self._is_current = is_current
        self._placement_ref = placement_ref
        self._insert = insert
        self._explicit_target = (list_id, insert_index) if list_id is not None else None
        self._public_talk = public_talk
        self._auto_target = auto_target
        self._lease: _PublicationLease | None = None
        self._epoch = 0
        self._alive = True
        self._released = False
        self._busy = False
        self._committing = False
        self._error = ""
        self._title = ""
        self._source_path = ""
        self._status = ""
        self._show_placement = False
        self._placement_options = []
        self._operation_ids: set[str] = set()
        self._preview_ids: dict[str, str] = {}
        self._preview_queue: deque[str] = deque()
        self._visible_previews: set[str] = set()
        self._previews: OrderedDict[str, str] = OrderedDict()
        self.model.set_previews(self._previews)
        self._prepared: list[dict] | None = None
        self._created_paths: list[str] = []
        self._import_directory: Path | None = None
        self.unresolved_count = 0
        operations.operationFinished.connect(self._operation_finished)

    @Property(QObject, constant=True)
    def rows(self):
        return self.model

    @Property(bool, notify=changed)
    def busy(self):
        return self._busy

    @Property(bool, notify=changed)
    def cancellable(self):
        return not self._committing

    @Property(str, notify=changed)
    def errorText(self):  # noqa: N802
        return self._error

    @Property(str, notify=changed)
    def publicationTitle(self):  # noqa: N802
        return self._title

    @Property(str, notify=changed)
    def sourcePath(self):  # noqa: N802
        return self._source_path

    @Property(str, notify=changed)
    def statusText(self):  # noqa: N802
        return self._status

    @Property(bool, constant=True)
    def publicTalk(self):  # noqa: N802
        return self._public_talk

    @Property(bool, notify=changed)
    def hasPublication(self):  # noqa: N802
        return self._lease is not None

    @Property(bool, notify=changed)
    def canImport(self):  # noqa: N802
        return bool(self.model.selected_ids) and not self._busy

    @Property(str, notify=changed)
    def importLabel(self):  # noqa: N802
        return self.tr("Import (%n)", "", len(self.model.selected_ids))

    @Property(bool, notify=changed)
    def showPlacement(self):  # noqa: N802
        return self._show_placement

    @Property(list, notify=changed)
    def placementOptions(self):  # noqa: N802
        return self._placement_options

    @Property(str, notify=changed)
    def pendingItemTitle(self):  # noqa: N802
        return self._title

    @Property(str, constant=True)
    def pendingItemThumb(self):  # noqa: N802
        return ""

    @Slot()
    def browse(self) -> None:
        if self._busy:
            return
        parent = self.parent()
        path, _ = QFileDialog.getOpenFileName(
            parent if isinstance(parent, QWidget) else None,
            self.tr("Choose a publication"),
            self._source_path,
            self.tr("JW Publication (*.jwpub)"),
        )
        if path:
            self.load_path(path)

    def load_path(self, path: str) -> None:
        if not self._alive or not self._is_current():
            return
        self._epoch += 1
        self._cancel_previews()
        self._busy = True
        if self._lease is None:
            self._source_path = path
        self._error = ""
        self._status = self.tr("Reading publication…")
        self.changed.emit()

        def run(cancellation):
            publication = self._service.open(
                JwpubImportRequest(path, language=self._language, resolve_urls=False),
                cancelled=cancellation.is_set,
            )
            lease = _PublicationLease(publication)
            if self._public_talk and publication.catalog.symbol.casefold() != "s-34mp":
                lease.retire()
                raise ValueError(self.tr("Choose an S-34mp publication."))
            if not publication.catalog.documents:
                lease.retire()
                raise ValueError(self.tr("The publication contains no usable media."))
            return lease

        def ready(lease):
            self._rollback()
            self._prepared = None
            if self._lease is not None:
                self._lease.retire()
            self._lease = lease
            catalog = lease.publication.catalog
            self._source_path = path
            self._title = catalog.title
            self.model.set_catalog(catalog)
            self._previews.clear()
            if self._public_talk:
                self._settings.set_source_path(path)
            self._busy = False
            self._status = ""
            self.changed.emit()

        self._submit("catalog", run, ready, dispose=lambda lease: lease.retire())

    @Slot(str)
    def setSearchQuery(self, query: str) -> None:  # noqa: N802
        self.model.set_query(query)

    @Slot(str)
    def toggleDocument(self, document_id: str) -> None:  # noqa: N802
        if not self._busy:
            self.model.toggle_document(document_id)

    @Slot(str, str)
    def toggleMedia(self, document_id: str, media_id: str) -> None:  # noqa: N802
        if not self._busy:
            self.model.toggle_media(document_id, media_id)

    @Slot(str)
    def toggleExpanded(self, document_id: str) -> None:  # noqa: N802
        if not self._busy:
            self.model.toggle_expanded(document_id)

    @Slot()
    def selectAll(self) -> None:  # noqa: N802
        if not self._busy:
            self.model.select_all()

    @Slot()
    def clearSelection(self) -> None:  # noqa: N802
        if not self._busy:
            self.model.clear_selection()

    @Slot(str)
    def requestPreview(self, key: str) -> None:  # noqa: N802
        if not self._alive or self._lease is None or not self.model.preview_media_id(key):
            return
        self._visible_previews.add(key)
        if key in self._previews:
            self._previews.move_to_end(key)
        elif key not in self._preview_ids and key not in self._preview_queue:
            self._preview_queue.append(key)
            self._pump_previews()

    @Slot(str)
    def releasePreview(self, key: str) -> None:  # noqa: N802
        self._visible_previews.discard(key)
        self._preview_queue = deque(value for value in self._preview_queue if value != key)
        if key in self._preview_ids:
            self._operations.cancel(self._preview_ids[key])

    def _pump_previews(self) -> None:
        while self._alive and not self._busy and self._preview_queue and len(self._preview_ids) < 2:
            key = self._preview_queue.popleft()
            media_id = self.model.preview_media_id(key)
            lease = self._lease
            if not media_id or lease is None or key not in self._visible_previews:
                continue

            def run(cancellation, current=lease, item_id=media_id):
                with current.use() as publication:
                    return _preview_url(publication.preview(item_id, cancelled=cancellation.is_set))

            def ready(url, current_key=key):
                self._previews[current_key] = url
                while len(self._previews) > 128:
                    self._previews.popitem(last=False)
                self.model.set_previews(self._previews)

            operation_id = self._submit("preview", run, ready, quiet=True)
            if operation_id:
                self._preview_ids[key] = operation_id

    def _selection_changed(self) -> None:
        if self._prepared is not None and not self._committing:
            self._rollback()
            self._prepared = None
        self.changed.emit()

    @Slot()
    def importSelected(self) -> None:  # noqa: N802
        if not self.canImport or self._lease is None:
            return
        self._error = ""
        if not self._is_current():
            self.cancel()
            return
        target = self._explicit_target
        if self._public_talk and self._auto_target is not None:
            target = self._auto_target()
        if target is None:
            options = build_media_placement_options(
                self._placement_ref(),
                translate=translate_media_placement,
                force_prompt=self._public_talk,
            )
            if options:
                self._placement_options = options
                self._show_placement = True
                self.changed.emit()
                return
            target = ("root", END_OF_LIST_INDEX)
        self._prepare(target)

    @Slot(str)
    def confirmPlacement(self, placement_id: str) -> None:  # noqa: N802
        self._show_placement = False
        self._prepare(resolve_media_placement(placement_id))

    @Slot()
    def cancelSelection(self) -> None:  # noqa: N802
        self._show_placement = False
        self.changed.emit()

    def _prepare(self, target: tuple[str, int]) -> None:
        if not self._alive or self._lease is None or not self._is_current():
            self.cancel()
            return
        self._busy = True
        self._status = self.tr("Importing media…")
        self._cancel_previews()
        self.changed.emit()
        if self._prepared is not None:
            lease = self._lease

            def validate(cancellation):
                with lease.use() as publication:
                    publication.validate_source(cancelled=cancellation.is_set)

            self._submit("validate", validate, lambda _: self._insert_prepared(target))
            return
        lease = self._lease
        selected_ids = self.model.selected_ids
        destination = self._destination / f"jwpub-{uuid4().hex}"
        self._import_directory = destination
        created_paths: list[str] = []
        self._created_paths = created_paths

        def run(cancellation):
            with lease.use() as publication:
                publication.validate_source(cancelled=cancellation.is_set)
                return publication.materialize(
                    selected_ids,
                    dest_images_dir=str(destination),
                    resolve_urls=True,
                    cancelled=cancellation.is_set,
                    created_paths=created_paths,
                )

        def ready(result):
            items, stem = result
            self._prepared = [dict(item) for item in playlist_items_from_jwpub(items, stem)]
            self.unresolved_count = sum(
                not item.get("url") for item in self._prepared if item.get("type") != "image"
            )
            if not self._prepared:
                self._busy = False
                self._error = self.tr("The publication contains no usable media.")
                self.changed.emit()
                return
            self._insert_prepared(target)

        self._submit("materialize", run, ready, dispose=lambda _: self._rollback())

    def _insert_prepared(self, target: tuple[str, int]) -> None:
        if not self._is_current():
            self.cancel()
            return
        self._committing = True
        self.changed.emit()

        def completed(success: bool) -> None:
            if not self._alive:
                return
            self._busy = False
            self._committing = False
            if success:
                self._created_paths.clear()
                self.finished.emit(True)
            elif self._is_current():
                self._error = self.tr(
                    "The selected location is unavailable. Choose another location."
                )
                self._placement_options = build_media_placement_options(
                    self._placement_ref(),
                    translate=translate_media_placement,
                    force_prompt=True,
                )
                self._show_placement = True
                self.changed.emit()
            else:
                self.cancel()

        try:
            self._insert(list(self._prepared or []), target[0], target[1], completed)
        except Exception:  # noqa: BLE001 - injected insertion boundary
            self._committing = False
            self._busy = False
            raise

    def _submit(self, kind, run, commit, *, dispose=None, quiet=False) -> str:
        operation_id = f"jwpub-{kind}:{uuid4().hex}"
        epoch = self._epoch
        holder: list[Any] = []

        def cleanup() -> None:
            if holder and dispose is not None:
                dispose(holder.pop())

        def runner(_report, cancellation):
            try:
                result = run(cancellation)
            except JwpubImportCancelled as exc:
                raise MediaOperationCancelled(str(exc)) from exc
            holder.append(result)
            if cancellation.is_set():
                cleanup()
                raise MediaOperationCancelled("Publication operation cancelled")
            return result

        def deliver(result):
            if not self._alive or epoch != self._epoch or not self._is_current():
                cleanup()
                if self._alive and not self._is_current():
                    self.cancel()
                return
            try:
                commit(result)
            except BaseException:  # noqa: BLE001 - release uncommitted worker results before delivery fails
                cleanup()
                raise
            holder.clear()

        def failed(message, _retryable=False):
            cleanup()
            if self._alive and not self._is_current():
                self.cancel()
                return
            if self._alive and epoch == self._epoch and not quiet:
                self._busy = False
                self._status = ""
                self._error = message
                self.changed.emit()

        def cancelled():
            cleanup()
            if self._alive and epoch == self._epoch and not quiet:
                self._busy = False
                if not self._is_current():
                    self.cancel()
                else:
                    self.changed.emit()

        self._operation_ids.add(operation_id)
        accepted = self._operations.submit(
            MediaOperationSpec(
                operation_id=operation_id,
                scope_id=self._scope_id,
                operation_type=f"jwpub-{kind}",
                conflict_key=f"jwpub-session:{id(self)}",
                presentation=MediaOperationPresentation.BACKGROUND,
                runner=runner,
                commit=deliver,
                failed=failed,
                cancelled=cancelled,
            )
        )
        if not accepted:
            self._operation_ids.discard(operation_id)
            failed(self.tr("The import service is unavailable."))
            return ""
        return operation_id

    @Slot(str, str)
    def _operation_finished(self, operation_id: str, _state: str) -> None:
        self._operation_ids.discard(operation_id)
        for key, value in list(self._preview_ids.items()):
            if value == operation_id:
                del self._preview_ids[key]
                if (
                    _state == "cancelled"
                    and key in self._visible_previews
                    and key not in self._previews
                    and self.model.preview_media_id(key)
                ):
                    self._preview_queue.append(key)
        if self._alive:
            QTimer.singleShot(0, self, self._pump_previews)
        elif not self._operation_ids:
            self._finish_dispose()

    def _cancel_previews(self) -> None:
        self._preview_queue.clear()
        self._visible_previews.clear()
        for operation_id in tuple(self._preview_ids.values()):
            self._operations.cancel(operation_id)

    def _rollback(self) -> None:
        for path in self._created_paths:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                log.warning(
                    "Could not remove uncommitted publication media %s", path, exc_info=True
                )
        self._created_paths.clear()
        if self._import_directory is not None and self._import_directory.is_dir():
            try:
                self._import_directory.rmdir()
            except OSError:
                log.debug("Publication import directory is still in use", exc_info=True)

    @Slot()
    def cancel(self) -> None:
        if self._alive and not self._committing:
            self.finished.emit(False)

    def dispose(self) -> None:
        if not self._alive:
            return
        self._alive = False
        # The operation owner keeps callbacks alive after the modal shell closes.
        self.setParent(self._operations)
        if self._lease is not None:
            self._lease.retire()
            self._lease = None
        for operation_id in tuple(self._operation_ids):
            self._operations.cancel(operation_id)
        self._cancel_previews()
        if not self._operation_ids:
            self._finish_dispose()

    def _finish_dispose(self) -> None:
        if self._released:
            return
        self._released = True
        self._rollback()
        self._operations.operationFinished.disconnect(self._operation_finished)
        self.deleteLater()


class JwpubSelectionDialog(QDialog):
    """Native modal shell sharing the application's QML theme and placement picker."""

    def __init__(self, bridge: JwpubSelectionBridge, parent=None) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self.setWindowTitle(self.tr("Import publication media"))
        self.setModal(True)
        self.resize(760, 660)
        self.setMinimumSize(540, 430)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.qml_widget = QQuickWidget(self)
        configure_qml_host(
            self.qml_widget,
            type_name="JwpubSelectionDialog",
            clear_color=PALETTE.bg0,
            context_properties={"selectionBridge": bridge},
            mouse_tracking=True,
            image_providers={
                "publicationicons": SvgIconProvider(
                    {
                        "publication": ICON_SECTION,
                        "image": ICON_IMAGE,
                        "video": ICON_VIDEO,
                        "audio": ICON_MUSIC,
                        "down": ICON_CHEVRON_DOWN,
                        "up": ICON_CHEVRON_UP,
                    },
                    default_icon="publication",
                )
            },
        )
        layout.addWidget(self.qml_widget)
        bridge.finished.connect(self._finish)

    def _finish(self, success: bool) -> None:
        self.done(QDialog.DialogCode.Accepted if success else QDialog.DialogCode.Rejected)

    def reject(self) -> None:
        if self.bridge.cancellable:
            super().reject()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        center_dialog_on_parent(self)

    def dispose(self) -> None:
        # Tear down delegates while their host, model and context still exist.
        self.qml_widget.setSource(QUrl())
        self.bridge.dispose()
        self.deleteLater()


class JwpubSelectionFactory:
    """Inject one parser and profile source setting into interactive entry points."""

    def __init__(self, settings, *, service=None, dialog_factory=JwpubSelectionDialog) -> None:
        self._settings = settings
        self._service = service or JwpubPlaylistImportService()
        self._dialog_factory = dialog_factory
        self._active = False

    def select(
        self,
        paths: list[str],
        *,
        parent,
        operations,
        language: str,
        dest_images_dir: str,
        scope_id: str,
        is_current: Callable[[], bool],
        placement_ref: Callable[[], Mapping | None],
        insert: Callable,
        list_id: str | None = None,
        insert_index: int = END_OF_LIST_INDEX,
        public_talk: bool = False,
        auto_target: Callable | None = None,
    ) -> None:
        if self._active:
            return
        self._active = True
        try:
            sources = [self._settings.source_path()] if public_talk else paths
            for path in sources:
                if not is_current():
                    break
                bridge = JwpubSelectionBridge(
                    service=self._service,
                    settings=self._settings,
                    operations=operations,
                    language=language,
                    dest_images_dir=dest_images_dir,
                    scope_id=scope_id,
                    is_current=is_current,
                    placement_ref=placement_ref,
                    insert=insert,
                    list_id=list_id,
                    insert_index=insert_index,
                    public_talk=public_talk,
                    auto_target=auto_target,
                )
                dialog = self._dialog_factory(bridge, parent)
                bridge.setParent(dialog)
                if path:
                    QTimer.singleShot(
                        0,
                        bridge,
                        lambda source=path, current=bridge: current.load_path(source),
                    )
                try:
                    accepted = dialog.exec() == QDialog.DialogCode.Accepted
                    if accepted and bridge.unresolved_count:
                        QMessageBox.warning(
                            parent,
                            dialog.tr("Import publication media"),
                            bridge.tr(
                                "%n media item(s) could not be resolved. Their references were retained.",
                                "",
                                bridge.unresolved_count,
                            ),
                        )
                finally:
                    dialog.dispose()
                if not accepted:
                    break
        finally:
            self._active = False
