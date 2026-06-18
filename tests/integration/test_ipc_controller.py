from solin.controllers.ipc_controller import IpcController
from solin.bootstrap import single_instance


class _Raw:
    def __init__(self, text):
        self._data = text.encode("utf-8")

    def data(self):
        return self._data

    def isEmpty(self):
        return not self._data


class _Conn:
    def __init__(self, text):
        self._raw = _Raw(text)

    def readAll(self):
        return self._raw


def test_valid_payload_paths_keeps_existing_files_and_urls(tmp_path):
    media_file = tmp_path / "clip.mp4"
    media_file.write_bytes(b"")

    payload = f"""
    {media_file}
    https://example.test/video.mp4
    http://example.test/audio.mp3
    C:/does/not/exist.mp4

    """

    assert IpcController._valid_payload_paths(payload) == [
        str(media_file),
        "https://example.test/video.mp4",
        "http://example.test/audio.mp3",
    ]


def test_valid_payload_paths_ignores_blank_payload():
    assert IpcController._valid_payload_paths("  \n  ") == []


def test_bootstrap_ipc_reuses_controller_payload_parser(monkeypatch):
    calls = []

    monkeypatch.setattr(
        IpcController,
        "_valid_payload_paths",
        staticmethod(lambda text: calls.append(text) or []),
    )

    server = single_instance.SingleInstanceServer(app=object(), window_ref=[], pending_files=[])
    server._bring_current_window_to_front = lambda: None
    server._on_ipc_data(_Conn("https://example.test/video.mp4\n"))

    assert calls == ["https://example.test/video.mp4"]


def test_ipc_controller_brings_window_forward_and_opens_valid_payload_paths(tmp_path):
    media_file = tmp_path / "clip.mp4"
    media_file.write_bytes(b"video")

    class _Window:
        def __init__(self):
            self.front_calls = 0
            self.opened = []

        def _bring_to_front(self):
            self.front_calls += 1

        def open_media_files(self, paths):
            self.opened.append(paths)

    window = _Window()
    controller = IpcController(
        window,
        bring_to_front=window._bring_to_front,
        open_media_files=window.open_media_files,
    )

    controller._on_data(
        _Conn(f"{media_file}\nC:/missing.mp4\nhttps://example.test/song.mp3\n")
    )

    assert window.front_calls == 1
    assert window.opened == [[str(media_file), "https://example.test/song.mp3"]]


def test_ipc_controller_ignores_empty_payload_without_focus_or_open():
    class _Window:
        def __init__(self):
            self.front_calls = 0
            self.opened = []

        def _bring_to_front(self):
            self.front_calls += 1

        def open_media_files(self, paths):
            self.opened.append(paths)

    window = _Window()
    controller = IpcController(
        window,
        bring_to_front=window._bring_to_front,
        open_media_files=window.open_media_files,
    )

    controller._on_data(_Conn(""))

    assert window.front_calls == 0
    assert window.opened == []


def test_single_instance_server_dispatches_media_to_current_window(tmp_path):
    media_file = tmp_path / "clip.mp4"
    media_file.write_bytes(b"video")

    class _Window:
        def __init__(self):
            self.opened = []

        def open_media_files(self, paths):
            self.opened.append(paths)

    window = _Window()
    server = single_instance.SingleInstanceServer(
        app=object(),
        window_ref=[window],
        pending_files=[],
    )
    server._bring_current_window_to_front = lambda: None

    server._on_ipc_data(_Conn(f"{media_file}\nhttps://example.test/clip.mp4\n"))

    assert window.opened == [[str(media_file), "https://example.test/clip.mp4"]]
    assert server._pending_files == []


def test_single_instance_server_queues_unique_media_until_window_exists(tmp_path):
    media_file = tmp_path / "clip.mp4"
    media_file.write_bytes(b"video")
    pending = [str(media_file)]
    server = single_instance.SingleInstanceServer(app=object(), window_ref=[], pending_files=pending)
    server._bring_current_window_to_front = lambda: None
    server._current_window = lambda: None

    server._on_ipc_data(_Conn(f"{media_file}\nhttps://example.test/new.mp4\n"))

    assert pending == [str(media_file), "https://example.test/new.mp4"]
