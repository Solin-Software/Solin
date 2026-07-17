from PySide6.QtCore import QCoreApplication

from solin.core.foundation.qr_codes import generate_qr_png
from solin.ui import qr_generation

_APP = QCoreApplication.instance() or QCoreApplication([])


class _SignalStub:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        for callback in list(self.callbacks):
            callback(*args)


class _FakeQrThread:
    instances = []

    def __init__(self, generation, url, parent=None):
        self.generation = generation
        self.url = url
        self.parent = parent
        self.completed = _SignalStub()
        self.failed = _SignalStub()
        self.finished = _SignalStub()
        self.started = False
        self.interrupted = False
        self.quit_called = False
        self.wait_calls = []
        self.detached = False
        self.deleted = False
        self.running = False
        self.__class__.instances.append(self)

    def start(self):
        self.started = True
        self.running = True

    def requestInterruption(self):
        self.interrupted = True

    def quit(self):
        self.quit_called = True

    def wait(self, wait_ms):
        self.wait_calls.append(wait_ms)
        self.running = False
        return True

    def isRunning(self):
        return self.running

    def setParent(self, _parent):
        self.detached = True

    def deleteLater(self):
        self.deleted = True

    def emit_completed(self, payload):
        self.completed.emit(self.generation, payload)
        self.running = False
        self.finished.emit()

    def emit_failed(self):
        self.failed.emit(self.generation)
        self.running = False
        self.finished.emit()


def _session(monkeypatch):
    _FakeQrThread.instances.clear()
    monkeypatch.setattr(qr_generation, "QrGenerationThread", _FakeQrThread)
    return qr_generation.QrGenerationSession()


def test_generate_qr_png_returns_png_bytes():
    data = generate_qr_png("http://solin.test")

    assert data.startswith(b"\x89PNG\r\n\x1a\n")


def test_qr_generation_thread_emits_png_bytes(monkeypatch):
    monkeypatch.setattr(qr_generation, "generate_qr_png", lambda url: url.encode())
    thread = qr_generation.QrGenerationThread(7, "http://solin.test")
    received = []
    failed = []
    thread.completed.connect(lambda generation, data: received.append((generation, data)))
    thread.failed.connect(lambda generation: failed.append(generation))

    thread.run()

    assert received == [(7, b"http://solin.test")]
    assert failed == []


def test_qr_generation_thread_normalizes_failures(monkeypatch):
    def _raise(_url):
        raise RuntimeError("codec failed")

    monkeypatch.setattr(qr_generation, "generate_qr_png", _raise)
    thread = qr_generation.QrGenerationThread(3, "http://solin.test")
    failed = []
    thread.failed.connect(failed.append)

    thread.run()

    assert failed == [3]


def test_qr_generation_session_starts_and_fences_cancelled_results(monkeypatch):
    session = _session(monkeypatch)
    ready = []
    failed = []
    session.ready.connect(ready.append)
    session.failed.connect(lambda: failed.append(True))

    assert session.start("http://old.test") is True
    old_thread = _FakeQrThread.instances[-1]
    session.cancel()
    old_thread.emit_completed(b"old")

    assert ready == []
    assert failed == []

    assert session.start("http://new.test") is True
    new_thread = _FakeQrThread.instances[-1]
    new_thread.emit_completed(b"new")

    assert ready == [b"new"]
    assert failed == []


def test_qr_generation_session_emits_current_failures_only(monkeypatch):
    session = _session(monkeypatch)
    failed = []
    session.failed.connect(lambda: failed.append(True))

    session.start("http://old.test")
    old_thread = _FakeQrThread.instances[-1]
    session.start("http://new.test")
    new_thread = _FakeQrThread.instances[-1]

    old_thread.emit_failed()
    new_thread.emit_failed()

    assert failed == [True]


def test_qr_generation_session_close_interrupts_owned_threads(monkeypatch):
    session = _session(monkeypatch)
    session.start("http://solin.test")
    thread = _FakeQrThread.instances[-1]

    session.close()
    session.close()

    assert thread.interrupted is True
    assert thread.quit_called is True
    assert thread.wait_calls == [2000]
    assert session.start("http://again.test") is False
