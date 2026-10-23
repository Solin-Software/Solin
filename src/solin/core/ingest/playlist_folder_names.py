"""Naming policy for folders classified as linked playlists."""

from solin.core.foundation.folder_names import (
    FolderNameError,
    FolderNameIssue,
    normalize_portable_folder_name,
)
from solin.core.meetings.folder_matcher import match_meeting_folder


class MeetingFolderNameError(FolderNameError):
    """A valid portable name belongs to the meetings classifier."""


def normalize_playlist_folder_name(value: object) -> str:
    name = normalize_portable_folder_name(value)
    if match_meeting_folder(name) is not None:
        raise MeetingFolderNameError(value, FolderNameIssue.RESERVED)
    return name
