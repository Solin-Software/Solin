from __future__ import annotations

from pathlib import Path

ABI_VERSION = 1

WINDOWS_LIBRARY_NAME = "native_webview_widget.dll"
MACOS_LIBRARY_NAMES = (
    "libnative_webview_widget.dylib",
    "native_webview_widget.dylib",
)

REQUIRED_EXPORTS = (
    "nwv_create",
    "nwv_destroy",
    "nwv_set_event_callback",
    "nwv_set_policy_callback",
    "nwv_set_capture_callback",
    "nwv_resize",
    "nwv_navigate",
    "nwv_set_html",
    "nwv_reload",
    "nwv_go_back",
    "nwv_go_forward",
    "nwv_eval_js",
    "nwv_add_document_script",
    "nwv_set_default_context_menu_enabled",
    "nwv_set_devtools_enabled",
    "nwv_capture_png",
    "nwv_capture_jpeg",
    "nwv_start_frame_stream",
    "nwv_stop_frame_stream",
    "nwv_set_cookie",
    "nwv_clear_cookies",
    "nwv_can_go_back",
    "nwv_can_go_forward",
)

EVENT_READY = 1
EVENT_NAVIGATION_STARTED = 2
EVENT_NAVIGATION_FINISHED = 3
EVENT_NAVIGATION_FAILED = 4
EVENT_TITLE_CHANGED = 5
EVENT_DOWNLOAD_REQUESTED = 6
EVENT_NEW_WINDOW_REQUESTED = 7
EVENT_SCRIPT_MESSAGE = 8

EVENTS = {
    "ready": EVENT_READY,
    "navigation_started": EVENT_NAVIGATION_STARTED,
    "navigation_finished": EVENT_NAVIGATION_FINISHED,
    "navigation_failed": EVENT_NAVIGATION_FAILED,
    "title_changed": EVENT_TITLE_CHANGED,
    "download_requested": EVENT_DOWNLOAD_REQUESTED,
    "new_window_requested": EVENT_NEW_WINDOW_REQUESTED,
    "script_message": EVENT_SCRIPT_MESSAGE,
}


def native_library_candidates(system: str, package_dir: Path) -> tuple[Path, ...]:
    if system == "Windows":
        return (package_dir / WINDOWS_LIBRARY_NAME,)
    if system == "Darwin":
        return tuple(package_dir / name for name in MACOS_LIBRARY_NAMES)
    return ()
