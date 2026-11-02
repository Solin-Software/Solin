from uuid import uuid4

import pytest
from PySide6.QtCore import QByteArray, QObject
from PySide6.QtNetwork import QLocalSocket

from solin.bootstrap.single_instance import SingleInstanceServer
from solin.controllers.ipc_controller import IpcController, encode_open_request


class _Connection(QObject):
    def __init__(self, payload=b""):
        super().__init__()
        self.pending = payload
        self.written = b""
        self.aborted = False
        self.disconnected = False

    def readAll(self):
        payload, self.pending = self.pending, b""
        return QByteArray(payload)

    def write(self, payload):
        self.written += payload
        return len(payload)

    def flush(self):
        return True

    def disconnectFromServer(self):
        self.disconnected = True

    def abort(self):
        self.aborted = True

    def state(self):
        if self.disconnected or self.aborted:
            return QLocalSocket.LocalSocketState.UnconnectedState
        return QLocalSocket.LocalSocketState.ConnectedState


def _receiver():
    opened, focused = [], []
    receiver = IpcController(
        object(),
        bring_to_front=lambda: focused.append(True),
        open_media_files=opened.append,
    )
    return receiver, opened, focused


def test_valid_payload_paths_keeps_existing_files_and_urls(tmp_path):
    media_file = tmp_path / "clip.mp4"
    media_file.touch()
    assert IpcController._valid_payload_paths(
        f"{media_file}\nhttps://example.test/video.mp4\nhttp://example.test/audio.mp3\nmissing.mp4\n",
    ) == [str(media_file), "https://example.test/video.mp4", "http://example.test/audio.mp3"]


def test_valid_payload_paths_ignores_blank_payload():
    assert IpcController._valid_payload_paths("  \n  ") == []


def test_complete_request_is_acknowledged_after_dispatch(tmp_path):
    media_file = tmp_path / "clip.mp4"
    media_file.touch()
    receiver, opened, focused = _receiver()
    identifier = uuid4().hex
    connection = _Connection(encode_open_request([str(media_file), "missing.mp4"], identifier))
    receiver.receive(connection)
    assert opened == [[str(media_file)]]
    assert focused == [True]
    assert connection.written == identifier.encode("ascii") + b"\n"
    assert connection.disconnected


def test_fragmented_request_is_not_accepted_until_complete():
    receiver, opened, focused = _receiver()
    identifier = uuid4().hex
    payload = encode_open_request(["https://example.test/video.mp4"], identifier)
    connection = _Connection(payload[:10])
    receiver.receive(connection)
    assert opened == []
    assert focused == []
    assert connection.written == b""
    connection.pending = payload[10:]
    receiver.receive(connection)
    assert opened == [["https://example.test/video.mp4"]]
    assert connection.written == identifier.encode("ascii") + b"\n"


def test_retry_is_acknowledged_without_dispatching_twice():
    receiver, opened, focused = _receiver()
    identifier = uuid4().hex
    payload = encode_open_request(["https://example.test/video.mp4"], identifier)
    receiver.receive(_Connection(payload))
    replacement, replacement_opened, _ = _receiver()
    replacement.restore_request_ids(receiver.accepted_request_ids)
    retried = _Connection(payload)
    replacement.receive(retried)
    assert opened == [["https://example.test/video.mp4"]]
    assert focused == [True]
    assert replacement_opened == []
    assert retried.written == identifier.encode("ascii") + b"\n"


@pytest.mark.parametrize(
    "payload",
    [
        b"[]\n",
        b"null\n",
        b"not json\n",
        b'{"id":"invalid","paths":[]}\n',
        b'{"id":"0123456789abcdef0123456789abcdef","paths":[true]}\n',
    ],
)
def test_invalid_request_cannot_dispatch_or_claim_acceptance(payload):
    receiver, opened, focused = _receiver()
    connection = _Connection(payload)
    receiver.receive(connection)
    assert connection.aborted
    assert connection.written == b""
    assert opened == []
    assert focused == []
    assert receiver.accepted_request_ids == []


def test_failed_dispatch_is_not_acknowledged_or_cached():
    receiver, _, _ = _receiver()

    def fail(_paths):
        raise OSError("cannot open media")

    receiver._open_media_files = fail
    connection = _Connection(encode_open_request(["https://example.test/video.mp4"], uuid4().hex))
    with pytest.raises(OSError, match="cannot open media"):
        receiver.receive(connection)
    assert connection.written == b""
    assert receiver.accepted_request_ids == []


def test_focus_request_does_not_import_media():
    receiver, opened, focused = _receiver()
    connection = _Connection(encode_open_request([], uuid4().hex))
    receiver.receive(connection)
    assert opened == []
    assert focused == [True]
    assert connection.written


def test_bootstrap_ipc_uses_shared_request_decoder_and_path_validation(monkeypatch):
    calls = []
    monkeypatch.setattr(
        IpcController,
        "_valid_payload_paths",
        staticmethod(
            lambda text: calls.append(text) or [],
        ),
    )
    server = SingleInstanceServer(object(), [], [])
    server._current_window = lambda: None
    server._receiver.receive(
        _Connection(encode_open_request(["https://example.test/video.mp4"], uuid4().hex))
    )
    assert calls == ["https://example.test/video.mp4"]


def test_bootstrap_dispatches_to_current_window():
    class Window:
        def __init__(self):
            self.opened = []

        def open_media_files(self, paths):
            self.opened.append(paths)

        def _bring_to_front(self):
            pass

    window = Window()
    server = SingleInstanceServer(object(), [window], [])
    server._receiver.receive(
        _Connection(encode_open_request(["https://example.test/video.mp4"], uuid4().hex))
    )
    assert window.opened == [["https://example.test/video.mp4"]]


def test_bootstrap_queues_unique_media_before_a_window_exists():
    pending = ["https://example.test/video.mp4"]
    server = SingleInstanceServer(object(), [], pending)
    server._current_window = lambda: None
    server._receiver.receive(
        _Connection(
            encode_open_request(
                [
                    "https://example.test/video.mp4",
                    "https://example.test/other.mp4",
                ],
                uuid4().hex,
            )
        )
    )
    assert pending == ["https://example.test/video.mp4", "https://example.test/other.mp4"]
