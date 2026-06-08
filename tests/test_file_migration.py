from app.core.storage.migration import (
    merge_dirs,
    move_dir_if_exists,
    move_file_if_exists,
    unique_destination,
)


def test_unique_destination_adds_legacy_suffix_for_existing_file(tmp_path):
    existing = tmp_path / "playlist.json"
    existing.write_text("current", encoding="utf-8")

    assert unique_destination(existing) == tmp_path / "playlist_legacy_2.json"


def test_move_file_if_exists_copies_to_unique_destination_and_removes_source(tmp_path):
    src = tmp_path / "legacy.json"
    dst = tmp_path / "profile" / "playlist.json"
    src.write_text("legacy", encoding="utf-8")
    dst.parent.mkdir()
    dst.write_text("current", encoding="utf-8")

    assert move_file_if_exists(src, dst) is True

    assert not src.exists()
    assert dst.read_text(encoding="utf-8") == "current"
    assert (dst.parent / "playlist_legacy_2.json").read_text(encoding="utf-8") == "legacy"


def test_move_dir_if_exists_merges_conflicting_files_and_removes_source(tmp_path):
    src = tmp_path / "legacy_images"
    dst = tmp_path / "profile" / "images"
    (src / "nested").mkdir(parents=True)
    (dst / "nested").mkdir(parents=True)
    (src / "cover.jpg").write_text("legacy-cover", encoding="utf-8")
    (dst / "cover.jpg").write_text("current-cover", encoding="utf-8")
    (src / "nested" / "a.txt").write_text("legacy-nested", encoding="utf-8")

    assert move_dir_if_exists(src, dst) is True

    assert not src.exists()
    assert (dst / "cover.jpg").read_text(encoding="utf-8") == "current-cover"
    assert (dst / "cover_legacy_2.jpg").read_text(encoding="utf-8") == "legacy-cover"
    assert (dst / "nested" / "a.txt").read_text(encoding="utf-8") == "legacy-nested"


def test_merge_dirs_creates_destination_when_missing(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    (src / "item.txt").write_text("value", encoding="utf-8")

    merge_dirs(src, dst)

    assert (dst / "item.txt").read_text(encoding="utf-8") == "value"
