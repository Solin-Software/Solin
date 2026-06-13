from types import SimpleNamespace

from solin.controllers.shutdown_controller import ShutdownController


class _CleanupWidget:
    def __init__(self, fail=False):
        self.cleaned = False
        self.fail = fail

    def cleanup(self):
        self.cleaned = True
        if self.fail:
            raise RuntimeError("cleanup failed")


class _FinishedSignal:
    def __init__(self):
        self.connected = None

    def connect(self, callback):
        self.connected = callback


class _ThreadStub:
    def __init__(self, running=True, stop_after_quit=True):
        self._running = running
        self._stop_after_quit = stop_after_quit
        self.quit_called = False
        self.wait_ms = None
        self.parent = object()
        self.finished = _FinishedSignal()

    def isRunning(self):
        return self._running

    def quit(self):
        self.quit_called = True
        if self._stop_after_quit:
            self._running = False

    def wait(self, wait_ms):
        self.wait_ms = wait_ms

    def setParent(self, parent):
        self.parent = parent

    def deleteLater(self):
        pass


def test_cleanup_widget_calls_cleanup():
    widget = _CleanupWidget()

    ShutdownController._cleanup_widget(widget)

    assert widget.cleaned is True


def test_cleanup_widget_swallows_cleanup_errors():
    widget = _CleanupWidget(fail=True)

    ShutdownController._cleanup_widget(widget)

    assert widget.cleaned is True


def test_stop_conversion_thread_quits_waits_and_removes_finished_thread():
    thread = _ThreadStub(running=True, stop_after_quit=True)
    threads = [thread]

    ShutdownController._stop_conversion_thread(thread, threads)

    assert thread.quit_called is True
    assert thread.wait_ms == 3000
    assert thread not in threads


def test_stop_conversion_thread_defers_delete_when_still_running():
    thread = _ThreadStub(running=True, stop_after_quit=False)
    threads = [thread]

    ShutdownController._stop_conversion_thread(thread, threads)

    assert thread in threads
    assert thread.parent is None
    assert thread.finished.connected == thread.deleteLater


def test_remove_or_queue_tmp_file_queues_when_remove_fails(monkeypatch):
    queued = []

    monkeypatch.setattr(
        "solin.controllers.shutdown_controller.os.path.isfile",
        lambda path: True,
    )
    monkeypatch.setattr(
        "solin.controllers.shutdown_controller.os.remove",
        lambda path: (_ for _ in ()).throw(OSError()),
    )
    monkeypatch.setattr(
        ShutdownController,
        "_queue_pending_deletion",
        lambda _self, path: queued.append(path),
    )

    controller = ShutdownController(SimpleNamespace())
    controller._remove_or_queue_tmp_file("temp.jwlplaylist")

    assert queued == ["temp.jwlplaylist"]
