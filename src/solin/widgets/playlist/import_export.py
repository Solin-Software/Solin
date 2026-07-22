from __future__ import annotations

from pathlib import Path

from ...core.jw.language_context import jw_media_language_context
from ...core.playlists.items import create_playlist_item


__all__ = ("PlaylistEditImportMixin",)


class PlaylistEditImportMixin:
    def _import_pdfs(
        self,
        pdf_paths: list[str],
        insert_at: int = -1,
        section_id: str = "",
    ) -> None:
        for pdf_path in pdf_paths:
            self._import_single_pdf(pdf_path, insert_at=insert_at, section_id=section_id)

    def _import_single_pdf(
        self,
        pdf_path: str,
        insert_at: int = -1,
        section_id: str = "",
    ) -> None:
        stem = Path(pdf_path).stem
        pages = self._document_conversion_service.cached_pdf_pages(pdf_path)
        if pages:
            self._on_pdf_pages_ready(pages, stem, insert_at, section_id)
            return
        self._notifications.information(
            self.tr("Converting PDF: {name}…").replace("{name}", str(stem))
        )
        thread = self._document_conversion_service.create_pdf_thread(
            pdf_path,
            parent=self,
        )
        self._pdf_threads.append(thread)
        thread.pages_ready.connect(
            lambda pages, stem, _pos=insert_at, _sid=section_id:
                self._on_pdf_pages_ready(pages, stem, _pos, _sid)
        )
        thread.conversion_failed.connect(self._on_pdf_failed)
        thread.finished.connect(
            lambda t=thread: self._pdf_threads.remove(t)
            if t in self._pdf_threads else None
        )
        thread.start()

    def _on_pdf_pages_ready(
        self,
        pages: list[str],
        pdf_stem: str,
        insert_at: int = -1,
        section_id: str = "",
    ) -> None:
        if not self._pl:
            return
        new_items = [
            create_playlist_item(
                title=f"{pdf_stem} — p. {i + 1}",
                url=page_path,
                type="image",
                section_id=section_id,
            )
            for i, page_path in enumerate(pages)
        ]
        items = self._pl.setdefault("items", [])
        if insert_at < 0 or insert_at >= len(items):
            items.extend(new_items)
        else:
            for i, ni in enumerate(new_items):
                items.insert(insert_at + i, ni)
        self._save()
        self._reconcile_playlist()
        self._notifications.success(
            self.tr("{name} opened ({pages} pages)")
            .replace("{name}", str(pdf_stem))
            .replace("{pages}", str(len(new_items)))
        )

    def _on_pdf_failed(self, error_msg: str) -> None:
        self._notifications.error(
            self.tr("Error converting PDF: {error}").replace("{error}", str(error_msg))
        )

    def _import_jwpubs(
        self,
        paths: list[str],
        insert_at: int = -1,
        section_id: str = "",
    ) -> None:
        for path in paths:
            self._import_single_jwpub(path, insert_at=insert_at, section_id=section_id)

    def _import_single_jwpub(
        self,
        jwpub_path: str,
        insert_at: int = -1,
        section_id: str = "",
    ) -> None:
        if not self._pl:
            return
        stem = Path(jwpub_path).stem
        lang = jw_media_language_context(self.lang).api_code
        pl_ref = self._pl
        insert_pos = insert_at
        target_section_id = section_id
        self._notifications.information(
            self.tr("Opening {name}…").replace("{name}", stem)
        )
        thread = self._jwpub_import_thread_factory.create(
            jwpub_path,
            lang=lang,
            parent=self,
        )
        if not hasattr(self, "_jwpub_threads"):
            self._jwpub_threads: list = []
        self._jwpub_threads.append(thread)
        thread.finished.connect(
            lambda t=thread: self._jwpub_threads.remove(t)
            if t in self._jwpub_threads else None
        )

        @thread.items_ready.connect
        def _on_ready(items: list, file_stem: str):
            if pl_ref is None:
                return
            new_items = []
            for raw in items:
                it = create_playlist_item(
                    title=raw.get("title", file_stem),
                    url=raw.get("url", ""),
                    type=raw.get("type", "video"),
                    key_symbol=raw.get("key_symbol"),
                    track=raw.get("track"),
                    issue_tag=raw.get("issue_tag"),
                    doc_id=raw.get("doc_id"),
                    section_id=target_section_id,
                )
                if raw.get("url") and raw.get("type") != "image":
                    it["auto_title"] = False
                new_items.append(it)
            if not new_items:
                self._notifications.warning(
                    self.tr("No media found in {name}").replace("{name}", file_stem)
                )
                return
            items_list = pl_ref.setdefault("items", [])
            if insert_pos < 0 or insert_pos >= len(items_list):
                items_list.extend(new_items)
            else:
                for i, ni in enumerate(new_items):
                    items_list.insert(insert_pos + i, ni)
            self._save()
            self._reconcile_playlist()
            parts = []
            n_img = sum(1 for it in new_items if it.get("type") == "image")
            n_vid = sum(1 for it in new_items if it.get("type") != "image" and it.get("url"))
            n_bad = sum(1 for it in new_items if it.get("type") != "image" and not it.get("url"))
            if n_img:
                parts.append(f"{n_img} " + self.tr("images"))
            if n_vid:
                parts.append(f"{n_vid} " + self.tr("videos"))
            msg = file_stem + " — " + ", ".join(parts) if parts else file_stem
            if n_bad:
                msg += f"  ({n_bad} " + self.tr("unresolved") + ")"
                self._notifications.warning(msg)
            else:
                self._notifications.success(msg)

        @thread.failed.connect
        def _on_fail(err: str):
            self._notifications.error(
                self.tr("Could not open .jwpub: {err}").replace("{err}", err[:160])
            )

        thread.start()

    def _import_lo_files(
        self,
        paths: list[str],
        insert_at: int = -1,
        section_id: str = "",
    ) -> None:
        for path in paths:
            self._import_single_lo(path, insert_at=insert_at, section_id=section_id)

    def _import_single_lo(
        self,
        lo_path: str,
        insert_at: int = -1,
        section_id: str = "",
    ) -> None:
        stem = Path(lo_path).stem
        pages = self._document_conversion_service.cached_office_pages(lo_path)
        if pages:
            self._on_lo_pages_ready(pages, stem, insert_at, section_id)
            return
        self._notifications.information(
            self.tr("Converting PDF: {name}…").replace("{name}", str(stem))
        )
        thread = self._document_conversion_service.create_office_thread(
            lo_path,
            parent=self,
        )
        self._lo_threads.append(thread)
        thread.pages_ready.connect(
            lambda pages, stem, _pos=insert_at, _sid=section_id:
                self._on_lo_pages_ready(pages, stem, _pos, _sid)
        )
        thread.conversion_failed.connect(self._on_pdf_failed)
        thread.finished.connect(
            lambda t=thread: self._lo_threads.remove(t)
            if t in self._lo_threads else None
        )
        thread.start()

    def _on_lo_pages_ready(
        self,
        pages: list[str],
        stem: str,
        insert_at: int = -1,
        section_id: str = "",
    ) -> None:
        if not self._pl:
            return
        new_items = [
            create_playlist_item(
                title=f"{stem} — p. {i + 1}",
                url=page_path,
                type="image",
                section_id=section_id,
            )
            for i, page_path in enumerate(pages)
        ]
        items = self._pl.setdefault("items", [])
        if insert_at < 0 or insert_at >= len(items):
            items.extend(new_items)
        else:
            for i, ni in enumerate(new_items):
                items.insert(insert_at + i, ni)
        self._save()
        self._reconcile_playlist()
        self._notifications.success(
            self.tr("{name} opened ({pages} pages)")
            .replace("{name}", str(stem))
            .replace("{pages}", str(len(new_items)))
        )

    def _import_jwlplaylists_drop(
        self,
        jwl_paths: list[str],
        insert_at: int = -1,
        section_id: str = "",
    ) -> None:
        if not self._pl:
            return
        self.import_jwl_requested.emit(jwl_paths, insert_at, section_id)

    def commit_imported_jwl_items(
        self,
        items: list[dict],
        *,
        insert_at: int,
        expected_playlist_id: str,
    ) -> None:
        if not self._pl or str(self._pl.get("id")) != expected_playlist_id:
            raise RuntimeError(self.tr("The destination playlist is no longer open."))
        if not items:
            return
        target = self._pl.setdefault("items", [])
        previous_items = list(target)
        if insert_at < 0 or insert_at >= len(target):
            target.extend(items)
        else:
            target[insert_at:insert_at] = items
        try:
            if self._is_temp:
                pass
            elif self._is_watched:
                self._save()
            else:
                self._playlist_repository.save_strict(self._all_playlists)
        except Exception:  # noqa: BLE001 - transactional playlist commit rollback
            self._pl["items"] = previous_items
            self._reconcile_playlist()
            raise
        self._reconcile_playlist()
        if len(items) == 1:
            self._notifications.success(self.tr("1 item imported"))
        else:
            self._notifications.success(
                self.tr("{count} items imported").replace("{count}", str(len(items)))
            )
