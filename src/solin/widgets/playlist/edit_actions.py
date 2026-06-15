from __future__ import annotations

import copy
import os
import random
import uuid
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox

from ...core.foundation.constants import (
    JWPUB_EXTS as _JWPUB_EXTS,
    PDF_EXTS as _PDF_EXTS,
)
from ...core.media.formats import MEDIA_EXTS as _MEDIA_EXTS, media_type_from_path
from ...core.jw.identifiers import lang_to_meps
from ...core.jw.language_context import jw_media_language_context
from ...core.playlists.jwl_files import PlaylistWriteError, write_jwlplaylist_document
from ...core.playlists.items import create_playlist_item
from .dialogs import NameDialog
from .item_visuals import enrich_items_for_export


class _PlaylistEditActionsMixin:
    def _list_id_for_section(self, section_id: str) -> str:
        if not section_id or not self._pl:
            return "root"
        section = next(
            (s for s in self._pl.get("sections", [])
             if s.get("id") == section_id),
            None,
        )
        if not section:
            return "root"
        prefix = "subsection" if section.get("parent_id") else "section"
        return f"{prefix}:{section_id}"

    def _sync_playlist_chrome(self, *, emit_data_changed: bool = True) -> None:
        if not self._pl:
            return
        items = self._pl.get("items", [])
        n = len(items)
        word = self.tr("item") if n == 1 else self.tr("items")
        self.bridge.set_state(
            name=self._pl.get("name", ""),
            is_temp=self._is_temp,
            is_watched=self._is_watched,
            item_count=n,
            item_word=word,
            has_entries=self.model.entry_count() > 0,
            emit_data_changed=emit_data_changed,
        )

    def _add_files(
        self,
        paths: list[str],
        insert_at: int = -1,
        section_id: str = "",
        target_list_id: str = "",
        target_list_index: int = -1,
    ) -> None:
        if not self._pl:
            return
        added = skipped = 0
        new_items = []
        for path in paths:
            if Path(path).suffix.lower() not in _MEDIA_EXTS:
                continue

            actual_path = path
            if self._is_watched and self._watched_path:
                watched_sub = Path(self._watched_path)
                try:
                    Path(path).relative_to(watched_sub)
                except ValueError:
                    try:
                        actual_path = self._watched_folder_file_store.copy_file_into_folder(
                            path,
                            watched_sub,
                        )
                    except OSError as e:
                        import logging

                        logging.getLogger(__name__).warning(
                            "Failed to copy %s to watched folder: %s",
                            path,
                            e,
                        )

            if self._url_in_playlist(actual_path):
                skipped += 1
                continue
            kw = {"section_id": section_id} if section_id else {}
            new_items.append(
                create_playlist_item(
                    title=Path(actual_path).stem,
                    url=actual_path,
                    **kw,
                )
            )
            added += 1
        if added:
            items = self._pl.setdefault("items", [])
            inserted_with_tree = (
                bool(target_list_id)
                and target_list_index >= 0
                and self.model.insert_media_refs(
                    target_list_id,
                    target_list_index,
                    new_items,
                )
            )
            appended_flat = False
            if inserted_with_tree:
                pass
            elif insert_at < 0 or insert_at >= len(items):
                items.extend(new_items)
                appended_flat = True
            else:
                for i, ni in enumerate(new_items):
                    items.insert(insert_at + i, ni)
                self.model.rebuild(self._pl)
            self._save()
            if appended_flat:
                self.model.rebuild(self._pl)
            self._sync_playlist_chrome(emit_data_changed=False)

            list_id = target_list_id or self._list_id_for_section(section_id)
            list_index = target_list_index if target_list_index >= 0 else 2**31 - 1
            self.bridge.emit_media_inserted(
                list_id,
                list_index,
                [item["id"] for item in new_items],
            )
            self.bridge.emit_section_counts_changed()
            QTimer.singleShot(0, self._request_missing_thumbnails)
            if added == 1:
                msg = self.tr("1 file added")
            else:
                msg = self.tr("{count} files added").replace("{count}", str(added))
            if skipped == 1:
                msg += "  " + self.tr("(1 duplicate skipped)")
            elif skipped > 1:
                msg += "  " + self.tr("({count} duplicates skipped)").replace(
                    "{count}",
                    str(skipped),
                )
            self._notifications.success(msg)
        elif skipped:
            if skipped == 1:
                self._notifications.warning(self.tr("File already in playlist"))
            else:
                self._notifications.warning(self.tr("Files already in playlist"))

    def _on_jw_media_confirmed(self, item_data: dict, target_list_id: str, target_index: int) -> None:
        if not self._pl:
            return

        pl_item_id = str(uuid.uuid4())

        track_val = None
        try:
            if item_data.get("track"):
                track_val = int(item_data["track"])
        except (ValueError, TypeError):
            pass

        meps_lang = 0
        try:
            meps_lang = int(item_data.get("meps_language") or 0)
        except (ValueError, TypeError):
            meps_lang = 0

        lang_str = item_data.get("language", "").upper()
        if not meps_lang and lang_str:
            meps_lang = lang_to_meps(lang_str)

        pl_item = {
            "id": pl_item_id,
            "title": item_data.get("title", ""),
            "url": item_data.get("download_url", ""),
            "type": item_data.get("media_type", "video"),
            "auto_title": True,
            "duration_seconds": item_data.get("duration_seconds", 0.0),
            "key_symbol": item_data.get("pub") or None,
            "track": track_val,
            "issue_tag": item_data.get("issue") or None,
            "doc_id": item_data.get("docid") or None,
            "meps_language": meps_lang,
        }

        thumb_path = item_data.get("thumbnail_path", "")
        if thumb_path and os.path.exists(thumb_path):
            try:
                self._playlist_thumbnail_store.copy_from(pl_item_id, thumb_path)
            except OSError as e:
                import logging

                logging.getLogger(__name__).warning(
                    "Failed to copy catalog thumbnail: %s",
                    e,
                )

        items = self._pl.setdefault("items", [])
        inserted = False
        if target_list_id and target_index >= 0:
            inserted = self.model.insert_media_refs(
                target_list_id,
                target_index,
                [pl_item],
            )

        if not inserted:
            items.append(pl_item)
            self.model.rebuild(self._pl)

        self._save()
        self._sync_playlist_chrome(emit_data_changed=False)

        list_id = target_list_id or "root"
        list_index = target_index if target_index >= 0 else 2**31 - 1
        self.bridge.emit_media_inserted(
            list_id,
            list_index,
            [pl_item_id],
        )
        self.bridge.emit_section_counts_changed()
        QTimer.singleShot(0, self._request_missing_thumbnails)

    def _add_dialog(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            self.tr("Add Media"),
            os.path.expanduser("~"),
            "Media, PDF & Publication "
            "(*.mp4 *.mkv *.mov *.avi *.webm *.mp3 *.m4a *.wav "
            "*.jpg *.jpeg *.png *.gif *.webp *.pdf *.jwpub *.jwlplaylist);;"
            "JW Playlist (*.jwlplaylist);;"
            "JW Publication (*.jwpub);;"
            "PDF (*.pdf);;"
            "Video (*.mp4 *.mkv *.mov *.avi *.webm);;"
            "Audio (*.mp3 *.m4a *.wav);;"
            "Image (*.jpg *.jpeg *.png *.gif *.webp);;"
            "All (*)",
        )
        if not paths:
            return
        from ...core.foundation.constants import PLAYLIST_EXTS as _PLAYLIST_EXTS

        media = [p for p in paths if Path(p).suffix.lower() in _MEDIA_EXTS]
        pdfs = [p for p in paths if Path(p).suffix.lower() in _PDF_EXTS]
        jwpubs = [p for p in paths if Path(p).suffix.lower() in _JWPUB_EXTS]
        jwl_files = [p for p in paths if Path(p).suffix.lower() in _PLAYLIST_EXTS]
        if media:
            self._add_files(media, insert_at=-1)
        if pdfs:
            self._import_pdfs(pdfs)
        if jwpubs:
            self._import_jwpubs(jwpubs)
        if jwl_files:
            self._import_jwlplaylists_drop(jwl_files)

    def _project_by_id(self, item_id: str) -> None:
        if not self._pl:
            return
        items = self._pl.get("items", [])
        try:
            idx = next(i for i, it in enumerate(items) if it["id"] == item_id)
        except StopIteration:
            return
        self.project_items.emit(items[idx:] + items[:idx], 0, "")

    @staticmethod
    def _is_playable(item: dict) -> bool:
        url = item.get("url", "")
        if not url:
            return False
        if url.startswith(("http://", "https://")):
            return True
        return os.path.exists(url)

    def _do_play_all(self) -> None:
        if not self._pl:
            return
        items = [it for it in self._pl.get("items", []) if self._is_playable(it)]
        if items:
            self.project_items.emit(items, 0, "next")

    def _do_shuffle(self) -> None:
        if not self._pl:
            return
        items = [it for it in self._pl.get("items", []) if self._is_playable(it)]
        if not items:
            return
        s = random.randrange(len(items))
        self.project_items.emit(items[s:] + items[:s], 0, "random")

    def _export(self) -> None:
        if not self._pl:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            self.tr("Export .jwlplaylist"),
            os.path.join(
                os.path.expanduser("~"),
                self._pl["name"].replace(" ", "_") + ".jwlplaylist",
            ),
            "JW Library Playlist (*.jwlplaylist)",
        )
        if not path:
            return
        try:
            items = enrich_items_for_export(
                self._pl.get("items", []),
                self._id_to_thumb,
                self._playlist_thumbnail_store,
            )
            fallback_lang = jw_media_language_context(self.lang).fallback_code
            write_jwlplaylist_document(
                self._pl["name"],
                items,
                path,
                self._media_cache_manager.media_cache_dir,
                fallback_lang_code=fallback_lang,
            )
            QMessageBox.information(
                self,
                self.tr("Export complete"),
                self.tr("Exported:\n{path}").replace("{path}", str(path)),
            )
        except (OSError, ValueError, PlaylistWriteError) as e:
            QMessageBox.critical(self, self.tr("Export error"), str(e))

    def _on_back(self) -> None:
        self._is_temp = False
        self._sync_order()
        self.back_requested.emit()

    def load_temp_playlist(
        self,
        items: list,
        lang=None,
        name: str | None = None,
        *,
        playlist_id: str | None = None,
    ) -> str:
        if lang:
            self.lang = lang
        self._is_temp = True
        display_name = name or self.tr("Current playback")
        norm = []
        for raw in items:
            item = dict(raw)
            if not item.get("id"):
                item["id"] = str(uuid.uuid4())
            if not item.get("type"):
                item["type"] = media_type_from_path(
                    item.get("url", ""),
                    default="video",
                )
            norm.append(item)
        temp_id = playlist_id or f"__temp__:{uuid.uuid4()}"
        pl = {"id": temp_id, "name": display_name, "items": norm, "_temp": True}
        self.load_playlist(pl)
        return temp_id

    def _save_temp_playlist(self) -> None:
        dlg = NameDialog(lang=self.lang, parent=self)
        dlg.setWindowTitle(self.tr("Save playlist"))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name = dlg.get_name()
        if not name:
            return
        self.model.finalize_drag()
        self.save_temp_as_permanent.emit(name, copy.deepcopy(self._pl or {}))
